"""Immutable, encrypted task outputs with source evidence and verified export."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from typing import Any

from .cancellation import CancellationToken
from .context import content_digest
from .execution import ExecutionEnvironment
from .session.journal import JournalConflictError, JournalPrincipal, SessionEventJournal
from .session.records import JournalRecordStore
from .tools.workspace_validators import object_args, text_arg
from .types import AgentTool, AgentToolResult, ToolUpdateCallback


@dataclass(frozen=True, slots=True)
class Artifact:
    artifact_id: str
    name: str
    media_type: str
    data: bytes
    digest: str
    citations: tuple[str, ...]

    def metadata(self) -> dict[str, Any]:
        return {
            "artifactId": self.artifact_id,
            "name": self.name,
            "mediaType": self.media_type,
            "bytes": len(self.data),
            "digest": self.digest,
            "citations": list(self.citations),
        }


class ArtifactStore:
    """Artifacts are private immutable run evidence, not external business writes.

    Publishing them into a mutable workspace is a separate explicit operation.
    A model cannot supply a filesystem path, tenant or arbitrary export location.
    """

    def __init__(
        self,
        journal: SessionEventJournal,
        principal: JournalPrincipal,
        session_id: str,
        *,
        max_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        if type(max_bytes) is not int or not 0 < max_bytes <= 16 * 1024 * 1024:
            raise ValueError("invalid artifact size limit")
        self.records = JournalRecordStore(journal, principal, session_id, "artifacts")
        self.max_bytes = max_bytes

    async def put(
        self,
        name: str,
        data: bytes,
        *,
        media_type: str = "text/markdown",
        citations: tuple[str, ...] = (),
    ) -> Artifact:
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 160
            or any(char in name for char in "/\\\x00")
            or name in {".", ".."}
        ):
            raise ValueError("artifact name must be a display name, not a path")
        if not isinstance(data, bytes) or not data or len(data) > self.max_bytes:
            raise ValueError("artifact data must be non-empty bounded bytes")
        if not isinstance(media_type, str) or not re.fullmatch(
            r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", media_type
        ):
            raise ValueError("artifact media type is invalid")
        if (
            len(citations) > 128
            or any(
                not isinstance(item, str) or not item.strip() or len(item) > 512
                for item in citations
            )
            or len(set(citations)) != len(citations)
        ):
            raise ValueError(
                "artifact citations must be unique bounded source identifiers"
            )
        digest = hashlib.sha256(data).hexdigest()
        metadata = {
            "name": name,
            "mediaType": media_type,
            "digest": digest,
            "citations": list(citations),
        }
        artifact_id = content_digest(metadata)
        payload = {
            **metadata,
            "artifactId": artifact_id,
            "data": base64.b64encode(data).decode("ascii"),
        }
        try:
            await self.records.append(
                artifact_id, "artifact_created", payload, expected_version=-1
            )
        except JournalConflictError:
            existing = await self.get(artifact_id)
            if (
                existing.data != data
                or existing.name != name
                or existing.citations != citations
            ):
                raise ValueError("artifact identity conflict") from None
            return existing
        return Artifact(artifact_id, name, media_type, data, digest, tuple(citations))

    async def get(self, artifact_id: str) -> Artifact:
        events = await self.records.read(artifact_id)
        if len(events) != 1 or events[0].event_type != "artifact_created":
            raise KeyError("artifact not found in the current scope")
        raw = events[0].payload
        data = base64.b64decode(raw["data"], validate=True)
        digest = hashlib.sha256(data).hexdigest()
        metadata = {
            key: raw[key] for key in ("name", "mediaType", "digest", "citations")
        }
        if (
            digest != raw["digest"]
            or content_digest(metadata) != artifact_id
            or len(data) > self.max_bytes
        ):
            raise ValueError("artifact integrity mismatch")
        return Artifact(
            artifact_id,
            raw["name"],
            raw["mediaType"],
            data,
            digest,
            tuple(raw["citations"]),
        )

    async def list(self) -> tuple[Artifact, ...]:
        return tuple([await self.get(key) for key in await self.records.all()])

    async def export(
        self,
        artifact_id: str,
        environment: ExecutionEnvironment,
        path: str,
        cancellation: CancellationToken,
        *,
        expected_digest: str | None = None,
    ) -> str:
        artifact = await self.get(artifact_id)
        cancellation.throw_if_cancelled()
        written = await environment.write_file(
            path, artifact.data, cancellation, expected_digest=expected_digest
        )
        if written != artifact.digest:
            raise ValueError("artifact export digest mismatch")
        observed = await environment.read_file(
            path, cancellation, max_bytes=self.max_bytes
        )
        if hashlib.sha256(observed).hexdigest() != artifact.digest:
            raise ValueError("artifact changed after export")
        return written

    def create_tool(self) -> AgentTool:
        def validate(value: Any) -> dict[str, Any]:
            args = object_args(
                value,
                allowed={"name", "content", "media_type", "citations"},
                required={"name", "content"},
            )
            citations = args.get("citations", [])
            if not isinstance(citations, list) or any(
                not isinstance(item, str) for item in citations
            ):
                raise ValueError("citations must be an array of source identifiers")
            return {
                "name": text_arg(args["name"], "name"),
                "content": text_arg(args["content"], "content"),
                "media_type": text_arg(
                    args.get("media_type", "text/markdown"), "media_type"
                ),
                "citations": citations,
            }

        async def execute(
            call_id: str,
            args: dict[str, Any],
            token: CancellationToken,
            update: ToolUpdateCallback,
        ) -> AgentToolResult:
            token.throw_if_cancelled()
            artifact = await self.put(
                args["name"],
                args["content"].encode("utf-8"),
                media_type=args["media_type"],
                citations=tuple(args["citations"]),
            )
            return AgentToolResult(
                content=[
                    {"type": "text", "text": "Artifact stored: " + artifact.artifact_id}
                ],
                details=artifact.metadata(),
            )

        return AgentTool(
            name="create_artifact",
            label="保存任务成果",
            description="将非空报告保存为当前会话的不可变交付物，返回可核验 ID；不会覆盖工作区文件。",
            execute=execute,
            validate_args=validate,
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "成果显示名称"},
                    "content": {"type": "string", "description": "完整报告正文"},
                    "media_type": {
                        "type": "string",
                        "description": "MIME 类型，默认 text/markdown",
                    },
                    "citations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "报告引用的可解析资料标识",
                    },
                },
                "required": ["name", "content"],
                "additionalProperties": False,
            },
            execution_mode="parallel",
            replay_policy="safe",
            timeout_seconds=30,
            security_policy_version=content_digest(
                {
                    "tenant": self.records.principal.tenant_id,
                    "session": self.records.session_id,
                    "maxBytes": self.max_bytes,
                    "contract": "immutable-artifact-v1",
                }
            ),
        )
