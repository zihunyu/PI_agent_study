"""Replaceable filesystem/process environments with explicit enforcement facts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from .cancellation import CancellationToken
from .async_utils import await_owned_cleanup, durable_to_thread
from .tools.atomic_writer import AtomicFileWriter
from .tools.mutation_queue import FileMutationQueue
from .tools.path_policy import WorkspacePathPolicy
from .tools.workspace_validators import object_args, text_arg
from .types import AgentTool, AgentToolResult, ToolUpdateCallback


@dataclass(frozen=True, slots=True)
class EnvironmentCapabilities:
    backend: str
    filesystem_isolated: bool
    network_isolated: bool
    processes_isolated: bool
    workspace_read_only: bool


@dataclass(frozen=True, slots=True)
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str


@runtime_checkable
class ExecutionEnvironment(Protocol):
    @property
    def capabilities(self) -> EnvironmentCapabilities: ...
    @property
    def configuration_fingerprint(self) -> str: ...
    async def read_file(
        self, path: str, cancellation: CancellationToken, *, max_bytes: int = 1_048_576
    ) -> bytes: ...
    async def list_dir(
        self, path: str, cancellation: CancellationToken
    ) -> tuple[str, ...]: ...
    async def write_file(
        self,
        path: str,
        data: bytes,
        cancellation: CancellationToken,
        *,
        expected_digest: str | None = None,
    ) -> str: ...
    async def run(
        self,
        argv: tuple[str, ...],
        cancellation: CancellationToken,
        *,
        timeout: float = 30,
    ) -> ProcessResult: ...
    async def aclose(self) -> None: ...


def _validate_process(argv: tuple[str, ...], timeout: float, max_bytes: int) -> None:
    if not argv or any(
        not isinstance(item, str) or not item or "\x00" in item for item in argv
    ):
        raise ValueError("process argv must contain non-empty strings")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 600
    ):
        raise ValueError("process timeout must be finite and at most 600 seconds")
    if type(max_bytes) is not int or not 0 < max_bytes <= 32 * 1024 * 1024:
        raise ValueError("process output limit is invalid")


async def _process(
    argv: tuple[str, ...],
    token: CancellationToken,
    *,
    cwd: Path | None = None,
    data: bytes | None = None,
    timeout: float = 30,
    max_bytes: int = 2_097_152,
) -> ProcessResult:
    _validate_process(argv, timeout, max_bytes)
    token.throw_if_cancelled()
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "TEMP", "TMP", "COMSPEC"}
    }
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **(
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        ),
    )
    used = 0

    async def read(stream: Any) -> bytes:
        nonlocal used
        chunks: list[bytes] = []
        while True:
            chunk = await stream.read(16384)
            if not chunk:
                return b"".join(chunks)
            used += len(chunk)
            if used > max_bytes:
                raise ValueError("process output exceeds limit")
            chunks.append(chunk)

    async def communicate() -> ProcessResult:
        readers = [
            asyncio.create_task(read(proc.stdout)),
            asyncio.create_task(read(proc.stderr)),
        ]
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.write(data or b"")
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    proc.stdin.close()
            stdout, stderr = await asyncio.gather(*readers)
            return ProcessResult(
                await proc.wait(),
                stdout.decode("utf-8", "replace"),
                stderr.decode("utf-8", "replace"),
            )
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

    work = asyncio.create_task(communicate())
    cancelled = asyncio.create_task(token.wait())
    try:
        async with asyncio.timeout(timeout):
            done, _ = await asyncio.wait(
                {work, cancelled}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancelled in done:
                token.throw_if_cancelled()
            return await work
    finally:

        async def cleanup() -> None:
            if os.name != "nt":
                import signal

                try:
                    getattr(os, "killpg")(proc.pid, getattr(signal, "SIGKILL"))
                except ProcessLookupError:
                    pass
            if proc.returncode is None:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec(
                        str(
                            Path(os.environ.get("SYSTEMROOT", "C:/Windows"))
                            / "System32"
                            / "taskkill.exe"
                        ),
                        "/PID",
                        str(proc.pid),
                        "/T",
                        "/F",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    try:
                        await asyncio.wait_for(killer.wait(), 5)
                    except TimeoutError:
                        killer.kill()
                        await killer.wait()
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
            work.cancel()
            cancelled.cancel()
            await asyncio.gather(work, cancelled, return_exceptions=True)

        await await_owned_cleanup(cleanup())


class LocalExecutionEnvironment:
    def __init__(
        self,
        workspace: str | Path,
        *,
        read_only: bool = True,
        allow_trusted_processes: bool = False,
    ) -> None:
        if type(read_only) is not bool or type(allow_trusted_processes) is not bool:
            raise ValueError("environment flags must be booleans")
        self.policy = WorkspacePathPolicy(workspace)
        self.capabilities = EnvironmentCapabilities(
            "local", False, False, False, read_only
        )
        self.allow_trusted_processes = allow_trusted_processes
        self._mutations = FileMutationQueue(self.policy)
        self._closed = False

    @property
    def configuration_fingerprint(self) -> str:
        return hashlib.sha256(
            repr(
                (
                    str(self.policy.workspace_root),
                    self.capabilities,
                    self.allow_trusted_processes,
                )
            ).encode()
        ).hexdigest()

    def _check(self, token: CancellationToken) -> None:
        if self._closed:
            raise RuntimeError("execution environment is closed")
        token.throw_if_cancelled()

    async def read_file(
        self, path: str, cancellation: CancellationToken, *, max_bytes: int = 1_048_576
    ) -> bytes:
        self._check(cancellation)
        if type(max_bytes) is not int or not 0 < max_bytes <= 16 * 1024 * 1024:
            raise ValueError("file read limit is invalid")
        resolved = self.policy.resolve(path, must_exist=True)

        def read() -> bytes:
            with resolved.open("rb") as stream:
                result = stream.read(max_bytes + 1)
            if len(result) > max_bytes:
                raise ValueError("file exceeds read limit")
            return result

        result = await durable_to_thread(read)
        cancellation.throw_if_cancelled()
        return result

    async def list_dir(
        self, path: str, cancellation: CancellationToken
    ) -> tuple[str, ...]:
        self._check(cancellation)
        resolved = self.policy.resolve(path, must_exist=True)

        def scan() -> tuple[str, ...]:
            values: list[str] = []
            for child in resolved.iterdir():
                if len(values) >= 4096:
                    raise ValueError("directory exceeds entry limit")
                if (
                    child.is_symlink()
                    or child.name.casefold() in self.policy.reserved_names
                ):
                    continue
                values.append(child.name + ("/" if child.is_dir() else ""))
            return tuple(sorted(values))

        result = await durable_to_thread(scan)
        cancellation.throw_if_cancelled()
        return result

    async def write_file(
        self,
        path: str,
        data: bytes,
        cancellation: CancellationToken,
        *,
        expected_digest: str | None = None,
    ) -> str:
        self._check(cancellation)
        if self.capabilities.workspace_read_only:
            raise PermissionError("workspace is read-only")
        if not isinstance(data, bytes) or len(data) > 5 * 1024 * 1024:
            raise ValueError("write payload must be bounded bytes")
        target = self.policy.resolve(path, must_exist=False)
        async with self._mutations.acquire(target):
            target = self.policy.revalidate(path, target, must_exist=False)
            if target.exists():
                if (
                    expected_digest is None
                    or hashlib.sha256(
                        await self.read_file(
                            path, cancellation, max_bytes=5 * 1024 * 1024
                        )
                    ).hexdigest()
                    != expected_digest
                ):
                    raise ValueError("stale or missing file version")
            elif expected_digest is not None:
                raise ValueError("file no longer exists")
            cancellation.throw_if_cancelled()
            # Complete the bounded atomic publication even when the outer task
            # is cancelled; callers must reconcile an interrupted write.
            await durable_to_thread(
                AtomicFileWriter().write, target, data, create_only=not target.exists()
            )
        return hashlib.sha256(data).hexdigest()

    async def run(
        self,
        argv: tuple[str, ...],
        cancellation: CancellationToken,
        *,
        timeout: float = 30,
    ) -> ProcessResult:
        self._check(cancellation)
        if not self.allow_trusted_processes:
            raise PermissionError(
                "local process execution requires explicit trusted opt-in"
            )
        return await _process(
            argv, cancellation, cwd=self.policy.workspace_root, timeout=timeout
        )

    async def aclose(self) -> None:
        self._closed = True


_CONTAINER_FILE_HELPER = r"""
import base64, hashlib, json, os, pathlib, sys, tempfile
request = json.load(sys.stdin)
root = pathlib.Path('/workspace')
raw = pathlib.PurePosixPath(request['path'])
if raw.is_absolute() or '..' in raw.parts:
    raise ValueError('path outside workspace')
