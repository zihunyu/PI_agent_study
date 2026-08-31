"""Backend-neutral contract for durable, fenced plan execution.

The protocol intentionally describes *semantic guarantees*, not a database
brand.  An adapter may implement it with PostgreSQL, another transactional
database, or a test double, but it must not advertise a capability that its
storage topology cannot actually provide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..session.operation_store import ClaimLease
    from .store import DurablePlanRecord, PlanCompletionEnvelope
    from .types import MultiIntentPlan, PlanEvent


class DurablePlanStoreConfigurationError(ValueError):
    """A Plan Store cannot satisfy the requested execution topology."""


@dataclass(frozen=True, slots=True)
class DurablePlanStoreCapabilities:
    """Auditable guarantees supplied by one concrete Plan Store adapter.

    ``atomic_fenced_append`` means lease generation verification, optional
    lease renewal, stream-head CAS, and event append occur in one storage
    transaction.  A separate ``verify`` followed by an ``append`` is not this
    guarantee.

    ``supports_cross_process`` covers several OS processes sharing one backend.
    ``supports_multi_host`` additionally covers workers on different machines;
    a SQLite file must therefore never set it to ``True``.
    """

    backend_name: str
    atomic_fenced_append: bool
    supports_cross_process: bool
    supports_multi_host: bool

    def __post_init__(self) -> None:
        if not isinstance(self.backend_name, str) or not self.backend_name.strip():
            raise ValueError("Plan Store backend_name 不能为空")
        for name in (
            "atomic_fenced_append",
            "supports_cross_process",
            "supports_multi_host",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"Plan Store {name} 必须是布尔值")
        if self.supports_multi_host and not self.supports_cross_process:
            raise ValueError("多机 Plan Store 必须同时支持跨进程")


@runtime_checkable
class DurablePlanStore(Protocol):
    """Minimal persistent queue/event-store contract used by plans and workers.

    Implementations are scoped to exactly one trusted tenant/session pair.
    This prevents a caller from injecting a shared adapter for the wrong
    security boundary merely because its methods happen to match.
    """

    @property
    def tenant_id(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def capabilities(self) -> DurablePlanStoreCapabilities: ...

    async def initialize(
        self,
        plan: MultiIntentPlan,
        *,
        dispatchable: bool = True,
    ) -> DurablePlanRecord: ...

    async def mark_dispatchable(self, plan_id: str) -> DurablePlanRecord: ...

    async def load(self, plan_id: str) -> DurablePlanRecord: ...

    async def list_runnable_plans(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[DurablePlanRecord, ...]: ...

    async def list_completion_pending_plans(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[DurablePlanRecord, ...]: ...

    async def append_event(
        self,
        plan_id: str,
        event: PlanEvent,
        *,
        expected_last_sequence: int,
        run_id: str | None = None,
        lease: ClaimLease | None = None,
        lease_seconds: float | None = None,
        validated_current: DurablePlanRecord | None = None,
    ) -> DurablePlanRecord: ...

    def new_owner_token(self) -> str: ...

    async def acquire_execution(
        self,
        plan_id: str,
        owner_token: str,
        *,
        lease_seconds: float,
    ) -> ClaimLease | None: ...

    async def renew_execution(
        self,
        lease: ClaimLease,
        *,
        lease_seconds: float,
    ) -> bool: ...

    async def verify_execution(self, lease: ClaimLease) -> bool: ...

    async def release_execution_lease(self, lease: ClaimLease) -> None: ...

    async def acquire_completion(
        self,
        plan_id: str,
        owner_token: str,
        *,
        lease_seconds: float,
    ) -> ClaimLease | None: ...

    async def renew_completion(
        self,
        lease: ClaimLease,
        *,
        lease_seconds: float,
    ) -> bool: ...

    async def ack_completion(
        self,
        plan_id: str,
        lease: ClaimLease,
        *,
        lease_seconds: float,
        envelope: PlanCompletionEnvelope,
    ) -> DurablePlanRecord: ...

    async def release_completion_lease(self, lease: ClaimLease) -> None: ...


def validate_durable_plan_store(
    store: Any,
    *,
    tenant_id: str | None = None,
    session_id: str | None = None,
    require_cross_process: bool = False,
    require_multi_host: bool = False,
) -> DurablePlanStore:
    """Validate identity, protocol shape, and semantic capabilities.

    This check is deliberately strict and side-effect free.  It does not probe
    a production database by writing test records; the adapter remains
    responsible for truthfully implementing its declared contract.
    """

    if not isinstance(store, DurablePlanStore):
        raise DurablePlanStoreConfigurationError(
            "plan_store 未实现完整 DurablePlanStore 协议"
        )
    capabilities = store.capabilities
    if not isinstance(capabilities, DurablePlanStoreCapabilities):
        raise DurablePlanStoreConfigurationError(
            "plan_store.capabilities 必须是 DurablePlanStoreCapabilities"
        )
    actual_tenant = store.tenant_id
    actual_session = store.session_id
    for value, name in (
        (actual_tenant, "plan_store.tenant_id"),
        (actual_session, "plan_store.session_id"),
    ):
        if not isinstance(value, str) or not value.strip():
            raise DurablePlanStoreConfigurationError(f"{name} 不能为空")
    if tenant_id is not None and actual_tenant != tenant_id:
        raise DurablePlanStoreConfigurationError(
            "Plan Store tenant 与 Host 不匹配："
            f"expected={tenant_id}, actual={actual_tenant}"
        )
    if session_id is not None and actual_session != session_id:
        raise DurablePlanStoreConfigurationError(
            "Plan Store session 与 Host 不匹配："
            f"expected={session_id}, actual={actual_session}"
        )
    if not capabilities.atomic_fenced_append:
        raise DurablePlanStoreConfigurationError(
            "Durable Plan 必须使用支持原子 Fenced Append 的 Store"
        )
    if require_cross_process and not capabilities.supports_cross_process:
        raise DurablePlanStoreConfigurationError(
            "Durable Plan Worker 需要支持跨进程 Claim/CAS 的 Store"
        )
    if require_multi_host and not capabilities.supports_multi_host:
        raise DurablePlanStoreConfigurationError(
            "distributed_execution=True 需要 supports_multi_host=True "
            "的共享 Plan Store；本机 SQLite 不满足该条件"
        )
    return store


__all__ = [
    "DurablePlanStore",
    "DurablePlanStoreCapabilities",
    "DurablePlanStoreConfigurationError",
    "validate_durable_plan_store",
]
