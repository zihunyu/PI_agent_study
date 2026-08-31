"""只读工作区工具：read、list_dir、find、grep。"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path, PurePath
from typing import Any

from ..cancellation import CancellationToken
from ..types import AgentTool, AgentToolResult, ToolUpdateCallback
from .errors import WorkspaceToolError
from .output import truncate_text
from .services import ToolServices
from .text_files import decode_text_file, read_bytes_limited
from .workspace_validators import int_arg, object_args, text_arg


_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pi-agent-output",
    }
)
_MAX_READ_FILE_BYTES = 20 * 1024 * 1024
_MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
_MAX_GREP_LINE_CHARS = 4 * 1024
_MAX_PATTERN_CHARS = 256
_MAX_SCANNED_ENTRIES = 10_000


def create_read_tool(services: ToolServices) -> AgentTool:
    def validate(value: Any) -> dict[str, Any]:
        args = object_args(
            value,
            allowed={"path", "offset", "limit"},
            required={"path"},
        )
        return {
            "path": text_arg(args["path"], "path"),
            "offset": int_arg(
                args.get("offset", 1), "offset", minimum=1, maximum=10**9
            ),
            "limit": int_arg(
                args.get("limit", services.output_policy.max_lines),
                "limit",
                minimum=1,
                maximum=services.output_policy.max_lines,
            ),
        }

    async def execute(
        _call_id: str,
        arguments: dict[str, Any],
        cancellation: CancellationToken,
        _on_update: ToolUpdateCallback,
    ) -> AgentToolResult:
        cancellation.throw_if_cancelled()
        path = services.path_policy.resolve(arguments["path"], must_exist=True)
        if not path.is_file():
            raise WorkspaceToolError("not_file", "目标不是普通文件", path=str(path))
        data = await asyncio.to_thread(
            read_bytes_limited,
            path,
            _MAX_READ_FILE_BYTES,
        )
        cancellation.throw_if_cancelled()
        decoded = decode_text_file(data, path=str(path))
        observation = services.observations.observe(path, data)
        lines = decoded.text.splitlines(keepends=True)
        offset = arguments["offset"]
        if offset > max(1, len(lines)):
            raise WorkspaceToolError(
                "invalid_args",
                "offset 超出文件行数",
                path=str(path),
                details={"totalLines": len(lines)},
            )
        selected, consumed, partial = _bounded_lines(
            lines[offset - 1 : offset - 1 + arguments["limit"]],
            services.output_policy.max_bytes,
        )
        line_end = offset + consumed - 1 if consumed else offset - 1
        truncated = partial or line_end < len(lines)
        details = {
            "absolutePath": str(path),
            "relativePath": services.path_policy.relative(path),
            "lineStart": offset,
            "lineEnd": line_end,
            "totalLines": len(lines),
            "truncated": truncated,
            "partialLine": partial,
            "nextOffset": line_end + 1 if line_end < len(lines) else None,
            "observation": observation.version,
            "size": observation.size,
            "bom": decoded.had_bom,
        }
        return AgentToolResult(
            content=[{"type": "text", "text": selected}],
            details=details,
        )

    return AgentTool(
        name="read",
        label="读取文件",
        description="读取工作区内 UTF-8 文本并返回可用于 CAS 的 observation",
        execute=execute,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        validate_args=validate,
        execution_mode="parallel",
        replay_policy="safe",
        security_policy_version=services.security_version("workspace-read-v2"),
    )


def create_list_dir_tool(services: ToolServices) -> AgentTool:
    def validate(value: Any) -> dict[str, Any]:
        args = object_args(value, allowed={"path", "limit"})
        return {
            "path": text_arg(args.get("path", "."), "path"),
            "limit": int_arg(args.get("limit", 500), "limit", minimum=1, maximum=500),
        }

    async def execute(
        _call_id: str,
        arguments: dict[str, Any],
        cancellation: CancellationToken,
        _on_update: ToolUpdateCallback,
    ) -> AgentToolResult:
        cancellation.throw_if_cancelled()
        path = services.path_policy.resolve(arguments["path"], must_exist=True)
        if not path.is_dir():
            raise WorkspaceToolError("not_directory", "目标不是目录", path=str(path))

        def scan() -> tuple[list[dict[str, Any]], bool]:
            items: list[dict[str, Any]] = []
            scan_truncated = False
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.name.casefold() in services.path_policy.reserved_names:
                        continue
                    if len(items) >= _MAX_SCANNED_ENTRIES:
                        scan_truncated = True
                        break
                    is_directory = entry.is_dir(follow_symlinks=False)
                    relative = services.path_policy.relative(Path(entry.path))
                    items.append(
                        {
                            "name": entry.name + ("/" if is_directory else ""),
                            "path": relative,
                            "type": (
                                "symlink"
                                if entry.is_symlink()
                                else "directory"
                                if is_directory
                                else "file"
                            ),
                        }
                    )
            return (
                sorted(items, key=lambda item: str(item["name"]).casefold()),
                scan_truncated,
            )

        items, scan_truncated = await asyncio.to_thread(scan)
        cancellation.throw_if_cancelled()
        limit = arguments["limit"]
        shown: list[dict[str, Any]] = []
        used_bytes = 0
        for item in items[:limit]:
            encoded = (str(item["name"]) + "\n").encode("utf-8")
            if (
                len(shown) >= services.output_policy.max_lines
                or used_bytes + len(encoded) > services.output_policy.max_bytes
            ):
                break
            shown.append(item)
            used_bytes += len(encoded)
        text = "\n".join(str(item["name"]) for item in shown)
        return AgentToolResult(
            content=[{"type": "text", "text": text}],
            details={
                "absolutePath": str(path),
                "relativePath": services.path_policy.relative(path),
                "entries": shown,
                "totalEntries": len(items),
                "truncated": scan_truncated or len(shown) < len(items),
            },
        )

    return AgentTool(
        name="list_dir",
        label="列出目录",
        description="列出工作区内目录的直接子项，不递归",
        execute=execute,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
        validate_args=validate,
        execution_mode="parallel",
        replay_policy="safe",
        security_policy_version=services.security_version("workspace-list-v2"),
    )


def create_find_tool(services: ToolServices) -> AgentTool:
    def validate(value: Any) -> dict[str, Any]:
        args = object_args(
            value,
            allowed={"pattern", "path", "limit"},
            required={"pattern"},
        )
        pattern = text_arg(args["pattern"], "pattern")
        if len(pattern) > _MAX_PATTERN_CHARS:
            raise ValueError(f"pattern 最多 {_MAX_PATTERN_CHARS} 个字符")
        return {
            "pattern": pattern,
            "path": text_arg(args.get("path", "."), "path"),
            "limit": int_arg(args.get("limit", 500), "limit", minimum=1, maximum=2_000),
        }

    async def execute(
        _call_id: str,
        arguments: dict[str, Any],
        cancellation: CancellationToken,
        _on_update: ToolUpdateCallback,
    ) -> AgentToolResult:
        root = services.path_policy.resolve(arguments["path"], must_exist=True)
        if not root.is_dir():
            raise WorkspaceToolError(
                "not_directory", "搜索根路径不是目录", path=str(root)
            )
        cancellation.throw_if_cancelled()
        matches, scan_truncated = await asyncio.to_thread(
            _find_paths,
            services,
            root,
            arguments["pattern"],
            arguments["limit"],
            cancellation,
        )
        cancellation.throw_if_cancelled()
        return AgentToolResult(
            content=[{"type": "text", "text": "\n".join(matches)}],
            details={
                "matches": matches,
                "count": len(matches),
                "truncated": scan_truncated,
            },
        )

    return AgentTool(
        name="find",
        label="查找文件",
        description="在工作区内按 glob 查找路径，跳过依赖和版本控制目录",
        execute=execute,
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        validate_args=validate,
        execution_mode="parallel",
        replay_policy="safe",
        security_policy_version=services.security_version("workspace-find-v2"),
    )


def create_grep_tool(services: ToolServices) -> AgentTool:
    def validate(value: Any) -> dict[str, Any]:
        args = object_args(
            value,
            allowed={
                "pattern",
                "path",
                "fileGlob",
                "ignoreCase",
                "literal",
                "contextLines",
                "limit",
            },
            required={"pattern"},
        )
        for name in ("ignoreCase", "literal"):
            if not isinstance(args.get(name, False), bool):
                raise ValueError(f"{name} 必须是布尔值")
        pattern = text_arg(args["pattern"], "pattern")
        if len(pattern) > _MAX_PATTERN_CHARS:
            raise ValueError(f"pattern 最多 {_MAX_PATTERN_CHARS} 个字符")
        literal = args.get("literal", True)
        if not literal:
            _validate_safe_regex(pattern)
        return {
            "pattern": pattern,
            "path": text_arg(args.get("path", "."), "path"),
            "fileGlob": text_arg(args.get("fileGlob", "*"), "fileGlob"),
            "ignoreCase": args.get("ignoreCase", False),
            "literal": literal,
            "contextLines": int_arg(
                args.get("contextLines", 0), "contextLines", minimum=0, maximum=20
            ),
            "limit": int_arg(args.get("limit", 200), "limit", minimum=1, maximum=2_000),
        }

    async def execute(
        _call_id: str,
        arguments: dict[str, Any],
        cancellation: CancellationToken,
        _on_update: ToolUpdateCallback,
    ) -> AgentToolResult:
        root = services.path_policy.resolve(arguments["path"], must_exist=True)
        if not root.is_dir():
            raise WorkspaceToolError(
                "not_directory", "搜索根路径不是目录", path=str(root)
            )
        flags = re.IGNORECASE if arguments["ignoreCase"] else 0
        expression = (
            re.escape(arguments["pattern"])
            if arguments["literal"]
            else arguments["pattern"]
        )
        try:
            matcher = re.compile(expression, flags)
        except re.error as error:
            raise WorkspaceToolError("invalid_args", "grep 正则表达式无效") from error
        cancellation.throw_if_cancelled()
        matches, skipped, search_truncated = await asyncio.to_thread(
            _grep_paths,
            services,
            root,
            matcher,
            arguments["fileGlob"],
            arguments["contextLines"],
            arguments["limit"],
            cancellation,
        )
        cancellation.throw_if_cancelled()
        text = "\n".join(item["display"] for item in matches)
        text, content_truncated = truncate_text(text, services.output_policy)
        return AgentToolResult(
            content=[{"type": "text", "text": text}],
            details={
                "matches": matches,
                "count": len(matches),
                "skippedFiles": skipped,
                "truncated": search_truncated or content_truncated,
            },
        )

    return AgentTool(
        name="grep",
        label="搜索文本",
        description="在工作区 UTF-8 文本中执行受限正则或文字搜索",
        execute=execute,
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "fileGlob": {"type": "string", "default": "*"},
                "ignoreCase": {"type": "boolean", "default": False},
                "literal": {"type": "boolean", "default": True},
                "contextLines": {"type": "integer", "minimum": 0, "maximum": 20},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        validate_args=validate,
        execution_mode="parallel",
        replay_policy="safe",
        security_policy_version=services.security_version("workspace-grep-v2"),
    )


def _bounded_lines(lines: list[str], maximum_bytes: int) -> tuple[str, int, bool]:
    selected: list[str] = []
    used = 0
    partial = False
    for line in lines:
        encoded = line.encode("utf-8")
        if used + len(encoded) <= maximum_bytes:
            selected.append(line)
            used += len(encoded)
            continue
        if not selected:
            clipped = encoded[:maximum_bytes]
            while clipped:
                try:
                    selected.append(clipped.decode("utf-8"))
                    break
                except UnicodeDecodeError:
                    clipped = clipped[:-1]
            partial = True
            return "".join(selected), 1, partial
        return "".join(selected), len(selected), True
    return "".join(selected), len(selected), partial


class _ScanLimitReached(Exception):
    pass


def _walk_files(
    services: ToolServices,
    root: Path,
    cancellation: CancellationToken,
) -> Iterator[Path]:
    scanned = 0
    for current, directories, filenames in os.walk(root, followlinks=False):
        cancellation.throw_if_cancelled()
        current_path = Path(current)
        safe_directories: list[str] = []
        for name in directories:
            if name in _IGNORED_DIRECTORIES:
                continue
            scanned += 1
            if scanned > _MAX_SCANNED_ENTRIES:
                raise _ScanLimitReached
            candidate = current_path / name
            try:
                services.path_policy.resolve(str(candidate), must_exist=True)
            except WorkspaceToolError:
                continue
            if candidate.is_symlink():
                continue
            safe_directories.append(name)
        directories[:] = safe_directories
        for filename in filenames:
            cancellation.throw_if_cancelled()
            scanned += 1
            if scanned > _MAX_SCANNED_ENTRIES:
                raise _ScanLimitReached
            candidate = current_path / filename
            try:
                resolved = services.path_policy.resolve(str(candidate), must_exist=True)
            except WorkspaceToolError:
                continue
            if resolved.is_file():
                yield resolved


def _glob_matches(relative: str, pattern: str) -> bool:
    return (
        fnmatch.fnmatch(relative, pattern)
        or PurePath(relative).match(pattern)
        or fnmatch.fnmatch(Path(relative).name, pattern)
    )


def _find_paths(
    services: ToolServices,
    root: Path,
    pattern: str,
    limit: int,
    cancellation: CancellationToken,
) -> tuple[list[str], bool]:
    matches: list[str] = []
    used_bytes = 0
    truncated = False
    try:
        for path in _walk_files(services, root, cancellation):
            relative = services.path_policy.relative(path)
            if not _glob_matches(relative, pattern):
                continue
            encoded = (relative + "\n").encode("utf-8")
            if (
                len(matches) >= limit
                or len(matches) >= services.output_policy.max_lines
                or used_bytes + len(encoded) > services.output_policy.max_bytes
            ):
                truncated = True
                break
            matches.append(relative)
            used_bytes += len(encoded)
    except _ScanLimitReached:
        truncated = True
    return sorted(matches, key=str.casefold), truncated


def _grep_paths(
    services: ToolServices,
    root: Path,
    matcher: re.Pattern[str],
    file_glob: str,
    context_lines: int,
    limit: int,
    cancellation: CancellationToken,
) -> tuple[list[dict[str, Any]], int, bool]:
    matches: list[dict[str, Any]] = []
    skipped = 0
    used_bytes = 0
    # Agent 可见 content 仍严格受 OutputPolicy 限制；结构化命中信息另设小型
    # 硬上限，避免极小 preview 配置把第一条可行动定位也完全抹掉。
    detail_budget = max(4 * services.output_policy.max_bytes, 4 * 1024)
    truncated = False
    try:
        for path in _walk_files(services, root, cancellation):
            relative = services.path_policy.relative(path)
            if not _glob_matches(relative, file_glob):
                continue
            try:
                data = read_bytes_limited(path, _MAX_SEARCH_FILE_BYTES)
                decoded = decode_text_file(data, path=str(path))
            except (OSError, WorkspaceToolError):
                skipped += 1
                continue
            lines = decoded.text.splitlines()
            for index, line in enumerate(lines):
                cancellation.throw_if_cancelled()
                searchable = line[:_MAX_GREP_LINE_CHARS]
                if matcher.search(searchable) is None:
                    continue
                start = max(0, index - context_lines)
                end = min(len(lines), index + context_lines + 1)
                display = f"{relative}:{index + 1}:{searchable}"
                candidate = {
                    "path": relative,
                    "line": index + 1,
                    "text": searchable,
                    "context": [
                        context[:_MAX_GREP_LINE_CHARS] for context in lines[start:end]
                    ],
                    "display": display,
                }
                candidate_bytes = len(
                    json.dumps(
                        candidate,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                if (
                    len(matches) >= limit
                    or len(matches) >= services.output_policy.max_lines
                    or used_bytes + candidate_bytes > detail_budget
                ):
                    return matches, skipped, True
                matches.append(candidate)
                used_bytes += candidate_bytes
    except _ScanLimitReached:
        truncated = True
    return matches, skipped, truncated


def _validate_safe_regex(pattern: str) -> None:
    """允许无分组的受限正则，排除 Python ``re`` 的常见指数回溯结构。"""

    escaped = False
    in_class = False
    for character in pattern:
        if escaped:
            if character.isdigit() or character in {"g", "k"}:
                raise ValueError("grep 正则不允许反向引用")
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "[":
            in_class = True
            continue
        if character == "]" and in_class:
            in_class = False
            continue
        if not in_class and character in {"(", ")"}:
            raise ValueError(
                "grep 安全正则不允许分组/环视；复杂搜索请拆分为 literal 查询"
            )
        if not in_class and character in {"*", "+", "?", "{", "}"}:
            raise ValueError("grep 安全正则不允许量词；复杂或重复搜索请使用 literal")
        if not in_class and character == "|":
            raise ValueError("grep 安全正则不允许分支；请拆分为多次查询")