path = root.joinpath(*raw.parts)
for parent in (path, *path.parents):
    if parent == root.parent: break
    if parent.is_symlink(): raise ValueError('symlink paths are forbidden')
if not path.resolve().is_relative_to(root): raise ValueError('path outside workspace')
op = request['op']
if op == 'read':
    with path.open('rb') as stream: data = stream.read(request['limit'] + 1)
    if len(data) > request['limit']: raise ValueError('file exceeds limit')
    print(json.dumps({'data': base64.b64encode(data).decode()}))
elif op == 'list':
    values = []
    for child in path.iterdir():
        if len(values) >= 4096: raise ValueError('entry limit')
        if not child.is_symlink(): values.append(child.name + ('/' if child.is_dir() else ''))
    print(json.dumps({'entries': sorted(values)}))
elif op == 'write':
    import fcntl
    with open('/workspace/.agent-write-lock', 'a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        expected = request.get('expected')
        if path.exists():
            with path.open('rb') as stream: previous = stream.read(5 * 1024 * 1024 + 1)
            if len(previous) > 5 * 1024 * 1024 or expected != hashlib.sha256(previous).hexdigest(): raise ValueError('stale file version')
        elif expected is not None: raise ValueError('file missing')
        data = base64.b64decode(request['data'], validate=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as out: out.write(data); out.flush(); os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        print(json.dumps({'digest': hashlib.sha256(data).hexdigest()}))
else: raise ValueError('unknown operation')
"""


class DockerExecutionEnvironment:
    """One ephemeral container per operation; all file/process IO stays inside.

    The image is selected by the operator and must already be present. No image
    pull, Docker socket mount, host network, elevated capabilities or host fallback.
    """

    def __init__(
        self, workspace: Path, image: str, executable: str, read_only: bool
    ) -> None:
        self.workspace = workspace
        self.image = image
        self.executable = executable
        self.capabilities = EnvironmentCapabilities(
            "docker", True, True, True, read_only
        )
        self._closed = False
        self._active: set[str] = set()

    @classmethod
    async def create(
        cls,
        workspace: str | Path,
        *,
        image: str,
        read_only: bool = True,
        executable: str = "docker",
    ) -> DockerExecutionEnvironment:
        root = Path(workspace).resolve(strict=True)
        if not root.is_dir() or "," in str(root) or "\n" in str(root):
            raise ValueError("invalid container workspace mount")
        if (
            type(read_only) is not bool
            or not isinstance(image, str)
            or not image
            or image.startswith("-")
            or any(c.isspace() for c in image)
        ):
            raise ValueError("invalid trusted Docker configuration")
        binary = shutil.which(executable)
        if binary is None:
            raise RuntimeError("Docker runtime unavailable; host fallback is forbidden")
        token = CancellationToken()
        for args in (
            (binary, "info", "--format", "{{.OSType}}"),
            (binary, "image", "inspect", image, "--format", "{{.Id}}"),
        ):
            result = await _process(args, token, timeout=15)
            if result.exit_code != 0:
                raise RuntimeError(
                    "Docker daemon or configured local image is unavailable"
                )
            if args[1] == "info" and result.stdout.strip() != "linux":
                raise RuntimeError("sandbox requires Linux containers")
        image_id = result.stdout.strip()
        if not image_id.startswith("sha256:") or len(image_id) != 71:
            raise RuntimeError("Docker did not return a stable image digest")
        instance = cls(root, image_id, binary, read_only)
        probe = await instance.run(
            (
                "python",
                "-c",
                "import os; assert os.geteuid()!=0; print('sandbox-ready')",
            ),
            token,
            timeout=15,
        )
        if probe.exit_code != 0 or "sandbox-ready" not in probe.stdout:
            raise RuntimeError("configured image failed sandbox startup probe")
        return instance

    def _argv(self, name: str, argv: tuple[str, ...]) -> tuple[str, ...]:
        mount = f"type=bind,source={self.workspace},target=/workspace" + (
            ",readonly" if self.capabilities.workspace_read_only else ""
        )
        return (
            self.executable,
            "run",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=64",
            "--memory=256m",
            "--cpus=1",
            "--user=65534:65534",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--mount",
            mount,
            "--workdir=/workspace",
            "-i",
            self.image,
            *argv,
        )

    @property
    def configuration_fingerprint(self) -> str:
        return hashlib.sha256(
            repr(
                (str(self.workspace), self.image, self.executable, self.capabilities)
            ).encode()
        ).hexdigest()

    async def _remove(self, name: str) -> None:
        if name not in self._active:
            return
        result = await _process(
            (self.executable, "rm", "--force", name), CancellationToken(), timeout=10
        )
        if result.exit_code != 0 and "No such container" not in result.stderr:
            raise RuntimeError("owned sandbox container cleanup failed")
        self._active.discard(name)

    async def _run(
        self,
        argv: tuple[str, ...],
        token: CancellationToken,
        timeout: float,
        data: bytes | None = None,
    ) -> ProcessResult:
        if self._closed:
            raise RuntimeError("execution environment is closed")
        _validate_process(argv, timeout, 16 * 1024 * 1024)
        name = "pi-agent-" + uuid4().hex
        self._active.add(name)
        try:
            return await _process(
                self._argv(name, argv),
                token,
                data=data,
                timeout=timeout,
                max_bytes=16 * 1024 * 1024,
            )
        finally:
            await await_owned_cleanup(self._remove(name))

    async def run(
        self,
        argv: tuple[str, ...],
        cancellation: CancellationToken,
        *,
        timeout: float = 30,
    ) -> ProcessResult:
        return await self._run(argv, cancellation, timeout)

    async def _file(
        self, request: dict[str, Any], token: CancellationToken
    ) -> dict[str, Any]:
        result = await self._run(
            ("python", "-c", _CONTAINER_FILE_HELPER),
            token,
            30,
            json.dumps(request).encode(),
        )
        if result.exit_code != 0:
            raise ValueError(
                "sandbox file operation failed; check path, access and expected version"
            )
        return json.loads(result.stdout)

    async def read_file(
        self, path: str, cancellation: CancellationToken, *, max_bytes: int = 1_048_576
    ) -> bytes:
        if type(max_bytes) is not int or not 0 < max_bytes <= 5 * 1024 * 1024:
            raise ValueError("file read limit is invalid")
        result = await self._file(
            {"op": "read", "path": path, "limit": max_bytes}, cancellation
        )
        return base64.b64decode(result["data"], validate=True)

    async def list_dir(
        self, path: str, cancellation: CancellationToken
    ) -> tuple[str, ...]:
        result = await self._file({"op": "list", "path": path}, cancellation)
        return tuple(result["entries"])

    async def write_file(
        self,
        path: str,
        data: bytes,
        cancellation: CancellationToken,
        *,
        expected_digest: str | None = None,
    ) -> str:
        if self.capabilities.workspace_read_only:
            raise PermissionError("sandbox workspace is read-only")
        if not isinstance(data, bytes) or len(data) > 5 * 1024 * 1024:
            raise ValueError("write payload exceeds limit")
        result = await self._file(
            {
                "op": "write",
                "path": path,
                "data": base64.b64encode(data).decode(),
                "expected": expected_digest,
            },
            cancellation,
        )
        return str(result["digest"])

    async def aclose(self) -> None:
        self._closed = True
        errors = await asyncio.gather(
            *(self._remove(name) for name in tuple(self._active)),
            return_exceptions=True,
        )
        failures = [error for error in errors if isinstance(error, BaseException)]
        if failures:
            raise BaseExceptionGroup("sandbox cleanup failed", failures)


def create_environment_tools(
    environment: ExecutionEnvironment, *, include_process: bool = False
) -> list[AgentTool]:
    if not isinstance(environment, ExecutionEnvironment):
        raise TypeError("environment must implement ExecutionEnvironment")
    capabilities = environment.capabilities
    version = environment.configuration_fingerprint
    if not isinstance(version, str) or not version:
        raise ValueError(
            "execution environment requires a stable configuration fingerprint"
        )

    def path_args(value: Any) -> dict[str, Any]:
        args = object_args(value, allowed={"path"}, required={"path"})
        return {"path": text_arg(args["path"], "path")}

    async def read(
        call_id: str,
        args: dict[str, Any],
        token: CancellationToken,
        update: ToolUpdateCallback,
    ) -> AgentToolResult:
        data = await environment.read_file(args["path"], token)
        return AgentToolResult(
            content=[{"type": "text", "text": data.decode("utf-8-sig")}],
            details={
                "path": args["path"],
                "digest": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            },
        )

    async def list_directory(
        call_id: str,
        args: dict[str, Any],
        token: CancellationToken,
        update: ToolUpdateCallback,
    ) -> AgentToolResult:
        entries = await environment.list_dir(args["path"], token)
        return AgentToolResult(
            content=[{"type": "text", "text": "\n".join(entries)}],
            details={"entries": list(entries)},
        )

    schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "工作区相对路径"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    tools = [
        AgentTool(
            name=name,
            label=label,
            description=label + "，所有访问经过同一个执行环境。",
            execute=handler,
            parameters=schema,
            validate_args=path_args,
            timeout_seconds=30,
            replay_policy="safe",
            execution_mode="parallel",
            security_policy_version=version,
        )
        for name, label, handler in (
            ("environment_read", "读取环境文件", read),
            ("environment_list", "列出环境目录", list_directory),
        )
    ]
    if include_process:
        # A replayable process tool is possible only for ephemeral, disconnected
        # containers with read-only persistent input. Writable/trusted processes
        # remain approval-bound tools supplied by the application.
        if not (
            capabilities.filesystem_isolated
            and capabilities.network_isolated
            and capabilities.processes_isolated
            and capabilities.workspace_read_only
        ):
            raise ValueError(
                "model process tool requires an isolated, network-disabled, read-only environment"
            )

        def validate_process(value: Any) -> dict[str, Any]:
            args = object_args(value, allowed={"argv"}, required={"argv"})
            if not isinstance(args["argv"], list) or len(args["argv"]) > 128:
                raise ValueError("argv must be a bounded array")
            _validate_process(tuple(args["argv"]), 30, 1_048_576)
            return {"argv": list(args["argv"])}

        async def run(
            call_id: str,
            args: dict[str, Any],
            token: CancellationToken,
            update: ToolUpdateCallback,
        ) -> AgentToolResult:
            result = await environment.run(tuple(args["argv"]), token)
            if result.exit_code != 0:
                raise RuntimeError("isolated process returned a nonzero exit code")
            return AgentToolResult(
                content=[{"type": "text", "text": result.stdout}],
                details={"exitCode": result.exit_code, "stderr": result.stderr},
            )

        tools.append(
            AgentTool(
                name="sandbox_run",
                label="运行隔离程序",
                description="在无网络、工作区只读的临时容器执行程序，返回输出。",
                execute=run,
                parameters={
                    "type": "object",
                    "properties": {
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "程序及独立参数",
                        }
                    },
                    "required": ["argv"],
                    "additionalProperties": False,
                },
                validate_args=validate_process,
                timeout_seconds=45,
                replay_policy="safe",
                execution_mode="parallel",
                security_policy_version=version,
            )
        )
    return tools
