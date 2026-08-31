"""工作区工具共享服务和安全 profile。"""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias

from .atomic_writer import AtomicFileWriter
from .mutation_queue import FileMutationQueue
from .observations import FileObservationStore
from .output import OutputPolicy
from .path_policy import WorkspacePathPolicy
from .process_runner import ProcessRunner


ToolSecurityProfile: TypeAlias = Literal[
    "read-only",
    "workspace-write",
    "full-access",
]

READ_ONLY_TOOL_NAMES = ("read", "list_dir", "find", "grep")
WORKSPACE_WRITE_TOOL_NAMES = (*READ_ONLY_TOOL_NAMES, "write", "edit")
FULL_ACCESS_TOOL_NAMES = (*WORKSPACE_WRITE_TOOL_NAMES, "shell")


@dataclass(frozen=True, slots=True)
class ToolServices:
    """由同一组工具共享的工作区安全状态。

    ``full-access`` 只额外开启受信 Shell；所有 Python 文件工具仍严格限制在
    workspace。Shell 不是安全沙箱，因此还必须传 ``allow_trusted_shell=True``。
    """

    workspace_root: Path
    security_profile: ToolSecurityProfile
    path_policy: WorkspacePathPolicy
    observations: FileObservationStore
    mutation_queue: FileMutationQueue
    atomic_writer: AtomicFileWriter
    output_policy: OutputPolicy
    spill_directory: Path
    process_runner: ProcessRunner | None
    configuration_fingerprint: str
    _spill_owner: tempfile.TemporaryDirectory[str] = field(
        repr=False,
        compare=False,
    )

    @classmethod
    def create(
        cls,
        workspace: str | Path,
        *,
        security_profile: ToolSecurityProfile = "read-only",
        allow_trusted_shell: bool = False,
        output_policy: OutputPolicy | None = None,
        inherit_shell_env: bool = True,
        blocked_shell_env_names: set[str] | frozenset[str] | None = None,
        shell_executable: str | None = None,
        shell_timeout_seconds: float = 30,
        shell_maximum_timeout_seconds: float = 300,
        atomic_writer: AtomicFileWriter | None = None,
    ) -> "ToolServices":
        if security_profile not in {
            "read-only",
            "workspace-write",
            "full-access",
        }:
            raise ValueError(f"未知 Tool Security Profile：{security_profile}")
        if type(allow_trusted_shell) is not bool:
            raise TypeError("allow_trusted_shell 必须是布尔值")
        if type(inherit_shell_env) is not bool:
            raise TypeError("inherit_shell_env 必须是布尔值")
        if allow_trusted_shell and security_profile != "full-access":
            raise ValueError("trusted Shell 只能与 full-access profile 一起启用")
        if security_profile == "full-access" and allow_trusted_shell is not True:
            raise ValueError(
                "full-access 包含非沙箱 Shell，必须显式传 allow_trusted_shell=True"
            )
        policy = WorkspacePathPolicy(workspace)
        limits = output_policy or OutputPolicy()
        spill_owner = tempfile.TemporaryDirectory(prefix="pi-agent-output-")
        spill_directory = Path(spill_owner.name).resolve()
        try:
            runner = None
            if security_profile == "full-access":
                runner = ProcessRunner(
                    policy,
                    limits,
                    spill_directory,
                    trusted_opt_in=True,
                    inherit_env=inherit_shell_env,
                    blocked_env_names=blocked_shell_env_names,
                    shell_executable=shell_executable,
                    default_timeout_seconds=shell_timeout_seconds,
                    maximum_timeout_seconds=shell_maximum_timeout_seconds,
                )
            writer = atomic_writer or AtomicFileWriter()
            fingerprint = _configuration_fingerprint(
                policy=policy,
                security_profile=security_profile,
                limits=limits,
                inherit_shell_env=inherit_shell_env,
                blocked_shell_env_names=blocked_shell_env_names,
                shell_executable=shell_executable,
                shell_timeout_seconds=shell_timeout_seconds,
                shell_maximum_timeout_seconds=shell_maximum_timeout_seconds,
                writer=writer,
            )
            return cls(
                workspace_root=policy.workspace_root,
                security_profile=security_profile,
                path_policy=policy,
                observations=FileObservationStore(policy),
                mutation_queue=FileMutationQueue(policy),
                atomic_writer=writer,
                output_policy=limits,
                spill_directory=spill_directory,
                process_runner=runner,
                configuration_fingerprint=fingerprint,
                _spill_owner=spill_owner,
            )
        except BaseException:
            spill_owner.cleanup()
            raise

    @property
    def allowed_tool_names(self) -> tuple[str, ...]:
        if self.security_profile == "read-only":
            return READ_ONLY_TOOL_NAMES
        if self.security_profile == "workspace-write":
            return WORKSPACE_WRITE_TOOL_NAMES
        return FULL_ACCESS_TOOL_NAMES

    def security_version(self, base: str) -> str:
        """把 workspace/profile/限制绑定进 Runtime 的持久安全合同。"""

        return f"{base}+cfg.{self.configuration_fingerprint[:24]}"

    def close(self) -> None:
        """删除本服务创建的敏感 Shell spill 临时目录。"""

        self._spill_owner.cleanup()

    def __enter__(self) -> "ToolServices":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def _configuration_fingerprint(
    *,
    policy: WorkspacePathPolicy,
    security_profile: ToolSecurityProfile,
    limits: OutputPolicy,
    inherit_shell_env: bool,
    blocked_shell_env_names: set[str] | frozenset[str] | None,
    shell_executable: str | None,
    shell_timeout_seconds: float,
    shell_maximum_timeout_seconds: float,
    writer: AtomicFileWriter,
) -> str:
    payload = {
        "contractVersion": 1,
        "workspace": policy.key(policy.workspace_root),
        "profile": security_profile,
        "output": {
            "lines": limits.max_lines,
            "bytes": limits.max_bytes,
            "spillBytes": limits.max_spill_bytes,
        },
        "shell": {
            "inheritEnv": inherit_shell_env,
            "blockedEnv": sorted(
                name.casefold() for name in (blocked_shell_env_names or set())
            ),
            "executable": shell_executable,
            "timeout": float(shell_timeout_seconds),
            "maximumTimeout": float(shell_maximum_timeout_seconds),
        },
        "writerType": f"{type(writer).__module__}.{type(writer).__qualname__}",
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
