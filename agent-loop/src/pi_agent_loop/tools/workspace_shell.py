"""受信本机 Shell AgentTool。"""

from __future__ import annotations

from typing import Any

from ..cancellation import CancellationToken
from ..types import AgentTool, AgentToolResult, ToolUpdateCallback
from .errors import WorkspaceToolError
from .output import OutputPolicy, truncate_text
from .process_runner import ProcessRunResult
from .services import ToolServices
from .workspace_validators import number_arg, object_args, text_arg


def create_shell_tool(services: ToolServices) -> AgentTool:
    if services.security_profile != "full-access" or services.process_runner is None:
        raise ValueError("shell 需要显式 full-access + trusted opt-in")
    runner = services.process_runner
    path_policy = services.path_policy
    output_policy = services.output_policy

    def validate(value: Any) -> dict[str, Any]:
        args = object_args(
            value,
            allowed={"command", "timeout", "cwd"},
            required={"command"},
        )
        timeout = args.get("timeout")
        if timeout is not None:
            timeout = number_arg(
                timeout,
                "timeout",
                minimum=0.01,
                maximum=runner.maximum_timeout_seconds,
            )
        return {
            "command": text_arg(args["command"], "command"),
            "timeout": timeout,
            "cwd": text_arg(args.get("cwd", "."), "cwd"),
        }

    async def execute(
        _call_id: str,
        arguments: dict[str, Any],
        cancellation: CancellationToken,
        on_update: ToolUpdateCallback,
    ) -> AgentToolResult:
        if cancellation.cancelled:
            raise WorkspaceToolError("aborted", cancellation.reason)
        cwd = path_policy.resolve(arguments["cwd"], must_exist=True)
        remaining_update_bytes = output_policy.max_bytes
        remaining_update_lines = output_policy.max_lines

        def update(stream_name: str, value: str) -> None:
            nonlocal remaining_update_bytes, remaining_update_lines
            if remaining_update_bytes <= 0 or remaining_update_lines <= 0:
                return
            update_policy = OutputPolicy(
                max_lines=remaining_update_lines,
                max_bytes=min(4_096, remaining_update_bytes),
            )
            preview, truncated = truncate_text(value, update_policy)
            if not preview:
                return
            remaining_update_bytes -= len(preview.encode("utf-8"))
            remaining_update_lines -= max(1, len(preview.splitlines()))
            on_update(
                AgentToolResult(
                    content=[{"type": "text", "text": preview}],
                    details={
                        "stream": stream_name,
                        "phase": "running",
                        "truncated": truncated,
                    },
                )
            )

        result = await runner.run(
            arguments["command"],
            cwd=cwd,
            timeout_seconds=arguments["timeout"],
            cancellation=cancellation,
            on_chunk=update,
        )
        if result.exit_code != 0:
            raise WorkspaceToolError(
                "non_zero_exit",
                f"Shell 命令退出码为 {result.exit_code}",
                details=_shell_details(result),
            )
        text_parts = []
        if result.stdout.text:
            text_parts.append(result.stdout.text)
        if result.stderr.text:
            text_parts.append("[stderr]\n" + result.stderr.text)
        return AgentToolResult(
            content=[{"type": "text", "text": "\n".join(text_parts)}],
            details=_shell_details(result),
        )

    return AgentTool(
        name="shell",
        label="受信本机 Shell",
        description=(
            "在工作区 cwd 运行受信本机命令；这不是安全沙箱，可能访问网络和"
            "工作区外资源，每次执行都必须经过 Approval"
        ),
        execute=execute,
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number", "minimum": 0.01},
                "cwd": {"type": "string", "default": "."},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        validate_args=validate,
        execution_mode="exclusive",
        replay_policy="never",
        requires_approval=True,
        implementation_version="1",
        security_policy_version=services.security_version("trusted-local-shell-v2"),
    )


def _shell_details(result: ProcessRunResult) -> dict[str, Any]:
    return {
        "exitCode": result.exit_code,
        "stdout": result.stdout.text,
        "stderr": result.stderr.text,
        "stdoutTruncated": result.stdout.truncated,
        "stderrTruncated": result.stderr.truncated,
        "stdoutBytes": result.stdout.total_bytes,
        "stderrBytes": result.stderr.total_bytes,
        "stdoutSpillPath": result.stdout.spill_path,
        "stderrSpillPath": result.stderr.spill_path,
        "stdoutSpillComplete": result.stdout.spill_complete,
        "stderrSpillComplete": result.stderr.spill_complete,
        "stdoutDroppedBytes": result.stdout.dropped_bytes,
        "stderrDroppedBytes": result.stderr.dropped_bytes,
        "sandboxed": False,
        "trustedLocalExecution": True,
    }
