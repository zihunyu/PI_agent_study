"""Immutable, serializable security contract for one registered Tool."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .types import AgentTool


@dataclass(frozen=True, slots=True)
class ToolSecurityContract:
    """Security-relevant Tool facts captured at a trusted registration boundary.

    Python callable identities are deliberately not serialized.  Deployments bind
    handler changes through ``implementation_version``; a live Runtime separately
    seals the exact callable objects so in-process replacement is also rejected.
    """

    name: str
    replay_policy: str
    requires_approval: bool
    implementation_version: str
    security_policy_version: str
    execution_mode: str | None
    resource_access: str
    supports_resource_fencing: bool
    handler_mode: str
    timeout_seconds: float | None
    lock_timeout_seconds: float | None
    priority: int
    retry_policy: tuple[Any, ...] | None

    @classmethod
    def capture(cls, tool: AgentTool) -> "ToolSecurityContract":
        if not isinstance(tool, AgentTool):
            raise TypeError("Tool Security Contract 只能从 AgentTool 创建")
        retry = tool.retry_policy
        retry_contract = (
            None
            if retry is None
            else (
                retry.max_retries,
                tuple(sorted(retry.retryable_codes)),
                retry.idempotent,
                retry.initial_delay_seconds,
                retry.max_delay_seconds,
                retry.jitter_ratio,
                retry.max_elapsed_seconds,
            )
        )
        return cls(
            name=tool.name,
            replay_policy=tool.replay_policy,
            requires_approval=tool.requires_approval,
            implementation_version=tool.implementation_version,
            security_policy_version=tool.security_policy_version,
            execution_mode=tool.execution_mode,
            resource_access=tool.resource_access,
            supports_resource_fencing=tool.supports_resource_fencing,
            handler_mode=(
                "context" if tool.execute_with_context is not None else "legacy"
            ),
            timeout_seconds=tool.timeout_seconds,
            lock_timeout_seconds=tool.lock_timeout_seconds,
            priority=tool.priority,
            retry_policy=retry_contract,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "replayPolicy": self.replay_policy,
            "requiresApproval": self.requires_approval,
            "implementationVersion": self.implementation_version,
            "securityPolicyVersion": self.security_policy_version,
            "executionMode": self.execution_mode,
            "resourceAccess": self.resource_access,
            "supportsResourceFencing": self.supports_resource_fencing,
            "handlerMode": self.handler_mode,
            "timeoutSeconds": self.timeout_seconds,
            "lockTimeoutSeconds": self.lock_timeout_seconds,
            "priority": self.priority,
            "retryPolicy": (
                None
                if self.retry_policy is None
                else {
                    "maxRetries": self.retry_policy[0],
                    "retryableCodes": list(self.retry_policy[1]),
                    "idempotent": self.retry_policy[2],
                    "initialDelaySeconds": self.retry_policy[3],
                    "maxDelaySeconds": self.retry_policy[4],
                    "jitterRatio": self.retry_policy[5],
                    "maxElapsedSeconds": self.retry_policy[6],
                }
            ),
        }

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def tool_callable_identity(tool: AgentTool) -> tuple[int | None, ...]:
    """Return the exact live callables sealed by a Runtime registration."""

    return (
        id(tool.execute) if tool.execute is not None else None,
        id(tool.execute_with_context)
        if tool.execute_with_context is not None
        else None,
        id(tool.validate_args),
        id(tool.prepare_arguments) if tool.prepare_arguments is not None else None,
        id(tool.resolve_resource_keys)
        if tool.resolve_resource_keys is not None
        else None,
        id(tool.resolve_tenant_id) if tool.resolve_tenant_id is not None else None,
    )


__all__ = ["ToolSecurityContract", "tool_callable_identity"]
