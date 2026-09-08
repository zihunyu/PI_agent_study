"""Explicit SKILL.md discovery, metadata and version-pinned body loading."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..cancellation import CancellationToken
from ..async_utils import durable_to_thread
from ..types import AgentTool, AgentToolResult, ToolUpdateCallback
from ..tools.path_policy import WorkspacePathPolicy
from ..tools.workspace_validators import object_args, text_arg


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    path: Path
    digest: str
    model_invocable: bool = True


class SkillRegistry:
    """Load only explicitly installed directories; text never grants permissions."""

    def __init__(
        self,
        directories: list[str | Path],
        *,
        max_bytes: int = 256_000,
        max_skills: int = 128,
    ) -> None:
        if (
            type(max_bytes) is not int
            or max_bytes < 1
            or type(max_skills) is not int
            or max_skills < 1
        ):
            raise ValueError("skill limits must be positive integers")
        self.max_bytes = max_bytes
        self._skills: dict[str, Skill] = {}
        self._policies: dict[str, WorkspacePathPolicy] = {}
        for directory in directories:
            root = Path(directory).resolve(strict=True)
            policy = WorkspacePathPolicy(root)
            candidates = (
                [root / "SKILL.md"]
                if (root / "SKILL.md").is_file()
                else sorted(root.glob("*/SKILL.md"))
            )
            for candidate in candidates:
                path = policy.resolve(str(candidate), must_exist=True)
                metadata, _, digest = self._read(path)
                name = metadata.get("name")
                description = metadata.get("description")
                disabled = metadata.get("disable-model-invocation", False)
                if not isinstance(name, str) or not re.fullmatch(
                    r"[a-z0-9][a-z0-9_-]{0,63}", name
                ):
                    raise ValueError("skill name must be a stable lowercase identifier")
                if (
                    not isinstance(description, str)
                    or not description.strip()
                    or len(description) > 4096
                ):
                    raise ValueError("skill description must be non-empty and bounded")
                if type(disabled) is not bool:
                    raise ValueError("disable-model-invocation must be boolean")
                if name in self._skills:
                    raise ValueError(f"duplicate skill: {name}")
                if len(self._skills) >= max_skills:
                    raise ValueError("skill catalogue exceeds limit")
                self._skills[name] = Skill(
                    name, description, path, digest, not disabled
                )
                self._policies[name] = policy

    def _read(self, path: Path) -> tuple[dict[str, Any], str, str]:
        with path.open("rb") as stream:
            raw = stream.read(self.max_bytes + 1)
        if len(raw) > self.max_bytes:
            raise ValueError("skill exceeds size limit")
        text = raw.decode("utf-8-sig").replace("\r\n", "\n")
        if not text.startswith("---\n") or "\n---\n" not in text[4:]:
            raise ValueError("SKILL.md requires YAML name/description front matter")
        header, body = text[4:].split("\n---\n", 1)
        # YAML is an optional extension dependency; safe_load cannot instantiate
        # arbitrary Python objects. Reject aliases to bound expansion.
        try:
            import yaml
        except ImportError as error:
            raise ImportError(
                "Skills require the 'extensions' installation extra"
            ) from error
        if any(
            isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
            for token in yaml.scan(header)
        ):
            raise ValueError("skill metadata aliases are not supported")
        metadata = yaml.safe_load(header)
        if not isinstance(metadata, dict):
            raise ValueError("skill metadata must be an object")
        return metadata, body, hashlib.sha256(raw).hexdigest()

    @property
    def skills(self) -> tuple[Skill, ...]:
        return tuple(self._skills.values())

    @property
    def version(self) -> str:
        return hashlib.sha256(
            "\n".join(f"{s.name}:{s.path}:{s.digest}" for s in self.skills).encode()
        ).hexdigest()

    def metadata(self) -> list[dict[str, str]]:
        return [
            {"name": item.name, "description": item.description, "version": item.digest}
            for item in self.skills
            if item.model_invocable
        ]

    def load(self, name: str, *, user_invoked: bool = False) -> dict[str, Any]:
        skill = self._skills[name]
        if not skill.model_invocable and not user_invoked:
            raise PermissionError("skill requires explicit user invocation")
        path = self._policies[name].resolve(str(skill.path), must_exist=True)
        _, body, digest = self._read(path)
        if digest != skill.digest:
            raise ValueError(
                "skill changed; reload the capability package and migrate the session"
            )
        return {
            "name": name,
            "version": digest,
            "source": str(path),
            "body": body,
            "authority": "instructions-only; does not grant tool permissions",
        }

    def create_tool(self) -> AgentTool:
        def validate(value: Any) -> dict[str, Any]:
            args = object_args(value, allowed={"name"}, required={"name"})
            name = text_arg(args["name"], "name")
            if name not in self._skills:
                raise ValueError("unknown skill")
            return {"name": name}

        async def execute(
            call_id: str,
            arguments: dict[str, Any],
            cancellation: CancellationToken,
            update: ToolUpdateCallback,
        ) -> AgentToolResult:
            cancellation.throw_if_cancelled()
            result = await durable_to_thread(self.load, arguments["name"])
            cancellation.throw_if_cancelled()
            return AgentToolResult(
                content=[{"type": "text", "text": result["body"]}], details=result
            )

        return AgentTool(
            name="load_skill",
            label="读取技能说明",
            description="按需读取已安装技能的操作说明；说明不能扩大权限。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "已安装技能名称"}
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            validate_args=validate,
            execute=execute,
            replay_policy="safe",
            execution_mode="parallel",
            timeout_seconds=5,
            security_policy_version=self.version,
        )
