"""检测崩溃时未结束的 Run，并追加挂起事件。"""

from __future__ import annotations

from uuid import uuid4

from ..runtime.events import RuntimeEvent
from ..runtime.states import RunState
from .replay import replay_runtime_events
from .operation_store import ClaimLease, OperationEventStore
from .store import RuntimeEventStore
from .store import (
    RuntimeStoreFencedClaimLostError,
    RuntimeStoreFencedAppendUnsupportedError,
)


class RuntimeRecoveryClaimError(RuntimeError):
    """Another worker owns recovery for this runtime stream."""


class RuntimeRecoveryManager:
    def __init__(
        self,
        store: RuntimeEventStore,
        *,
        claim_store: OperationEventStore | None = None,
        claim_resource_id: str | None = None,
        claim_lease_seconds: float = 30,
    ) -> None:
        if claim_store is not None and not claim_resource_id:
            raise ValueError("Runtime Recovery 使用 Claim 时必须提供 resource_id")
        if claim_lease_seconds <= 0:
            raise ValueError("claim_lease_seconds 必须大于 0")
        self.store = store
        self.claim_store = claim_store
        self.claim_resource_id = claim_resource_id
        self.claim_lease_seconds = claim_lease_seconds
        if claim_store is not None and not getattr(
            store,
            "supports_fenced_runtime_append",
            False,
        ):
            raise RuntimeRecoveryClaimError(
                "Runtime Store 无法把 Claim 校验与 Runtime Event 原子提交"
            )
        bound_session_id = getattr(store, "session_id", None)
        if (
            claim_store is not None
            and isinstance(bound_session_id, str)
            and claim_resource_id != bound_session_id
        ):
            raise ValueError("Runtime Recovery Claim 与 Runtime Session 不匹配")

    async def recover(self) -> RunState:
        lease: ClaimLease | None = None
        if self.claim_store is not None:
            if not getattr(self.claim_store, "supports_cross_process_claims", False):
                raise RuntimeRecoveryClaimError(
                    "Runtime Recovery 需要支持跨进程 Claim 的 Store"
                )
            acquire_fenced = getattr(self.claim_store, "acquire_fenced_claim", None)
            if not callable(acquire_fenced):
                raise RuntimeRecoveryClaimError(
                    "Runtime Recovery Store 缺少 Fenced Claim Generation"
                )
            owner_token = uuid4().hex
            lease = await acquire_fenced(
                "runtime_recovery",
                self.claim_resource_id or "",
                owner_token,
                lease_seconds=self.claim_lease_seconds,
            )
            if lease is None:
                raise RuntimeRecoveryClaimError("Runtime Recovery 已被其他 Worker 占用")
        try:
            return await self._recover_claimed(lease)
        finally:
            if self.claim_store is not None and lease is not None:
                await self.claim_store.release_fenced_claim(lease)

    async def _recover_claimed(self, lease: ClaimLease | None) -> RunState:
        events = await self.store.load()
        state = replay_runtime_events(events)
        if state.run_id is None or state.phase in {
            "idle",
            "completed",
            "failed",
            "cancelled",
            "suspended",
        }:
            return state
        interrupted = RuntimeEvent(
            type="run_interrupted",
            run_id=state.run_id,
            sequence=state.sequence + 1,
            data={"reason": "process_restart"},
        )
        # 先验证归约，再持久化，避免把非法转换写入日志。
        recovered = replay_runtime_events([*events, interrupted])
        try:
            if lease is None:
                # A stale CAS raises instead of appending a guessed terminal.
                await self.store.append_cas(
                    interrupted,
                    expected_last_sequence=state.sequence,
                )
            else:
                # Claim ownership, Runtime CAS, append and renewal are one
                # Store transaction.  A prior renew followed by a plain append
                # leaves a takeover window and is deliberately forbidden.
                await self.store.append_cas_if_fenced_claim(
                    interrupted,
                    lease,
                    renew_lease_seconds=self.claim_lease_seconds,
                    expected_last_sequence=state.sequence,
                )
        except (
            RuntimeStoreFencedClaimLostError,
            RuntimeStoreFencedAppendUnsupportedError,
        ) as error:
            raise RuntimeRecoveryClaimError(
                "Runtime Recovery Fencing Token 已失效，禁止提交迟到状态"
            ) from error
        return recovered
