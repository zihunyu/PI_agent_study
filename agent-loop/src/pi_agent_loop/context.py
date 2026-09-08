"""Bounded context assembly and encrypted, dispatch-time request evidence.

Snapshots are audit evidence, never authority to replay tools or Python callbacks.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4

from .cancellation import CancellationToken
from .retry.compaction import TokenAwareStructuredCompactor, estimate_message_tokens
from .session.journal import JournalPrincipal, SessionEventJournal, SessionEventSpec
from .types import AgentTool, Model


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def model_context_snapshot(context: dict[str, Any]) -> dict[str, Any]:
    """Capture model-visible tools without serializing handlers or API clients."""
    tools = []
    for tool in context.get("tools", []):
        if isinstance(tool, AgentTool):
            tools.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": copy.deepcopy(tool.parameters),
                }
            )
        elif isinstance(tool, dict):
            tools.append(copy.deepcopy(tool))
        else:
            raise TypeError("model context tool must be AgentTool or JSON object")
    return {
        "systemPrompt": context.get("systemPrompt", ""),
        "messages": copy.deepcopy(context.get("messages", [])),
        "tools": tools,
    }


class ContextBudgetExceeded(ValueError):
    """Required context cannot fit; no model request should be dispatched."""


@dataclass(frozen=True, slots=True)
class ContextBudget:
    context_window: int
    output_reserve: int = 4096
    safety_margin: int = 512
    keep_recent_messages: int = 8
    version: str = "1"

    def __post_init__(self) -> None:
        for name in ("context_window", "output_reserve", "keep_recent_messages"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.safety_margin) is not int or self.safety_margin < 0:
            raise ValueError("safety_margin must be a non-negative integer")
        if self.output_reserve + self.safety_margin >= self.context_window:
            raise ValueError(
                "context window must exceed output reserve and safety margin"
            )
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("context budget version must be non-empty")

    @property
    def fingerprint(self) -> str:
        return content_digest(asdict(self))

    def validate_prepared(self, context: dict[str, Any]) -> None:
        """Final size check after safety transformations; never rewrite a verdict."""
        snapshot = model_context_snapshot(context)
        fixed = estimate_message_tokens(
            {
                "role": "system",
                "content": canonical_json(
                    {"system": snapshot["systemPrompt"], "tools": snapshot["tools"]}
                ),
            }
        )
        total = (
            fixed
            + sum(estimate_message_tokens(item) for item in snapshot["messages"])
            + self.output_reserve
            + self.safety_margin
        )
        if total > self.context_window:
            raise ContextBudgetExceeded(
                "checked model input exceeds the context budget"
            )

    async def prepare(
        self, context: dict[str, Any], cancellation: CancellationToken
    ) -> dict[str, Any]:
        cancellation.throw_if_cancelled()
        snapshot = model_context_snapshot(context)
        # This is an intentionally conservative byte-based estimate, not an
        # assertion of a provider-specific tokenizer. The margin is configurable.
        fixed = estimate_message_tokens(
            {
                "role": "system",
                "content": canonical_json(
                    {"system": snapshot["systemPrompt"], "tools": snapshot["tools"]}
                ),
            }
        )
        available = (
            self.context_window - self.output_reserve - self.safety_margin - fixed
        )
        if available < 1:
            raise ContextBudgetExceeded(
                "system prompt and tool schemas exhaust the context budget"
            )
        messages = copy.deepcopy(snapshot["messages"])
        if sum(estimate_message_tokens(item) for item in messages) <= available:
            return {**context, "messages": messages}
        # Keep the original goal in full. It must not be replaced by a model's
        # paraphrase when the remainder of the conversation is compacted.
        for item in messages:
            if item.get("role") == "user":
                item["preserveInCompaction"] = True
                break
        compactor = TokenAwareStructuredCompactor(
            available, keep_recent_messages=self.keep_recent_messages
        )
        replacement = await compactor(messages)
        replacement.verify(messages)
        cancellation.throw_if_cancelled()
        if replacement.budget_exceeded:
            raise ContextBudgetExceeded(
                "required task facts and recent tool pairs exceed the context budget"
            )
        return {**context, "messages": copy.deepcopy(list(replacement.messages))}


class JournalModelRequestAudit:
    """Immutable request snapshots in the existing encrypted Session Journal."""

    def __init__(
        self,
        journal: SessionEventJournal,
        principal: JournalPrincipal,
        session_id: str,
        *,
        max_snapshot_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("audit session_id must be non-empty")
        if type(max_snapshot_bytes) is not int or max_snapshot_bytes < 1:
            raise ValueError("max_snapshot_bytes must be positive")
        self.journal = journal
        self.principal = principal
        self.session_id = session_id
        self.max_snapshot_bytes = max_snapshot_bytes

    async def __call__(
        self, model: Model, context: dict[str, Any], options: dict[str, Any]
    ) -> None:
        snapshot = model_context_snapshot(context)
        generation = {
            key: copy.deepcopy(options[key])
            for key in (
                "max_tokens",
                "max_completion_tokens",
                "temperature",
                "top_p",
                "stream_options",
                "tool_choice",
                "allowed_tool_names",
                "required_capabilities",
                "expected_tool_arguments",
            )
            if key in options
        }
        payload = {
            "snapshotId": uuid4().hex,
            "requestId": options.get("_audit_request_id"),
            "tenantId": self.principal.tenant_id,
            "sessionId": self.session_id,
            "phase": options.get("_execution_phase", "agent"),
            "policyVersion": options.get("_audit_policy_version"),
            "model": {"provider": model.provider, "id": model.id, "api": model.api},
            "context": snapshot,
            "generation": generation,
            **(
                {"wireBody": copy.deepcopy(options["_audit_wire_body"])}
                if "_audit_wire_body" in options
                else {}
            ),
        }
        encoded = canonical_json(payload).encode("utf-8")
        if len(encoded) > self.max_snapshot_bytes:
            raise ContextBudgetExceeded(
                "model request audit snapshot exceeds the storage limit"
            )
        payload["requestDigest"] = hashlib.sha256(encoded).hexdigest()
        await self.journal.append_events(
            self.principal,
            [
                SessionEventSpec(
                    journal_kind="audit",
                    event_type="model_request_snapshot",
                    session_id=self.session_id,
                    payload=payload,
                )
            ],
        )

    async def load(
        self, *, request_id: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        events = await self.journal.load_events(
            self.principal, session_id=self.session_id, journal_kind="audit"
        )
        result = []
        for event in events:
            if event.event_type != "model_request_snapshot":
                continue
            payload = copy.deepcopy(event.payload)
            if request_id is not None and payload.get("requestId") != request_id:
                continue
            digest = payload.pop("requestDigest", None)
            if digest != content_digest(payload):
                raise ValueError("model request snapshot integrity mismatch")
            payload["requestDigest"] = digest
            result.append(payload)
        return tuple(result)
