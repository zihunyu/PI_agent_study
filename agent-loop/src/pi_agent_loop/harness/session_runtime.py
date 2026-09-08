"""Session configuration identity and renewable single-writer lease."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from typing import Any
from uuid import uuid4

from ..session import ClaimLease, OperationEventStore
from ..routing.capabilities import CapabilityRegistry
from ..tool_contract import ToolSecurityContract
from ..types import AgentTool, Model


class SessionAlreadyOpenError(RuntimeError):
    """Another Host currently owns the product-session writer lease."""


class SessionWriterBusyError(SessionAlreadyOpenError):
    """A capable store has an unexpired writer; admission can be retried later."""


class SessionWriterLeaseLostError(RuntimeError):
    """The Host lost its session lease and must stop accepting prompts."""


def agent_configuration_hash(
    *,
    model: Model,
    system_prompt: str,
    tools: list[AgentTool],
    router: Any | None = None,
    capabilities: CapabilityRegistry | None = None,
    plan_policies: Mapping[str, Any] | None = None,
    plan_tool_bindings: Mapping[str, str] | None = None,
    plan_budget: Any | None = None,
    router_policy_version: str = "1",
    approval_policy_version: str = "1",
    plan_policy_version: str = "1",
    security_policy_version: str = "1",
    execution_policy_version: str | None = None,
) -> str:
    """Return a stable digest for inputs that change transcript semantics."""

    for name, version in (
        ("router_policy_version", router_policy_version),
        ("approval_policy_version", approval_policy_version),
        ("plan_policy_version", plan_policy_version),
        ("security_policy_version", security_policy_version),
    ):
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f"{name} 必须是非空字符串")

    value = {
        "model": {
            "id": model.id,
            "provider": model.provider,
            "api": model.api,
            "contextWindow": model.context_window,
            "maxTokens": model.max_tokens,
            "reasoning": model.reasoning,
        },
        "systemPrompt": system_prompt,
        "tools": [
            {
                "securityContractDigest": (
                    ToolSecurityContract.capture(tool).digest
                ),
                "name": tool.name,
                "description": tool.description,
                "parameters": _json_value(tool.parameters),
                "executionMode": tool.execution_mode,
                "timeoutSeconds": tool.timeout_seconds,
                "replayPolicy": tool.replay_policy,
                "requiresApproval": tool.requires_approval,
                "implementationVersion": tool.implementation_version,
                "securityPolicyVersion": tool.security_policy_version,
                "supportsResourceFencing": tool.supports_resource_fencing,
                "handlerMode": (
                    "context" if tool.execute_with_context is not None else "legacy"
                ),
                "priority": tool.priority,
                "resourceAccess": tool.resource_access,
                "lockTimeoutSeconds": tool.lock_timeout_seconds,
                "retryPolicy": _json_value(tool.retry_policy),
            }
            for tool in tools
        ],
        "router": _router_configuration(router, router_policy_version),
        "capabilities": _capability_configuration(capabilities),
        "approvalPolicy": {
            "version": approval_policy_version,
            "enforcement": "tool-capability-intent-union-v1",
        },
        "planPolicy": {
            "version": plan_policy_version,
            "policies": _plan_policy_configuration(plan_policies),
            "toolBindings": dict(sorted((plan_tool_bindings or {}).items())),
            "budget": _json_value(plan_budget),
        },
        "securityPolicyVersion": security_policy_version,
        **({"executionPolicyVersion": execution_policy_version} if execution_policy_version is not None else {}),
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _router_configuration(router: Any | None, policy_version: str) -> Any:
    if router is None:
        return None
    router_type = type(router)
    value: dict[str, Any] = {
        "type": f"{router_type.__module__}.{router_type.__qualname__}",
        "policyVersion": policy_version,
    }
    # HybridModelRouter 暴露的这些字段是纯策略配置；不读取 stream_fn、Token、
    # 运行计数等瞬态或敏感属性。Custom Router 通过 policyVersion 显式版本化。
    if hasattr(router, "config"):
        config = getattr(router, "config")
        if is_dataclass(config) or isinstance(config, Mapping):
            value["businessConfig"] = _json_value(config)
        elif config is not None:
            config_type = type(config)
            value["businessConfigType"] = (
                f"{config_type.__module__}.{config_type.__qualname__}"
            )
    for attribute, key in (
        ("confidence_threshold", "confidenceThreshold"),
        ("max_intents", "maxIntents"),
    ):
        if hasattr(router, attribute):
            value[key] = _json_value(getattr(router, attribute))
    return value


def _capability_configuration(
    capabilities: CapabilityRegistry | None,
) -> list[dict[str, Any]]:
    if capabilities is None:
        return []
    return [
        {
            "tool": entry.tool.name,
            "capabilities": sorted(entry.capabilities),
            "domain": entry.domain,
            "operation": entry.operation,
            "sideEffect": entry.side_effect,
            "effectiveSideEffect": entry.has_side_effect,
            "risk": entry.risk,
            "requiresApproval": entry.requires_approval,
            "priority": entry.priority,
        }
        for entry in sorted(
            capabilities.all_entries(),
            key=lambda item: item.tool.name,
        )
    ]


def _plan_policy_configuration(
    policies: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Serialize trusted Plan policies by their public, stable contract."""

    result: dict[str, Any] = {}
    for intent, policy in sorted((policies or {}).items()):
        serializer = getattr(policy, "to_dict", None)
        result[str(intent)] = (
            _json_value(serializer()) if callable(serializer) else _json_value(policy)
        )
    return result


