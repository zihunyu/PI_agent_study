"""CAS + mutation lock + atomic publish 的 write/edit 工具。"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, TypeVar

from ..cancellation import CancellationToken
from ..types import AgentTool, AgentToolResult, ToolUpdateCallback
from .errors import WorkspaceToolError, WorkspaceToolPreconditionError
from .output import truncate_text
from .services import ToolServices
from .text_files import DecodedTextFile, decode_text_file
from .workspace_validators import object_args, text_arg


_MAX_WRITE_BYTES = 5 * 1024 * 1024
_T = TypeVar("_T")


def create_write_tool(services: ToolServices) -> AgentTool:
    _require_write_profile(services)

    def validate(value: Any) -> dict[str, Any]:
        args = object_args(
            value,
            allowed={"path", "content", "expectedVersion"},
            required={"path", "content"},
        )
        path = text_arg(args["path"], "path")
        content = text_arg(args["content"], "content", allow_empty=True)
        expected = args.get("expectedVersion")
        if expected is not None:
            expected = text_arg(expected, "expectedVersion")
        if len(content.encode("utf-8")) > _MAX_WRITE_BYTES:
            raise ValueError("write content 超过 5 MiB 限制")
        return {"path": path, "content": content, "expectedVersion": expected}

    async def execute(
        _call_id: str,
        arguments: dict[str, Any],
        cancellation: CancellationToken,
        _on_update: ToolUpdateCallback,
    ) -> AgentToolResult:
        raw_path = arguments["path"]
        try:
            initial = services.path_policy.resolve(raw_path, must_exist=False)
        except WorkspaceToolError as error:
            raise WorkspaceToolPreconditionError.from_workspace_error(error) from error
        async with services.mutation_queue.acquire(initial):
            try:
                _assert_precommit_not_cancelled(cancellation)
                path = services.path_policy.revalidate(
                    raw_path,
                    initial,
                    must_exist=False,
                )
                if not path.parent.is_dir():
                    raise WorkspaceToolError(
                        "not_found",
                        "目标父目录不存在；write 不会隐式创建目录",
                        path=str(path.parent),
                    )
                exists = path.exists()
                if exists and not path.is_file():
                    raise WorkspaceToolError(
                        "not_file", "覆盖目标不是普通文件", path=str(path)
                    )
                if exists:
                    await asyncio.to_thread(
                        services.observations.assert_current,
                        path,
                        arguments["expectedVersion"],
                        maximum_bytes=_MAX_WRITE_BYTES,
                    )
                elif arguments["expectedVersion"] is not None:
                    raise WorkspaceToolError(
                        "stale_observation",
                        "目标文件已不存在，不能使用旧 observation 创建",
                        path=str(path),
                    )
                payload = arguments["content"].encode("utf-8")
                _assert_precommit_not_cancelled(cancellation)
            except WorkspaceToolPreconditionError:
                raise
            except WorkspaceToolError as error:
                raise WorkspaceToolPreconditionError.from_workspace_error(
                    error
                ) from error
            await _complete_file_call(
                asyncio.to_thread(
                    services.atomic_writer.write,
                    path,
                    payload,
                    create_only=not exists,
                )
            )
            observation = services.observations.observe(path, payload)
            return AgentToolResult(
                content=[
                    {
                        "type": "text",
                        "text": f"已写入 {services.path_policy.relative(path)}",
                    }
                ],
                details={
                    "absolutePath": str(path),
                    "relativePath": services.path_policy.relative(path),
                    "created": not exists,
                    "bytesWritten": len(payload),
                    "observation": observation.version,
                    "atomic": True,
                },
            )

    return AgentTool(
        name="write",
        label="写入文件",
        description="在工作区创建文件；覆盖已有文件必须传入 read observation",
        execute=execute,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "expectedVersion": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        validate_args=validate,
        execution_mode="resource_locked",
        resolve_resource_keys=lambda arguments: _resource_key(
            services,
            arguments,
            must_exist=False,
        ),
        resource_access="write",
        replay_policy="never",
        requires_approval=True,
        implementation_version="3",
        security_policy_version=services.security_version("workspace-write-cas-v2"),
    )


def create_edit_tool(services: ToolServices) -> AgentTool:
    _require_write_profile(services)

    def validate(value: Any) -> dict[str, Any]:
        args = object_args(
            value,
            allowed={"path", "edits", "expectedVersion"},
            required={"path", "edits", "expectedVersion"},
        )
        if not isinstance(args["edits"], list) or not args["edits"]:
            raise ValueError("edits 必须是非空数组")
        edits: list[dict[str, str]] = []
        for index, raw in enumerate(args["edits"]):
            edit = object_args(
                raw,
                allowed={"oldText", "newText"},
                required={"oldText", "newText"},
            )
            edits.append(
                {
                    "oldText": text_arg(edit["oldText"], f"edits[{index}].oldText"),
                    "newText": text_arg(
                        edit["newText"],
                        f"edits[{index}].newText",
                        allow_empty=True,
                    ),
                }
            )
        return {
            "path": text_arg(args["path"], "path"),
            "edits": edits,
            "expectedVersion": text_arg(args["expectedVersion"], "expectedVersion"),
        }

    async def execute(
        _call_id: str,
        arguments: dict[str, Any],
        cancellation: CancellationToken,
        _on_update: ToolUpdateCallback,
    ) -> AgentToolResult:
        raw_path = arguments["path"]
        try:
            initial = services.path_policy.resolve(raw_path, must_exist=True)
        except WorkspaceToolError as error:
            raise WorkspaceToolPreconditionError.from_workspace_error(error) from error
        async with services.mutation_queue.acquire(initial):
            try:
                _assert_precommit_not_cancelled(cancellation)
                path = services.path_policy.revalidate(
                    raw_path,
                    initial,
                    must_exist=True,
                )
                if not path.is_file():
                    raise WorkspaceToolError(
                        "not_file", "编辑目标不是普通文件", path=str(path)
                    )
                original_bytes = await asyncio.to_thread(
                    services.observations.assert_current,
                    path,
                    arguments["expectedVersion"],
                    maximum_bytes=_MAX_WRITE_BYTES,
                )
                original = decode_text_file(original_bytes, path=str(path))
                updated_text = _apply_exact_edits(
                    original.text,
                    arguments["edits"],
                    path,
                )
                updated = DecodedTextFile(updated_text, original.had_bom)
                payload = updated.encode()
                if len(payload) > _MAX_WRITE_BYTES:
                    raise WorkspaceToolError(
                        "invalid_args",
                        "编辑后的文件超过 5 MiB 限制",
                        path=str(path),
                    )
                _assert_precommit_not_cancelled(cancellation)
            except WorkspaceToolPreconditionError:
                raise
            except WorkspaceToolError as error:
                raise WorkspaceToolPreconditionError.from_workspace_error(
                    error
                ) from error
            await _complete_file_call(
                asyncio.to_thread(
                    services.atomic_writer.write,
                    path,
                    payload,
                    create_only=False,
                )
            )
            observation = services.observations.observe(path, payload)
            diff = "".join(
                difflib.unified_diff(
                    original.text.splitlines(keepends=True),
                    updated_text.splitlines(keepends=True),
                    fromfile=services.path_policy.relative(path),
                    tofile=services.path_policy.relative(path),
                )
            )
            diff, diff_truncated = truncate_text(diff, services.output_policy)
            if diff_truncated:
                diff += "\n[diff truncated]"
            return AgentToolResult(
                content=[{"type": "text", "text": diff or "文件已修改"}],
                details={
                    "absolutePath": str(path),
                    "relativePath": services.path_policy.relative(path),
                    "observation": observation.version,
                    "previousObservation": arguments["expectedVersion"],
                    "editsApplied": len(arguments["edits"]),
                    "bytesWritten": len(payload),
                    "bomPreserved": original.had_bom,
                    "atomic": True,
                },
            )

    return AgentTool(
        name="edit",
        label="精确编辑文件",
        description="按唯一 exact match 编辑工作区文件，必须传入 read observation",
        execute=execute,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "oldText": {"type": "string"},
                            "newText": {"type": "string"},
                        },
                        "required": ["oldText", "newText"],
                        "additionalProperties": False,
                    },
                },
                "expectedVersion": {"type": "string"},
            },
            "required": ["path", "edits", "expectedVersion"],
            "additionalProperties": False,
        },
        validate_args=validate,
        execution_mode="resource_locked",
        resolve_resource_keys=lambda arguments: _resource_key(
            services,
            arguments,
            must_exist=True,
        ),
        resource_access="write",
        replay_policy="never",
        requires_approval=True,
        implementation_version="3",
        security_policy_version=services.security_version("workspace-edit-cas-v2"),
    )


def _apply_exact_edits(
    original: str,
    edits: list[dict[str, str]],
    path: Path,
) -> str:
    replacements: list[tuple[int, int, str]] = []
    for edit in edits:
        old = edit["oldText"]
        count = original.count(old)
        if count == 0:
            raise WorkspaceToolError("edit_not_found", "找不到 oldText", path=str(path))
        if count != 1:
            raise WorkspaceToolError(
                "edit_not_unique",
                "oldText 在文件中不是唯一匹配",
                path=str(path),
                details={"occurrences": count},
            )
        start = original.index(old)
        replacements.append((start, start + len(old), edit["newText"]))
    ordered = sorted(replacements)
    for previous, current in zip(ordered, ordered[1:]):
        if previous[1] > current[0]:
            raise WorkspaceToolError(
                "edit_overlap", "多个 edit 匹配范围重叠", path=str(path)
            )
    updated = original
    for start, end, replacement in reversed(ordered):
        updated = updated[:start] + replacement + updated[end:]
    if updated == original:
        raise WorkspaceToolError("no_change", "edit 没有产生内容变化", path=str(path))
    return updated


async def _complete_file_call(awaitable: Awaitable[_T]) -> _T:
    """任务取消时仍等待底层文件调用结束，保证 mutation lock 不提前释放。"""

    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def _require_write_profile(services: ToolServices) -> None:
    if services.security_profile not in {"workspace-write", "full-access"}:
        raise ValueError("write/edit 只允许 workspace-write 或 full-access profile")


def _resource_key(
    services: ToolServices,
    arguments: dict[str, Any],
    *,
    must_exist: bool,
) -> str:
    raw = arguments.get("path")
    try:
        path = services.path_policy.resolve(str(raw), must_exist=must_exist)
    except WorkspaceToolError:
        # Resource resolver 不能把结构化路径错误提前降级成通用调度失败。
        # 真正 Handler 会再次 fail-closed；这个稳定哨兵只用于调度排队。
        digest = hashlib.sha256(str(raw).encode("utf-8")).hexdigest()
        return f"workspace-file-invalid:{digest}"
    return f"workspace-file:{services.path_policy.key(path)}"


def _assert_precommit_not_cancelled(cancellation: CancellationToken) -> None:
    if cancellation.cancelled:
        raise WorkspaceToolPreconditionError(
            "aborted",
            cancellation.reason,
        )
