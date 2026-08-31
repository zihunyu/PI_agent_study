"""显式受信 opt-in 的本机 Shell 进程管理。"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import signal
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import cast

from ..cancellation import CancellationToken
from .errors import WorkspaceToolError
from .output import AccumulatedOutput, OutputAccumulator, OutputPolicy
from .path_policy import WorkspacePathPolicy


_SENSITIVE_ENV_FRAGMENTS = (
    "API_KEY",
    "ACCESS_KEY",
    "AUTH",
    "BEARER",
    "COOKIE",
    "CREDENTIAL",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "SESSION_TOKEN",
    "TOKEN",
)


@dataclass(frozen=True, slots=True)
class ProcessRunResult:
    exit_code: int
    stdout: AccumulatedOutput
    stderr: AccumulatedOutput


@dataclass(frozen=True, slots=True, init=False)
class ProcessRunner:
    """运行可信本机命令。

    这不是安全沙箱：命令仍可访问网络、工作区外路径和本机资源。实现只提供
    cwd 校验、敏感环境清理、输出限额，以及尽力而为的跨平台进程树终止。
    """

    policy: WorkspacePathPolicy
    output_policy: OutputPolicy
    spill_directory: Path
    inherit_env: bool
    extra_env: Mapping[str, str]
    blocked_env_names: frozenset[str]
    shell_executable: str | None
    default_timeout_seconds: float
    maximum_timeout_seconds: float
    termination_grace_seconds: float

    def __init__(
        self,
        policy: WorkspacePathPolicy,
        output_policy: OutputPolicy,
        spill_directory: Path,
        *,
        trusted_opt_in: bool,
        inherit_env: bool = True,
        extra_env: Mapping[str, str] | None = None,
        blocked_env_names: set[str] | frozenset[str] | None = None,
        shell_executable: str | None = None,
        default_timeout_seconds: float = 30,
        maximum_timeout_seconds: float = 300,
        termination_grace_seconds: float = 0.5,
    ) -> None:
        if trusted_opt_in is not True:
            raise ValueError("ProcessRunner 只能在显式 trusted opt-in 后创建")
        if type(inherit_env) is not bool:
            raise TypeError("inherit_env 必须是布尔值")
        for name, value in (
            ("default_timeout_seconds", default_timeout_seconds),
            ("maximum_timeout_seconds", maximum_timeout_seconds),
            ("termination_grace_seconds", termination_grace_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} 必须是有限正数")
        if default_timeout_seconds > maximum_timeout_seconds:
            raise ValueError("默认 Shell timeout 不能超过最大值")
        if shell_executable is not None and (
            not isinstance(shell_executable, str) or not shell_executable.strip()
        ):
            raise ValueError("shell_executable 必须是非空字符串或 None")
        if extra_env is not None and any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in extra_env.items()
        ):
            raise TypeError("extra_env 的名称和值必须是字符串")
        if blocked_env_names is not None and any(
            not isinstance(name, str) or not name for name in blocked_env_names
        ):
            raise TypeError("blocked_env_names 必须只包含非空字符串")
        object.__setattr__(self, "policy", policy)
        object.__setattr__(self, "output_policy", output_policy)
        object.__setattr__(self, "spill_directory", spill_directory)
        object.__setattr__(self, "inherit_env", inherit_env)
        object.__setattr__(
            self,
            "extra_env",
            MappingProxyType(dict(extra_env or {})),
        )
        object.__setattr__(
            self,
            "blocked_env_names",
            frozenset(name.casefold() for name in (blocked_env_names or set())),
        )
        object.__setattr__(self, "shell_executable", shell_executable)
        object.__setattr__(
            self,
            "default_timeout_seconds",
            float(default_timeout_seconds),
        )
        object.__setattr__(
            self,
            "maximum_timeout_seconds",
            float(maximum_timeout_seconds),
        )
        object.__setattr__(
            self,
            "termination_grace_seconds",
            float(termination_grace_seconds),
        )

    async def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout_seconds: float | None,
        cancellation: CancellationToken,
        on_chunk: Callable[[str, str], None] | None = None,
    ) -> ProcessRunResult:
        if not isinstance(command, str) or not command.strip():
            raise WorkspaceToolError("invalid_args", "Shell command 不能为空")
        if cancellation.cancelled:
            raise WorkspaceToolError("aborted", cancellation.reason)
        canonical_cwd = self.policy.resolve(str(cwd), must_exist=True)
        if not canonical_cwd.is_dir():
            raise WorkspaceToolError(
                "not_directory",
                "Shell cwd 不是目录",
                path=str(canonical_cwd),
            )
        timeout = (
            self.default_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
            or timeout > self.maximum_timeout_seconds
        ):
            raise WorkspaceToolError(
                "invalid_args",
                f"Shell timeout 必须在 0 到 {self.maximum_timeout_seconds:g} 秒之间",
            )
        timeout = float(timeout)
        argv = self._shell_argv(command)
        try:
            if os.name == "nt":
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=canonical_cwd,
                    env=self._sanitized_environment(),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=canonical_cwd,
                    env=self._sanitized_environment(),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
        except (OSError, ValueError) as error:
            raise WorkspaceToolError(
                "spawn_error",
                "无法启动 Shell 进程",
                path=str(canonical_cwd),
            ) from error

        stdout_acc = OutputAccumulator(
            self.output_policy,
            mode="tail",
            spill_directory=self.spill_directory,
            prefix="shell-stdout-",
        )
        stderr_acc = OutputAccumulator(
            self.output_policy,
            mode="tail",
            spill_directory=self.spill_directory,
            prefix="shell-stderr-",
        )
        stdout_task = asyncio.create_task(
            self._drain(process.stdout, stdout_acc, "stdout", on_chunk)
        )
        stderr_task = asyncio.create_task(
            self._drain(process.stderr, stderr_acc, "stderr", on_chunk)
        )
        wait_task = asyncio.create_task(process.wait())
        cancel_task = asyncio.create_task(cancellation.wait())
        drain_failure_task = asyncio.create_task(
            self._wait_for_drain_failure(stdout_task, stderr_task)
        )
        failure: tuple[str, str] | None = None
        cancelled_error: asyncio.CancelledError | None = None
        drain_error: BaseException | None = None
        try:
            done, _ = await asyncio.wait(
                {wait_task, cancel_task, drain_failure_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wait_task in done:
                await wait_task
            elif cancel_task in done:
                failure = ("aborted", cancellation.reason)
                await self._terminate_tree(process)
                await wait_task
            elif drain_failure_task in done:
                try:
                    await drain_failure_task
                except BaseException as error:
                    drain_error = error
                await self._terminate_tree(process)
                await wait_task
            else:
                failure = ("timeout", f"Shell 命令超过 {timeout:g} 秒")
                await self._terminate_tree(process)
                await wait_task
        except asyncio.CancelledError as error:
            cancelled_error = error
            await self._terminate_tree(process)
            await wait_task
        finally:
            cancel_task.cancel()
            drain_failure_task.cancel()
            await asyncio.gather(
                cancel_task,
                drain_failure_task,
                return_exceptions=True,
            )
            drain_results = await asyncio.gather(
                stdout_task,
                stderr_task,
                return_exceptions=True,
            )
            if drain_error is None:
                drain_error = next(
                    (
                        result
                        for result in drain_results
                        if isinstance(result, BaseException)
                    ),
                    None,
                )

        stdout: AccumulatedOutput | None = None
        stderr: AccumulatedOutput | None = None
        finish_error: BaseException | None = None
        try:
            stdout = stdout_acc.finish()
        except BaseException as error:
            finish_error = error
        try:
            stderr = stderr_acc.finish()
        except BaseException as error:
            if finish_error is None:
                finish_error = error
        if cancelled_error is not None:
            raise cancelled_error
        if drain_error is not None:
            raise RuntimeError("Shell 输出管道读取失败") from drain_error
        if finish_error is not None:
            raise RuntimeError("Shell 输出持久化失败") from finish_error
        assert stdout is not None and stderr is not None
        if failure is not None:
            code, message = failure
            raise WorkspaceToolError(
                code,
                message,
                details=self._output_details(process.returncode, stdout, stderr),
            )
        return ProcessRunResult(
            exit_code=int(process.returncode or 0),
            stdout=stdout,
            stderr=stderr,
        )

    async def _drain(
        self,
        stream: asyncio.StreamReader | None,
        accumulator: OutputAccumulator,
        stream_name: str,
        on_chunk: Callable[[str, str], None] | None,
    ) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(16 * 1024)
            if not chunk:
                return
            accumulator.feed(chunk)
            if on_chunk is not None:
                try:
                    on_chunk(stream_name, chunk.decode("utf-8", errors="replace"))
                except Exception:
                    # UI update 不能阻塞 pipe drain，否则子进程可能死锁。
                    pass

    @staticmethod
    async def _wait_for_drain_failure(
        stdout_task: asyncio.Task[None],
        stderr_task: asyncio.Task[None],
    ) -> None:
        pending = {stdout_task, stderr_task}
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                task.result()
        # 正常 EOF 不是故障；保持等待直到 process.wait 分支取消本 watcher。
        await asyncio.Future()

    async def _terminate_tree(
        self,
        process: asyncio.subprocess.Process,
    ) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            taskkill = shutil.which("taskkill")
            if taskkill is not None:
                try:
                    killer = await asyncio.create_subprocess_exec(
                        taskkill,
                        "/PID",
                        str(process.pid),
                        "/T",
                        "/F",
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        await asyncio.wait_for(killer.wait(), timeout=5)
                    except TimeoutError:
                        killer.kill()
                        await killer.wait()
                except OSError:
                    # taskkill 不可用时仍至少终止直接子进程。
                    pass
            if process.returncode is None:
                process.kill()
        else:
            kill_process_group = cast(
                Callable[[int, int], None],
                getattr(os, "killpg"),
            )
            try:
                kill_process_group(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=self.termination_grace_seconds,
                )
                return
            except TimeoutError:
                try:
                    kill_process_group(
                        process.pid,
                        cast(int, getattr(signal, "SIGKILL")),
                    )
                except ProcessLookupError:
                    pass
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=max(1.0, self.termination_grace_seconds * 2),
            )
        except TimeoutError:
            if process.returncode is None:
                process.kill()
                await process.wait()

    def _shell_argv(self, command: str) -> tuple[str, ...]:
        if self.shell_executable:
            executable = self.shell_executable
        elif os.name == "nt":
            executable = (
                shutil.which("powershell.exe")
                or shutil.which("powershell")
                or "powershell.exe"
            )
        else:
            executable = "/bin/bash"
        if os.name == "nt":
            return (
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            )
        return (executable, "--noprofile", "--norc", "-c", command)

    def _sanitized_environment(self) -> dict[str, str]:
        if self.inherit_env:
            environment = dict(os.environ)
        else:
            environment = {
                name: os.environ[name]
                for name in ("PATH", "PATHEXT", "SYSTEMROOT", "COMSPEC", "TMP", "TEMP")
                if name in os.environ
            }
        environment.update(self.extra_env)
        return {
            name: value
            for name, value in environment.items()
            if not self._blocked_environment_name(name)
        }

    def _blocked_environment_name(self, name: str) -> bool:
        folded = name.casefold()
        if folded in self.blocked_env_names:
            return True
        upper = name.upper()
        return any(fragment in upper for fragment in _SENSITIVE_ENV_FRAGMENTS)

    @staticmethod
    def _output_details(
        exit_code: int | None,
        stdout: AccumulatedOutput,
        stderr: AccumulatedOutput,
    ) -> dict[str, object]:
        return {
            "exitCode": exit_code,
            "stdout": stdout.text,
            "stderr": stderr.text,
            "stdoutTruncated": stdout.truncated,
            "stderrTruncated": stderr.truncated,
            "stdoutSpillPath": stdout.spill_path,
            "stderrSpillPath": stderr.spill_path,
            "stdoutSpillComplete": stdout.spill_complete,
            "stderrSpillComplete": stderr.spill_complete,
            "stdoutDroppedBytes": stdout.dropped_bytes,
            "stderrDroppedBytes": stderr.dropped_bytes,
        }