class SessionWriterLease:
    """Renewable cross-process claim preventing divergent session histories."""

    claim_type = "conversation_session_writer"

    def __init__(
        self,
        store: OperationEventStore,
        session_id: str,
        *,
        lease_seconds: float,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("session_writer_lease_seconds 必须大于 0")
        self.store = store
        self.session_id = session_id
        self.lease_seconds = lease_seconds
        self.owner_token = uuid4().hex
        self.claim_lease: ClaimLease | None = None
        self._renew_task: asyncio.Task[None] | None = None
        self._closed = False
        self._lost_error: BaseException | None = None
        self._loss_callback: Callable[[], None] | None = None

    @classmethod
    async def acquire(
        cls,
        store: OperationEventStore,
        session_id: str,
        *,
        lease_seconds: float = 30,
    ) -> "SessionWriterLease":
        if not getattr(store, "supports_cross_process_claims", False):
            raise SessionAlreadyOpenError(
                "当前 Operation Store 不支持跨进程 Session Writer Lease；"
                "生产/多窗口写入请使用 journal、SQLite 或分布式 Store Adapter"
            )
        lease = cls(store, session_id, lease_seconds=lease_seconds)
        acquire_fenced = getattr(store, "acquire_fenced_claim", None)
        if not callable(acquire_fenced):
            raise SessionAlreadyOpenError(
                "Operation Store 不支持带 Generation 的 Fenced Claim；"
                "禁止以可复活的普通 Lease 打开 Durable Session"
            )
        acquired = await acquire_fenced(
            lease.claim_type,
            session_id,
            lease.owner_token,
            lease_seconds=lease_seconds,
        )
        if not acquired:
            raise SessionWriterBusyError(
                f"Session {session_id} 已被另一个 DurableAgentHost 打开"
            )
        lease.claim_lease = acquired
        lease._renew_task = asyncio.create_task(
            lease._renew_loop(),
            name=f"session-writer-lease:{session_id}",
        )
        return lease

    def assert_owned(self) -> None:
        if self._closed:
            raise SessionWriterLeaseLostError("Session Writer Lease 已关闭")
        if self._lost_error is not None:
            raise SessionWriterLeaseLostError(
                f"Session Writer Lease 已丢失：{self._lost_error}"
            ) from self._lost_error

    @property
    def fencing_token(self) -> int:
        self.assert_owned()
        if self.claim_lease is None:
            raise SessionWriterLeaseLostError("Session Fenced Claim 尚未建立")
        return self.claim_lease.fencing_token

    async def verify_owned(self) -> None:
        """按同一 generation 续租并验证，而不只相信本地 Heartbeat。"""

        self.assert_owned()
        lease = self.claim_lease
        if lease is None:
            self._mark_lost(RuntimeError("Session Fenced Claim 缺失"))
            self.assert_owned()
            raise AssertionError("unreachable")
        try:
            held = await self.store.renew_fenced_claim(
                lease,
                lease_seconds=self.lease_seconds,
            )
        except BaseException as error:
            self._mark_lost(error)
            self.assert_owned()
            raise AssertionError("unreachable")
        if not held:
            self._mark_lost(RuntimeError("Lease 已被其他 Owner 接管"))
            self.assert_owned()

    def set_loss_callback(self, callback: Callable[[], None]) -> None:
        self._loss_callback = callback
        if self._lost_error is not None:
            callback()

    def _mark_lost(self, error: BaseException) -> None:
        if self._lost_error is not None:
            return
        self._lost_error = error
        callback = self._loss_callback
        if callback is not None:
            try:
                callback()
            except BaseException:
                # The lease is already fail-closed; callback failures must not
                # keep the heartbeat alive or hide the original loss reason.
                pass

    async def _renew_loop(self) -> None:
        interval = max(0.05, min(self.lease_seconds / 3, 30))
        try:
            while True:
                await asyncio.sleep(interval)
                lease = self.claim_lease
                if lease is None:
                    self._mark_lost(RuntimeError("Session Fenced Claim 缺失"))
                    return
                renewed = await self.store.renew_fenced_claim(
                    lease,
                    lease_seconds=self.lease_seconds,
                )
                if not renewed:
                    self._mark_lost(RuntimeError("Lease 已被其他 Owner 接管"))
                    return
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self._mark_lost(error)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._renew_task
        self._renew_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        lease = self.claim_lease
        self.claim_lease = None
        if lease is not None:
            await self.store.release_fenced_claim(lease)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_json_value(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    return repr(value)


__all__ = [
    "SessionAlreadyOpenError",
    "SessionWriterBusyError",
    "SessionWriterLease",
    "SessionWriterLeaseLostError",
    "agent_configuration_hash",
]
