"""持久写操作状态机：可信身份、Approval、幂等和结果核对。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from ..approval.state_machine import (
    ApprovalError,
    ApprovalService,
    _replay_approval,
    action_digest,
    build_approval_request_event,
)
from ..durable_action import DurableActionEnvelope, strict_json_equal
from ..retry.errors import (
    DefinitelyNotCommittedToolError,
    OutcomeUnknownToolError,
)
from ..security import VerifiedIdentity
from ..session.operation_events import OperationEvent
from ..session.operation_state import replay_operation, replay_operation_with_specs
from ..session.operation_store import (
    ClaimLease,
    OperationEventSpec,
    OperationEventStore,
    OperationStoreConflictError,
    OperationStoreDeadlineExceeded,
    OperationStoreFencedClaimLostError,
    fenced_claim_resource_id,
    operation_last_sequence,
)

_MAX_CONFLICT_RETRIES = 20
_SCOPED_IDEMPOTENCY_HASH_VERSION = 2
_NONTERMINAL_RECONCILE_STATUSES = frozenset(
    {"pending", "outcome_unknown", "unknown", "not_found", "reconciling"}
)


class WriteOperationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class WriteOutcomeUnknownError(WriteOperationError):
    """外部副作用可能已经成功，但本地无法持久确认其结果。"""

    outcome_unknown = True

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)
        self.public_message = "写操作结果未知，需要人工核对"


@dataclass(frozen=True, slots=True)
class TrustedWriteAuthorization:
    """Immutable proof that another trusted boundary already consumed approval.

    This value is intentionally domain-neutral.  The producer is responsible for
    validating its native receipt (for example a Plan approval receipt) and
    binding ``action_hash`` to that exact action.  ``WriteOperationService`` then
    verifies freshness/consumption and persists the complete audit snapshot in
    the same ``write_prepared`` fact as the idempotency and entity-version data.
    """

    receipt_id: str
    action_hash: str
    verification_id: str
    consumed_at: float
    expires_at: float
    subject: dict[str, Any]

    def __post_init__(self) -> None:
        for name, value in (
            ("receipt_id", self.receipt_id),
            ("action_hash", self.action_hash),
            ("verification_id", self.verification_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Trusted Write Authorization {name} 不能为空")
        if len(self.action_hash) != 64 or any(
            character not in "0123456789abcdefABCDEF"
            for character in self.action_hash
        ):
            raise ValueError("Trusted Write Authorization action_hash 必须是 SHA-256")
        for name, timestamp_value in (
            ("consumed_at", self.consumed_at),
            ("expires_at", self.expires_at),
        ):
            if (
                isinstance(timestamp_value, bool)
                or not isinstance(timestamp_value, (int, float))
                or not math.isfinite(float(timestamp_value))
            ):
                raise ValueError(f"Trusted Write Authorization {name} 必须是有限时间戳")
        if self.consumed_at > self.expires_at:
            raise ValueError("Trusted Write Authorization 消费时间不能晚于过期时间")
        snapshot = copy.deepcopy(dict(self.subject))
        try:
            json.dumps(snapshot, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Trusted Write Authorization subject 必须是严格 JSON"
            ) from error
        object.__setattr__(self, "subject", snapshot)

    def to_dict(self) -> dict[str, Any]:
        return {
            "receiptId": self.receipt_id,
            "actionHash": self.action_hash,
            "verificationId": self.verification_id,
            "consumedAt": float(self.consumed_at),
            "expiresAt": float(self.expires_at),
            "subject": copy.deepcopy(self.subject),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TrustedWriteAuthorization":
        if set(value) != {
            "receiptId",
            "actionHash",
            "verificationId",
            "consumedAt",
            "expiresAt",
            "subject",
        }:
            raise ValueError("Trusted Write Authorization 字段无效")
        subject = value.get("subject")
        if not isinstance(subject, dict):
            raise ValueError("Trusted Write Authorization subject 必须是对象")
        consumed_at = value.get("consumedAt")
        expires_at = value.get("expiresAt")
        for name, timestamp_value in (
            ("consumedAt", consumed_at),
            ("expiresAt", expires_at),
        ):
            if (
                isinstance(timestamp_value, bool)
                or not isinstance(timestamp_value, (int, float))
                or not math.isfinite(float(timestamp_value))
            ):
                raise ValueError(
                    f"Trusted Write Authorization {name} 必须是有限时间戳"
                )
        assert isinstance(consumed_at, (int, float))
        assert isinstance(expires_at, (int, float))
        return cls(
            receipt_id=str(value.get("receiptId", "")),
            action_hash=str(value.get("actionHash", "")),
            verification_id=str(value.get("verificationId", "")),
            consumed_at=float(consumed_at),
            expires_at=float(expires_at),
            subject=subject,
        )


@dataclass(frozen=True, slots=True)
class WriteOperation:
    write_id: str
    session_id: str
    operation_id: str
    tool_name: str
    arguments: dict[str, Any]
    action_hash: str
    idempotency_key_hash: str
    requester_id: str
    state: str
    idempotency_hash_version: int = 1
    entity_id: str | None = None
    expected_entity_version: int | None = None
    business_preconditions: dict[str, Any] | None = None
    tool_call_id: str | None = None
    approval_id: str | None = None
    result: dict[str, Any] | None = None
    trusted_authorization: TrustedWriteAuthorization | None = None
    # Ephemeral ownership epoch supplied to a Reconciliation Adapter.  It is
    # deliberately not restored as durable business state; each claim gets a
    # fresh monotonically increasing generation.
    fencing_token: int | None = None
    fencing_scope: str | None = None
    reconcile_fencing_token: int | None = None
    reconcile_fencing_scope: str | None = None


@dataclass(frozen=True, slots=True)
class WriteExecutionContext:
    """Canonical durable write bindings exposed to a business CAS adapter.

    Values are reconstructed from the persisted ``write_prepared`` fact rather
    than from the caller that resumes an Approval.  Mutable JSON fields are
    copied so adapter-side mutation cannot rewrite the in-memory Write record.
    """

    write_id: str
    session_id: str
    operation_id: str
    tool_name: str
    arguments: dict[str, Any]
    idempotency_key: str
    actor: VerifiedIdentity
    action_hash: str
    requester_id: str
    tool_call_id: str | None = None
    approval_id: str | None = None
    entity_id: str | None = None
    expected_entity_version: int | None = None
    business_preconditions: dict[str, Any] | None = None
    # Approval/Worker Claim 的单调代际；业务适配器应把它用于下游 CAS/Fence。
    fencing_token: int | None = None
    fencing_scope: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", copy.deepcopy(self.arguments))
        object.__setattr__(
            self,
            "business_preconditions",
            copy.deepcopy(self.business_preconditions),
        )
        if self.fencing_token is not None and (
            isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or self.fencing_token < 1
        ):
            raise ValueError("Write fencing_token 必须是正整数或 None")
        if (self.fencing_token is None) != (self.fencing_scope is None):
            raise ValueError("Write fencing_token 与 fencing_scope 必须同时提供")
        if self.fencing_scope is not None and not self.fencing_scope.strip():
            raise ValueError("Write fencing_scope 不能为空")


WriteHandler = Callable[
    [dict[str, Any], str, VerifiedIdentity],
    Awaitable[dict[str, Any]],
]
WriteContextHandler = Callable[
    [WriteExecutionContext],
    Awaitable[dict[str, Any]],
]
ReconcileHandler = Callable[[WriteOperation], Awaitable[dict[str, Any]]]


class WriteOperationService:
    def __init__(
        self,
        store: OperationEventStore,
        approvals: ApprovalService,
        *,
        reconcile_claim_lease_seconds: float = 300,
        reconcile_claim_renew_interval_seconds: float | None = None,
    ) -> None:
        if reconcile_claim_lease_seconds <= 0:
            raise ValueError("Reconcile Claim Lease 必须大于 0")
        renew_interval = (
            reconcile_claim_renew_interval_seconds
            if reconcile_claim_renew_interval_seconds is not None
            else min(60.0, reconcile_claim_lease_seconds / 3)
        )
        if renew_interval <= 0 or renew_interval >= reconcile_claim_lease_seconds:
            raise ValueError("Reconcile Claim 续租间隔必须小于 Lease")
        self.store = store
        self.approvals = approvals
        self.reconcile_claim_lease_seconds = reconcile_claim_lease_seconds
        self.reconcile_claim_renew_interval_seconds = renew_interval

    async def prepare(
        self,
        *,
        session_id: str,
        operation_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        idempotency_key: str,
        requester: VerifiedIdentity,
        requires_approval: bool,
        required_approval_role: str = "approver",
        tool_call_id: str | None = None,
        entity_id: str | None = None,
        expected_entity_version: int | None = None,
        business_preconditions: dict[str, Any] | None = None,
        trusted_authorization: TrustedWriteAuthorization | None = None,
        authorization_action_hash: str | None = None,
        fenced_claim: ClaimLease | None = None,
        fenced_plan_id: str | None = None,
        fenced_step_id: str | None = None,
        fenced_claim_lease_seconds: float | None = None,
    ) -> WriteOperation:
        if fenced_claim_lease_seconds is not None and (
            isinstance(fenced_claim_lease_seconds, bool)
            or not isinstance(fenced_claim_lease_seconds, (int, float))
            or fenced_claim_lease_seconds <= 0
        ):
            raise ValueError("fenced_claim_lease_seconds 必须是正数或 None")
        claim_lease_seconds = (
            float(fenced_claim_lease_seconds)
            if fenced_claim_lease_seconds is not None
            else self.reconcile_claim_lease_seconds
        )
        fenced_entity_id: str | None = None
        if fenced_claim is not None:
            if fenced_claim.claim_type == "plan_execution":
                _validate_plan_authorization(
                    trusted_authorization,
                    plan_id=fenced_plan_id,
                    step_id=fenced_step_id,
                )
                fenced_entity_id = fenced_plan_id
            elif fenced_claim.claim_type == "conversation_session_writer":
                if fenced_plan_id is not None or fenced_step_id is not None:
                    raise WriteOperationError(
                        "write_fenced_claim_scope_conflict",
                        "Session Writer Claim 不能冒充 Plan Execution Claim",
                    )
            else:
                raise WriteOperationError(
                    "write_fenced_claim_type_invalid",
                    f"{fenced_claim.claim_type} Claim 不能 Prepare Write",
                )
        elif fenced_plan_id is not None or fenced_step_id is not None:
            raise WriteOperationError(
                "write_fenced_claim_required",
                "Plan Write Prepare 必须传递完整 Plan Execution Claim",
            )
        if requires_approval and trusted_authorization is not None:
            raise WriteOperationError(
                "duplicate_approval_boundary",
                "Write 不能同时创建 Approval 并接受外部已消费授权",
            )
        requester = self.approvals.assert_trusted_identity(
            requester,
            purpose=(
                "Write approval requester"
                if requires_approval
                else "Write requester"
            ),
        )
        if (trusted_authorization is None) != (authorization_action_hash is None):
            raise WriteOperationError(
                "trusted_authorization_incomplete",
                "Trusted Write Authorization 与授权 Action Hash 必须同时提供",
            )
        if trusted_authorization is not None:
            assert authorization_action_hash is not None
            if trusted_authorization.action_hash != authorization_action_hash:
                raise WriteOperationError(
                    "trusted_authorization_action_mismatch",
                    "Trusted Write Authorization 与执行 Action 不匹配",
                )
        if requires_approval and not self.store.supports_atomic_transactions:
            raise WriteOperationError(
                "approval_atomic_store_required",
                "需要 Approval 的 Write 必须使用原子事务 Store",
            )
        key_hash = hash_scoped_idempotency_key(
            idempotency_key,
            session_id=session_id,
            principal_id=requester.principal_id,
            tool_name=tool_name,
        )
        legacy_key_hash = _hash_secret(idempotency_key)
        _validate_write_preconditions(
            entity_id=entity_id,
            expected_entity_version=expected_entity_version,
            business_preconditions=business_preconditions,
        )
        canonical_arguments = copy.deepcopy(arguments)
        semantic_action = _write_semantic_action(
            tool_name=tool_name,
            arguments=canonical_arguments,
            entity_id=entity_id,
            expected_entity_version=expected_entity_version,
            business_preconditions=business_preconditions,
            authorization_action_hash=authorization_action_hash,
        )
        semantic_digest = action_digest(semantic_action)
        existing = await self._find_by_idempotency(
            (key_hash, legacy_key_hash),
            session_id=session_id,
            requester_id=requester.principal_id,
            tool_name=tool_name,
        )
        if existing is not None:
            return _same_idempotent_action(existing, semantic_digest)
        if trusted_authorization is not None:
            now = time.time()
            if now < trusted_authorization.consumed_at:
                raise WriteOperationError(
                    "trusted_authorization_not_yet_consumed",
                    "Trusted Write Authorization 消费时间晚于当前时间",
                )
            if now > trusted_authorization.expires_at:
                raise WriteOperationError(
                    "trusted_authorization_expired",
                    "Trusted Write Authorization 已过期",
                )

        write_id = str(uuid4())
        action = _durable_write_action(
            operation_id=operation_id,
            tool_call_id=tool_call_id,
            write_id=write_id,
            tool_name=tool_name,
            arguments=canonical_arguments,
            entity_id=entity_id,
            expected_entity_version=expected_entity_version,
            business_preconditions=business_preconditions,
            authorization_action_hash=authorization_action_hash,
        )
        digest = action_digest(action)
        specs: list[OperationEventSpec] = [
            (
                "write_prepared",
                {
                    "writeId": write_id,
                    "toolName": tool_name,
                    "arguments": copy.deepcopy(canonical_arguments),
                    "actionHash": digest,
                    "idempotencyKeyHash": key_hash,
                    "idempotencyHashVersion": _SCOPED_IDEMPOTENCY_HASH_VERSION,
                    "requesterId": requester.principal_id,
                    "requesterVerificationId": requester.verification_id,
                    "toolCallId": tool_call_id,
                    "entityId": entity_id,
                    "expectedEntityVersion": expected_entity_version,
                    "businessPreconditions": copy.deepcopy(
                        business_preconditions
                    ),
                    "trustedAuthorization": (
                        None
                        if trusted_authorization is None
                        else trusted_authorization.to_dict()
                    ),
                },
            )
        ]
        if requires_approval:
            approval_id, approval_data = build_approval_request_event(
                requester=requester,
                action=action,
                action_summary=f"执行写工具 {tool_name}",
                required_role=required_approval_role,
            )
            specs.extend(
                [
                    ("approval_requested", approval_data),
                    (
                        "write_waiting_approval",
                        {"writeId": write_id, "approvalId": approval_id},
                    ),
                ]
            )
        else:
            specs.append(
                (
                    "write_approved",
                    {"writeId": write_id, "approvalId": None},
                )
            )

        for _ in range(_MAX_CONFLICT_RETRIES):
            existing = await self._find_by_idempotency(
                (key_hash, legacy_key_hash),
                session_id=session_id,
                requester_id=requester.principal_id,
                tool_name=tool_name,
            )
            if existing is not None:
                return _same_idempotent_action(existing, semantic_digest)
            events = await self.store.load(
                session_id=session_id,
                operation_id=operation_id,
            )
            if not events:
                raise WriteOperationError(
                    "operation_not_found",
                    "Write 必须属于已持久化的 Operation",
                )
            replay_operation_with_specs(events, specs)
            try:
                if fenced_claim is None:
                    await self.store.append_batch(
                        session_id,
                        operation_id,
                        specs,
                        expected_last_sequence=operation_last_sequence(events),
                    )
                else:
                    await self.store.append_batch_if_fenced_claim(
                        session_id,
                        operation_id,
                        specs,
                        fenced_claim,
                        renew_lease_seconds=claim_lease_seconds,
                        expected_last_sequence=operation_last_sequence(events),
                        expected_claim_entity_id=fenced_entity_id,
                    )
            except OperationStoreFencedClaimLostError as error:
                raise WriteOperationError(
                    "write_fenced_claim_lost",
                    "Write Prepare Claim 已丢失，禁止持久化",
                ) from error
            except OperationStoreConflictError:
                continue
            return await self.get(write_id)
        raise WriteOperationError("write_conflict", "Write Prepare 并发冲突")

    async def execute(
        self,
        write_id: str,
        *,
        actor: VerifiedIdentity,
        idempotency_key: str,
        handler: WriteHandler | None = None,
        context_handler: WriteContextHandler | None = None,
        approval_resume_id: str | None = None,
        fencing_token: int | None = None,
        fenced_claim: ClaimLease | None = None,
        fenced_plan_id: str | None = None,
        fenced_step_id: str | None = None,
        fenced_claim_lease_seconds: float | None = None,
    ) -> WriteOperation:
        if (handler is None) == (context_handler is None):
            raise ValueError("handler 与 context_handler 必须且只能提供一个")
        if fencing_token is not None and (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token < 1
        ):
            raise ValueError("Write fencing_token 必须是正整数或 None")
        record = await self.get(write_id)
        actor = self.approvals.assert_trusted_identity(
            actor,
            purpose=(
                "Write approval consumer"
                if record.approval_id is not None
                else "Write actor"
            ),
        )
        if fenced_claim_lease_seconds is not None and (
            isinstance(fenced_claim_lease_seconds, bool)
            or not isinstance(fenced_claim_lease_seconds, (int, float))
            or fenced_claim_lease_seconds <= 0
        ):
            raise ValueError("fenced_claim_lease_seconds 必须是正数或 None")
        claim_lease_seconds = (
            float(fenced_claim_lease_seconds)
            if fenced_claim_lease_seconds is not None
            else self.reconcile_claim_lease_seconds
        )
        fenced_entity_id: str | None = None
        if fenced_claim is not None:
            if fencing_token != fenced_claim.fencing_token:
                raise WriteOperationError(
                    "write_fencing_token_mismatch",
                    "Write fencing_token 与 Fenced Claim Generation 不一致",
                )
            if fenced_claim.claim_type == "approval_resume":
                if approval_resume_id is None:
                    raise WriteOperationError(
                        "write_fenced_claim_scope_missing",
                        "Approval Resume Fenced Claim 必须绑定 Approval ID",
                    )
                if fenced_plan_id is not None or fenced_step_id is not None:
                    raise WriteOperationError(
                        "write_fenced_claim_scope_conflict",
                        "Approval Resume Claim 不能冒充 Plan Execution Claim",
                    )
                fenced_entity_id = approval_resume_id
            elif fenced_claim.claim_type == "plan_execution":
                if approval_resume_id is not None:
                    raise WriteOperationError(
                        "write_fenced_claim_scope_conflict",
                        "Plan Execution Claim 不能冒充 Approval Resume Claim",
                    )
                _validate_plan_write_claim(
                    record,
                    plan_id=fenced_plan_id,
                    step_id=fenced_step_id,
                )
                fenced_entity_id = fenced_plan_id
            elif fenced_claim.claim_type == "conversation_session_writer":
                if fenced_plan_id is not None or fenced_step_id is not None:
                    raise WriteOperationError(
                        "write_fenced_claim_scope_conflict",
                        "Session Writer Claim 不能冒充 Plan Execution Claim",
                    )
            else:
                raise WriteOperationError(
                    "write_fenced_claim_type_invalid",
                    f"{fenced_claim.claim_type} Claim 不能执行 Write",
                )
        elif approval_resume_id is not None and (
            fencing_token is not None or self.store.supports_cross_process_claims
        ):
            raise WriteOperationError(
                "write_fenced_claim_required",
                "跨 Worker Approval Resume 必须传递完整 Fenced Claim",
            )
        elif fenced_plan_id is not None or fenced_step_id is not None:
            raise WriteOperationError(
                "write_fenced_claim_required",
                "Plan Write 必须传递完整 Plan Execution Fenced Claim",
            )
        elif fencing_token is not None:
            raise WriteOperationError(
                "write_fenced_claim_required",
                "Write 不能只接受裸 fencing_token，必须传递完整 Fenced Claim",
            )
        if fenced_claim is not None and context_handler is None:
            raise WriteOperationError(
                "write_fenced_context_handler_required",
                "Fenced Write 必须使用能接收 scope+token 的 context_handler",
            )
        if (
            record.approval_id is not None
            and not self.store.supports_atomic_transactions
        ):
            raise WriteOperationError(
                "approval_atomic_store_required",
                "Approval Consume 与 Write Claim 必须使用原子事务 Store",
            )
        if not idempotency_key_matches(record, idempotency_key):
            raise WriteOperationError(
                "idempotency_key_mismatch",
                "Idempotency Key 不匹配",
            )

        claimed: WriteOperation | None = None
        for _ in range(_MAX_CONFLICT_RETRIES):
            events = await self.store.load(
                session_id=record.session_id,
                operation_id=record.operation_id,
            )
            current = _replay_write(events, write_id)
            if (
                approval_resume_id is not None
                and current.approval_id != approval_resume_id
            ):
                raise WriteOperationError(
                    "approval_resume_write_mismatch",
                    "Approval Resume 与 Write 绑定不一致",
                )
            if current.state == "succeeded":
                return current
            specs: list[OperationEventSpec] = []
            approval_deadline_ms: int | None = None
            if current.state == "waiting_approval":
                if current.approval_id is None:
                    raise WriteOperationError(
                        "approval_missing",
                        "写操作缺少 Approval ID",
                    )
                approval = _replay_approval(events, current.approval_id)
                if approval.state not in {"approved", "consumed"}:
                    raise ApprovalError(
                        "approval_not_approved",
                        "Approval 尚未批准或状态不可执行",
                    )
                if approval.action_hash != current.action_hash:
                    raise WriteOperationError(
                        "approval_action_mismatch",
                        "Approval 与 Write Action 不匹配",
                    )
                if approval.state == "approved":
                    approval_deadline_ms = approval.expires_at
                    specs.append(
                        (
                            "approval_consumed",
                            {
                                "approvalId": current.approval_id,
                                "consumerId": actor.principal_id,
                                "consumerVerificationId": actor.verification_id,
                            },
                        )
                    )
                if approval_resume_id is not None and not _resume_started(
                    events,
                    approval_resume_id,
                ):
                    specs.append(
                        (
                            "approval_resume_started",
                            {
                                "approvalId": approval_resume_id,
                                "consumerId": actor.principal_id,
                                "fencingToken": fencing_token,
                            },
                        )
                    )
                specs.append(
                    (
                        "write_approved",
                        {
                            "writeId": write_id,
                            "approvalId": current.approval_id,
                        },
                    )
                )
            elif current.state != "approved":
                raise WriteOperationError(
                    "write_not_executable",
                    f"写操作当前状态 {current.state} 不能执行",
                )

            specs.extend(_tool_dispatch_specs(events, current))
            specs.append(
                (
                    "write_submitting",
                    {
                        "writeId": write_id,
                        "actorId": actor.principal_id,
                        "actorVerificationId": actor.verification_id,
                    },
                )
            )
            replay_operation_with_specs(events, specs)
            try:
                if fenced_claim is None:
                    await self.store.append_batch(
                        current.session_id,
                        current.operation_id,
                        specs,
                        expected_last_sequence=operation_last_sequence(events),
                        deadline_ms=approval_deadline_ms,
                    )
                else:
                    await self.store.append_batch_if_fenced_claim(
                        current.session_id,
                        current.operation_id,
                        specs,
                        fenced_claim,
                        renew_lease_seconds=claim_lease_seconds,
                        expected_last_sequence=operation_last_sequence(events),
                        deadline_ms=approval_deadline_ms,
                        expected_claim_entity_id=fenced_entity_id,
                    )
            except OperationStoreDeadlineExceeded as error:
                raise ApprovalError(
                    "approval_expired",
                    "Approval 已过期，禁止执行写操作",
                ) from error
            except OperationStoreFencedClaimLostError as error:
                raise WriteOperationError(
                    "write_fenced_claim_lost",
                    "Write Fenced Claim 已丢失，禁止派发外部写操作",
                ) from error
            except OperationStoreConflictError:
                continue
            claimed = replace(current, state="submitting")
            break
        if claimed is None:
            raise WriteOperationError("write_conflict", "Write Claim 并发冲突")

        try:
            if context_handler is not None:
                result = await context_handler(
                    WriteExecutionContext(
                        write_id=claimed.write_id,
                        session_id=claimed.session_id,
                        operation_id=claimed.operation_id,
                        tool_name=claimed.tool_name,
                        arguments=claimed.arguments,
                        idempotency_key=idempotency_key,
                        actor=actor,
                        action_hash=claimed.action_hash,
                        requester_id=claimed.requester_id,
                        tool_call_id=claimed.tool_call_id,
                        approval_id=claimed.approval_id,
                        entity_id=claimed.entity_id,
                        expected_entity_version=claimed.expected_entity_version,
                        business_preconditions=claimed.business_preconditions,
                        fencing_token=fencing_token,
                        fencing_scope=(
                            fenced_claim.resource_id
                            if fenced_claim is not None
                            else None
                        ),
                    )
                )
            else:
                assert handler is not None
                result = await handler(claimed.arguments, idempotency_key, actor)
        except asyncio.CancelledError:
            # 取消可能发生在外部系统已接收请求之后，也可能落在本地成功事件
            # 提交期间。先屏蔽取消读取真实终态；仅仍为 Submitting 时标记未知。
            cleanup = asyncio.create_task(
                self._mark_outcome_unknown_if_submitting(
                    claimed,
                    reason="execution_cancelled",
                    fenced_claim=fenced_claim,
                    fenced_claim_lease_seconds=claim_lease_seconds,
                ),
                name=f"write-cancel-cleanup:{write_id}",
            )
            try:
                await asyncio.shield(cleanup)
            except BaseException:
                await asyncio.gather(cleanup, return_exceptions=True)
            raise
        except DefinitelyNotCommittedToolError as error:
            try:
                await self._finish_transition(
                    claimed,
                    required_state="submitting",
                    event_type="write_failed",
                    data={
                        "writeId": write_id,
                        "errorCode": error.code,
                        "definitelyNotCommitted": True,
                    },
                    fenced_claim=fenced_claim,
                    fenced_claim_lease_seconds=claim_lease_seconds,
                )
            except Exception as persistence_error:
                raise WriteOutcomeUnknownError(
                    "write_failure_persistence_unknown",
                    "写 Handler 报错，但失败事件未能可靠持久化",
                ) from persistence_error
            raise
        except Exception as error:
            unknown_data = _outcome_unknown_event_data(write_id, error)
            unknown_data.setdefault("reason", "handler_exception_after_dispatch")
            try:
                await self._finish_transition(
                    claimed,
                    required_state="submitting",
                    event_type="write_outcome_unknown",
                    data=unknown_data,
                    fenced_claim=fenced_claim,
                    fenced_claim_lease_seconds=claim_lease_seconds,
                )
            except Exception as persistence_error:
                raise WriteOutcomeUnknownError(
                    "write_outcome_unknown_persistence_failed",
                    "外部写操作结果不确定，且未知状态未能可靠持久化",
                ) from persistence_error
            if is_outcome_unknown_error(error):
                raise
            raise WriteOutcomeUnknownError(
                "write_handler_outcome_unknown",
                "写 Handler 已开始执行后发生异常，外部结果需要核对",
            ) from error

        # 外部 Handler 与成功事实落盘是两个不同的故障边界。Handler 返回后，
        # 副作用可能已经永久发生；此后任何异常都绝不能再被解释为业务失败。
        try:
            return await self._finish_transition(
                claimed,
                required_state="submitting",
                event_type="write_succeeded",
                data={"writeId": write_id, "result": result},
                fenced_claim=fenced_claim,
                fenced_claim_lease_seconds=claim_lease_seconds,
            )
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(
                self._mark_outcome_unknown_if_submitting(
                    claimed,
                    reason="success_persistence_cancelled",
                    fenced_claim=fenced_claim,
                    fenced_claim_lease_seconds=claim_lease_seconds,
                ),
                name=f"write-success-cancel-cleanup:{write_id}",
            )
            try:
                await asyncio.shield(cleanup)
            except BaseException:
                await asyncio.gather(cleanup, return_exceptions=True)
            raise
        except Exception as persistence_error:
            unknown = WriteOutcomeUnknownError(
                "write_success_persistence_unknown",
                "外部写操作已经返回成功，但成功事件未能可靠持久化",
            )
            try:
                await self._mark_outcome_unknown_if_submitting(
                    claimed,
                    reason="write_succeeded_persistence_failed",
                    fenced_claim=fenced_claim,
                    fenced_claim_lease_seconds=claim_lease_seconds,
                )
            except Exception as mark_error:
                unknown.add_note(
                    "尝试持久化 outcome_unknown 也失败，Write 必须继续按 "
                    f"submitting 处理：{type(mark_error).__name__}"
                )
            raise unknown from persistence_error

    async def _mark_outcome_unknown_if_submitting(
        self,
        claimed: WriteOperation,
        *,
        reason: str,
        fenced_claim: ClaimLease | None = None,
        fenced_claim_lease_seconds: float | None = None,
    ) -> None:
        current = await self.get(claimed.write_id)
        if current.state != "submitting":
            return
        await self._finish_transition(
            current,
            required_state="submitting",
            event_type="write_outcome_unknown",
            data={
                "writeId": current.write_id,
                "reason": reason,
            },
            fenced_claim=fenced_claim,
            fenced_claim_lease_seconds=fenced_claim_lease_seconds,
        )

    async def reconcile(
        self,
        write_id: str,
        handler: ReconcileHandler,
    ) -> WriteOperation:
        acquire_fenced = getattr(self.store, "acquire_fenced_claim", None)
        if not callable(acquire_fenced):
            raise WriteOperationError(
                "write_reconcile_fencing_required",
                "Write Reconciliation Store 必须支持 Fenced Claim",
            )
        record = await self.get(write_id)
        owner_token = str(uuid4())
        resource_id = fenced_claim_resource_id(
            "write_reconcile",
            session_id=record.session_id,
            operation_id=record.operation_id,
            entity_id=write_id,
        )
        lease = await acquire_fenced(
            "write_reconcile",
            resource_id,
            owner_token,
            lease_seconds=self.reconcile_claim_lease_seconds,
        )
        if lease is None:
            raise WriteOperationError(
                "write_reconcile_claimed",
                "Write Reconciliation 已被另一个 Worker Claim",
            )
        claim_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._renew_reconcile_claim(lease, claim_lost),
            name=f"write-reconcile-heartbeat:{write_id}",
        )
        try:
            if record.state == "outcome_unknown":
                claimed = await self._finish_transition(
                    record,
                    required_state="outcome_unknown",
                    event_type="write_reconciling",
                    data={
                        "writeId": write_id,
                        "fencingToken": lease.fencing_token,
                        "fencingScope": lease.resource_id,
                    },
                    fenced_claim=lease,
                )
            elif record.state == "reconciling":
                # 上一个 Worker 可能在核对调用中崩溃；Lease 获胜者可重入。
                claimed = await self._finish_transition(
                    record,
                    required_state="reconciling",
                    event_type="write_reconcile_reentered",
                    data={
                        "writeId": write_id,
                        "fencingToken": lease.fencing_token,
                        "fencingScope": lease.resource_id,
                    },
                    fenced_claim=lease,
                )
            else:
                raise WriteOperationError(
                    "write_not_uncertain",
                    "只有 outcome_unknown/reconciling 可以核对",
                )
            claimed = replace(
                claimed,
                fencing_token=lease.fencing_token,
                fencing_scope=lease.resource_id,
            )
            try:
                result = await handler(claimed)
            except Exception:
                await self._assert_reconcile_claim(
                    lease,
                    claim_lost,
                )
                await self._finish_transition(
                    claimed,
                    required_state="reconciling",
                    event_type="write_reconcile_failed",
                    data={
                        "writeId": write_id,
                        "reason": "reconciliation_handler_error",
                        "fencingToken": lease.fencing_token,
                        "fencingScope": lease.resource_id,
                    },
                    fenced_claim=lease,
                )
                raise
            await self._assert_reconcile_claim(
                lease,
                claim_lost,
            )
            if not isinstance(result, dict):
                result = {
                    "status": "unknown",
                    "reason": "reconciliation_result_not_an_object",
                }
            status_value = result.get("status")
            status = (
                status_value.strip().casefold()
                if isinstance(status_value, str)
                else "unknown"
            )
            if status == "succeeded":
                event_type = "write_succeeded"
            elif status == "failed":
                event_type = "write_failed"
            else:
                # A reconciliation response proves failure only when the
                # adapter explicitly says ``failed``.  Pending, not-found,
                # malformed and vendor-specific statuses all remain uncertain.
                if status not in _NONTERMINAL_RECONCILE_STATUSES:
                    result = {
                        **result,
                        "reportedStatus": status_value,
                        "status": "unknown",
                        "reason": "unrecognized_reconciliation_status",
                    }
                event_type = "write_reconcile_deferred"
            return await self._finish_transition(
                claimed,
                required_state="reconciling",
                event_type=event_type,
                data={
                    "writeId": write_id,
                    "result": result,
                    "fencingToken": lease.fencing_token,
                    "fencingScope": lease.resource_id,
                },
                fenced_claim=lease,
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            await self.store.release_fenced_claim(lease)

    async def _renew_reconcile_claim(
        self,
        lease: ClaimLease,
        claim_lost: asyncio.Event,
    ) -> None:
        while True:
            try:
                renewed = await self.store.renew_fenced_claim(
                    lease,
                    lease_seconds=self.reconcile_claim_lease_seconds,
                )
            except Exception:
                claim_lost.set()
                return
            if not renewed:
                claim_lost.set()
                return
            await asyncio.sleep(self.reconcile_claim_renew_interval_seconds)

    async def _assert_reconcile_claim(
        self,
        lease: ClaimLease,
        claim_lost: asyncio.Event,
    ) -> None:
        # 在提交持久事实前按“同一个 owner + 同一个 generation”续租。相比只读
        # verify，这既能抵抗繁忙事件循环造成的边界抖动，又无法让已过期或已经
        # 被新 generation 接管的旧 Worker 复活。
        held = False
        if not claim_lost.is_set():
            try:
                held = await self.store.renew_fenced_claim(
                    lease,
                    lease_seconds=self.reconcile_claim_lease_seconds,
                )
            except Exception:
                held = False
        if not held:
            claim_lost.set()
            raise WriteOperationError(
                "write_reconcile_claim_lost",
                "Write Reconciliation Lease 已丢失，禁止提交结果",
            )

    async def get(self, write_id: str) -> WriteOperation:
        events = await self.store.load()
        record = _replay_write(events, write_id)
        operation_events = [
            event
            for event in events
            if event.session_id == record.session_id
            and event.operation_id == record.operation_id
        ]
        replay_operation(operation_events)
        return record

    async def _find_by_idempotency(
        self,
        key_hashes: tuple[str, ...],
        *,
        session_id: str,
        requester_id: str,
        tool_name: str,
    ) -> WriteOperation | None:
        events = await self.store.load()
        write_ids = [
            str(event.data.get("writeId"))
            for event in events
            if event.type == "write_prepared"
            and event.data.get("idempotencyKeyHash") in key_hashes
            and event.session_id == session_id
            and event.data.get("requesterId") == requester_id
            and event.data.get("toolName") == tool_name
        ]
        return await self.get(write_ids[-1]) if write_ids else None

    async def _finish_transition(
        self,
        record: WriteOperation,
        *,
        required_state: str,
        event_type: str,
        data: dict[str, Any],
        fenced_claim: ClaimLease | None = None,
        fenced_claim_lease_seconds: float | None = None,
    ) -> WriteOperation:
        for _ in range(_MAX_CONFLICT_RETRIES):
            events = await self.store.load(
                session_id=record.session_id,
                operation_id=record.operation_id,
            )
            current = _replay_write(events, record.write_id)
            if event_type == "write_succeeded" and current.state == "succeeded":
                return current
            if current.state != required_state:
                raise WriteOperationError(
                    "invalid_write_transition",
                    f"Write 当前状态 {current.state} 不能执行 {event_type}",
                )
            specs = [(event_type, data)]
            replay_operation_with_specs(events, specs)
            try:
                if fenced_claim is None:
                    await self.store.append_batch(
                        current.session_id,
                        current.operation_id,
                        specs,
                        expected_last_sequence=operation_last_sequence(events),
                    )
                else:
                    await self.store.append_batch_if_fenced_claim(
                        current.session_id,
                        current.operation_id,
                        specs,
                        fenced_claim,
                        renew_lease_seconds=(
                            fenced_claim_lease_seconds
                            or self.reconcile_claim_lease_seconds
                        ),
                        expected_last_sequence=operation_last_sequence(events),
                        expected_claim_entity_id=(
                            _fenced_write_entity_id(current, fenced_claim)
                        ),
                    )
            except OperationStoreFencedClaimLostError as error:
                raise WriteOperationError(
                    "write_fenced_claim_lost",
                    "Write Fenced Claim 已丢失，禁止提交状态",
                ) from error
            except OperationStoreConflictError:
                continue
            return _replay_write(
                await self.store.load(
                    session_id=current.session_id,
                    operation_id=current.operation_id,
                ),
                current.write_id,
            )
        raise WriteOperationError("write_conflict", "Write 状态转换并发冲突")


def _tool_dispatch_specs(
    events: list[OperationEvent],
    write: WriteOperation,
) -> list[OperationEventSpec]:
    if write.tool_call_id is None:
        return []
    intent_exists = any(
        event.type == "tool_intent_recorded"
        and event.data.get("toolCallId") == write.tool_call_id
        and event.data.get("toolName") == write.tool_name
        and strict_json_equal(event.data.get("arguments"), write.arguments)
        for event in events
    )
    if not intent_exists:
        raise WriteOperationError(
            "tool_intent_missing",
            "带 Tool Call ID 的 Write 缺少匹配 Tool Intent，禁止执行",
        )
    dispatch_exists = any(
        event.type == "tool_dispatch_started"
        and event.data.get("toolCallId") == write.tool_call_id
        for event in events
    )
    if dispatch_exists:
        return []
    return [
        (
            "tool_dispatch_started",
            {"toolCallId": write.tool_call_id, "source": "write_claim"},
        )
    ]


def _validate_plan_write_claim(
    record: WriteOperation,
    *,
    plan_id: str | None,
    step_id: str | None,
) -> None:
    _validate_plan_authorization(
        record.trusted_authorization,
        plan_id=plan_id,
        step_id=step_id,
    )


def _validate_plan_authorization(
    authorization: TrustedWriteAuthorization | None,
    *,
    plan_id: str | None,
    step_id: str | None,
) -> None:
    if not plan_id or not step_id:
        raise WriteOperationError(
            "write_plan_claim_scope_missing",
            "Plan Execution Claim 必须绑定 Plan ID 和 Step ID",
        )
    if authorization is None:
        raise WriteOperationError(
            "write_plan_authorization_missing",
            "Plan Write 缺少持久化 Trusted Authorization",
        )
    subject = authorization.subject
    if (
        subject.get("source") != "plan_approval_receipt"
        or subject.get("planId") != plan_id
        or subject.get("stepId") != step_id
    ):
        raise WriteOperationError(
            "write_plan_claim_scope_mismatch",
            "Plan Execution Claim 与 Write 的持久授权不匹配",
        )


def _fenced_write_entity_id(
    record: WriteOperation,
    lease: ClaimLease,
) -> str | None:
    if lease.claim_type == "conversation_session_writer":
        return None
    if lease.claim_type == "approval_resume":
        if not record.approval_id:
            raise WriteOperationError(
                "write_fenced_claim_scope_missing",
                "Approval Resume Write 缺少 Approval ID",
            )
        return record.approval_id
    if lease.claim_type == "write_reconcile":
        return record.write_id
    if lease.claim_type == "plan_execution":
        authorization = record.trusted_authorization
        plan_id = (
            authorization.subject.get("planId")
            if authorization is not None
            else None
        )
        if not isinstance(plan_id, str) or not plan_id:
            raise WriteOperationError(
                "write_plan_authorization_missing",
                "Plan Write 缺少持久化 Plan ID",
            )
        return plan_id
    raise WriteOperationError(
        "write_fenced_claim_type_invalid",
        f"{lease.claim_type} Claim 不能提交 Write Event",
    )


def _resume_started(events: list[OperationEvent], approval_id: str) -> bool:
    return any(
        event.type == "approval_resume_started"
        and event.data.get("approvalId") == approval_id
        for event in events
    )


def _same_idempotent_action(
    existing: WriteOperation,
    semantic_digest: str,
) -> WriteOperation:
    existing_semantic = _write_semantic_action(
        tool_name=existing.tool_name,
        arguments=existing.arguments,
        entity_id=existing.entity_id,
        expected_entity_version=existing.expected_entity_version,
        business_preconditions=existing.business_preconditions,
        authorization_action_hash=(
            existing.trusted_authorization.action_hash
            if existing.trusted_authorization is not None
            else None
        ),
    )
    if action_digest(existing_semantic) != semantic_digest:
        raise WriteOperationError(
            "idempotency_conflict",
            "同一个 Idempotency Key 不能用于不同操作",
        )
    return existing


def _write_semantic_action(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    entity_id: str | None,
    expected_entity_version: int | None,
    business_preconditions: dict[str, Any] | None,
    authorization_action_hash: str | None,
) -> dict[str, Any]:
    """Stable business identity used only for idempotency-key comparison."""

    action: dict[str, Any] = {
        "tool": tool_name,
        "arguments": copy.deepcopy(arguments),
    }
    if entity_id is not None:
        action["entityId"] = entity_id
    if expected_entity_version is not None:
        action["expectedEntityVersion"] = expected_entity_version
    if business_preconditions is not None:
        action["businessPreconditions"] = copy.deepcopy(business_preconditions)
    if authorization_action_hash is not None:
        action["authorizationActionHash"] = authorization_action_hash
    return action


def _durable_write_action(
    *,
    operation_id: str,
    tool_call_id: str | None,
    write_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    entity_id: str | None,
    expected_entity_version: int | None,
    business_preconditions: dict[str, Any] | None,
    authorization_action_hash: str | None,
) -> dict[str, Any]:
    """Build the approval/audit hash input with all durable execution IDs."""

    if tool_call_id is not None:
        action = DurableActionEnvelope(
            operation_id=operation_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            write_id=write_id,
            entity_id=entity_id,
            expected_entity_version=expected_entity_version,
            business_preconditions=business_preconditions,
        ).to_dict()
    else:
        # Direct write APIs do not always have a model Tool Call, but the absent
        # binding is still explicit and is part of the digest.
        action = {
            "version": 1,
            "operationId": operation_id,
            "toolCallId": None,
            "toolName": tool_name,
            "arguments": copy.deepcopy(arguments),
            "writeId": write_id,
        }
        if entity_id is not None:
            action["entityId"] = entity_id
        if expected_entity_version is not None:
            action["expectedEntityVersion"] = expected_entity_version
        if business_preconditions is not None:
            action["businessPreconditions"] = copy.deepcopy(
                business_preconditions
            )
    if authorization_action_hash is not None:
        action["authorizationActionHash"] = authorization_action_hash
    return action


def _replay_write(
    events: list[OperationEvent],
    write_id: str,
) -> WriteOperation:
    record: WriteOperation | None = None
    for event in events:
        if event.data.get("writeId") != write_id:
            continue
        if event.type == "write_prepared":
            if record is not None:
                raise WriteOperationError("duplicate_write", "Write Prepared 重复")
            raw_expected_version = event.data.get("expectedEntityVersion")
            if raw_expected_version is not None and (
                isinstance(raw_expected_version, bool)
                or not isinstance(raw_expected_version, int)
                or raw_expected_version < 0
            ):
                raise WriteOperationError(
                    "invalid_expected_entity_version",
                    "Write Prepared expectedEntityVersion 必须是非负整数或 None",
                )
            record = WriteOperation(
                write_id=write_id,
                session_id=event.session_id,
                operation_id=event.operation_id,
                tool_name=str(event.data.get("toolName", "")),
                arguments=copy.deepcopy(event.data.get("arguments", {})),
                action_hash=str(event.data.get("actionHash", "")),
                idempotency_key_hash=str(
                    event.data.get("idempotencyKeyHash", "")
                ),
                requester_id=str(event.data.get("requesterId", "")),
                state="prepared",
                idempotency_hash_version=int(
                    event.data.get("idempotencyHashVersion", 1)
                ),
                entity_id=(
                    str(event.data.get("entityId"))
                    if event.data.get("entityId") is not None
                    else None
                ),
                expected_entity_version=(
                    raw_expected_version
                    if raw_expected_version is not None
                    else None
                ),
                business_preconditions=(
                    copy.deepcopy(event.data.get("businessPreconditions"))
                    if isinstance(event.data.get("businessPreconditions"), dict)
                    else None
                ),
                trusted_authorization=(
                    TrustedWriteAuthorization.from_dict(
                        copy.deepcopy(event.data["trustedAuthorization"])
                    )
                    if isinstance(event.data.get("trustedAuthorization"), dict)
                    else None
                ),
                tool_call_id=(
                    str(event.data.get("toolCallId"))
                    if event.data.get("toolCallId") is not None
                    else None
                ),
            )
        elif record is None:
            raise WriteOperationError(
                "write_event_without_prepare",
                "写事件缺少 Prepared",
            )
        elif event.type == "write_waiting_approval":
            if record.state != "prepared":
                raise WriteOperationError(
                    "invalid_write_transition",
                    "只有 Prepared 可以等待 Approval",
                )
            record = replace(
                record,
                state="waiting_approval",
                approval_id=str(event.data.get("approvalId", "")),
            )
        elif event.type == "write_approved":
            if record.state not in {"prepared", "waiting_approval"}:
                raise WriteOperationError(
                    "invalid_write_transition",
                    "不能进入 Approved",
                )
            record = replace(record, state="approved")
        elif event.type == "write_submitting":
            if record.state != "approved":
                raise WriteOperationError(
                    "invalid_write_transition",
                    "只有 Approved 可以提交",
                )
            record = replace(record, state="submitting")
        elif event.type == "write_succeeded":
            if record.state not in {"submitting", "reconciling"}:
                raise WriteOperationError(
                    "invalid_write_transition",
                    "当前状态不能成功",
                )
            if record.state == "reconciling":
                _validate_reconcile_terminal_fence(record, event.data)
            record = replace(
                record,
                state="succeeded",
                result=dict(event.data.get("result", {})),
            )
        elif event.type == "write_failed":
            approval_denied = (
                record.state == "waiting_approval"
                and event.data.get("reason") == "approval_not_granted"
            )
            if record.state not in {"submitting", "reconciling"} and not approval_denied:
                raise WriteOperationError(
                    "invalid_write_transition",
                    "当前状态不能失败",
                )
            if record.state == "reconciling":
                _validate_reconcile_terminal_fence(record, event.data)
            record = replace(
                record,
                state="failed",
                result=dict(event.data.get("result", {})),
            )
        elif event.type == "write_outcome_unknown":
            if record.state != "submitting":
                raise WriteOperationError(
                    "invalid_write_transition",
                    "只有 Submitting 可变成 Unknown",
                )
            record = replace(record, state="outcome_unknown")
        elif event.type == "write_reconciling":
            if record.state != "outcome_unknown":
                raise WriteOperationError(
                    "invalid_write_transition",
                    "只有 Unknown 可以核对",
                )
            fencing_token, fencing_scope = _required_event_fence_pair(event.data)
            _validate_canonical_reconcile_scope(record, fencing_scope)
            record = replace(
                record,
                state="reconciling",
                reconcile_fencing_token=fencing_token,
                reconcile_fencing_scope=fencing_scope,
            )
        elif event.type == "write_reconcile_reentered":
            if record.state != "reconciling":
                raise WriteOperationError(
                    "invalid_write_transition",
                    "只有 Reconciling 可以由新 Worker 重入",
                )
            fencing_token, fencing_scope = _required_event_fence_pair(event.data)
            _validate_canonical_reconcile_scope(record, fencing_scope)
            if fencing_scope != record.reconcile_fencing_scope:
                raise WriteOperationError(
                    "stale_write_fencing_scope",
                    "Write Reconciliation 重入 Fencing Scope 与当前 Claim 不一致",
                )
            if (
                record.reconcile_fencing_token is not None
                and fencing_token <= record.reconcile_fencing_token
            ):
                raise WriteOperationError(
                    "stale_write_fencing_token",
                    "Write Reconciliation 重入 Fencing Token 必须严格递增",
                )
            record = replace(
                record,
                reconcile_fencing_token=fencing_token,
                reconcile_fencing_scope=fencing_scope,
            )
        elif event.type == "write_reconcile_failed":
            if record.state != "reconciling":
                raise WriteOperationError(
                    "invalid_write_transition",
                    "只有 Reconciling 可以记录核对失败",
                )
            _validate_reconcile_terminal_fence(record, event.data)
            record = replace(record, state="outcome_unknown")
        elif event.type == "write_reconcile_deferred":
            if record.state != "reconciling":
                raise WriteOperationError(
                    "invalid_write_transition",
                    "只有 Reconciling 可以延后核对",
                )
            _validate_reconcile_terminal_fence(record, event.data)
            record = replace(
                record,
                state="outcome_unknown",
                result=dict(event.data.get("result", {})),
            )
    if record is None:
        raise WriteOperationError(
            "write_not_found",
            f"Write 不存在：{write_id}",
        )
    return record


def hash_idempotency_key(value: str) -> str:
    if not value:
        raise ValueError("Idempotency Key 不能为空")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _optional_event_fencing_token(data: dict[str, Any]) -> int | None:
    value = data.get("fencingToken")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WriteOperationError(
            "invalid_write_fencing_token",
            "Write Event fencingToken 必须是正整数",
        )
    return value


def _required_event_fencing_token(data: dict[str, Any]) -> int:
    value = _optional_event_fencing_token(data)
    if value is None:
        raise WriteOperationError(
            "write_fencing_token_missing",
            "Write Reconciliation Event 缺少 fencingToken",
        )
    return value


def _optional_event_fencing_scope(data: dict[str, Any]) -> str | None:
    value = data.get("fencingScope")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WriteOperationError(
            "invalid_write_fencing_scope",
            "Write Event fencingScope 必须是非空字符串",
        )
    return value


def _required_event_fence_pair(data: dict[str, Any]) -> tuple[int, str]:
    token = _optional_event_fencing_token(data)
    scope = _optional_event_fencing_scope(data)
    if token is None and scope is None:
        raise WriteOperationError(
            "write_fencing_pair_missing",
            "Write Reconciliation Event 缺少 fencingToken+fencingScope",
        )
    if token is None or scope is None:
        raise WriteOperationError(
            "write_fencing_pair_incomplete",
            "Write Reconciliation Event 必须成对提供 fencingToken+fencingScope",
        )
    return token, scope


def _canonical_reconcile_fencing_scope(record: WriteOperation) -> str:
    return fenced_claim_resource_id(
        "write_reconcile",
        session_id=record.session_id,
        operation_id=record.operation_id,
        entity_id=record.write_id,
    )


def _validate_canonical_reconcile_scope(
    record: WriteOperation,
    scope: str,
) -> None:
    if scope != _canonical_reconcile_fencing_scope(record):
        raise WriteOperationError(
            "invalid_write_fencing_scope",
            "Write Reconciliation Fencing Scope 与持久化 Write 不一致",
        )


def _validate_reconcile_terminal_fence(
    record: WriteOperation,
    data: dict[str, Any],
) -> None:
    if (
        record.reconcile_fencing_token is None
        or record.reconcile_fencing_scope is None
    ):
        raise WriteOperationError(
            "write_fencing_pair_missing",
            "Write Reconciliation 状态缺少完整 Fencing Pair",
        )
    token, scope = _required_event_fence_pair(data)
    _validate_canonical_reconcile_scope(record, scope)
    if (
        token != record.reconcile_fencing_token
        or scope != record.reconcile_fencing_scope
    ):
        raise WriteOperationError(
            "stale_write_fencing_pair",
            "Write Reconciliation 终态 Fencing Pair 与当前 Claim 不一致",
        )


def _validate_write_preconditions(
    *,
    entity_id: str | None,
    expected_entity_version: int | None,
    business_preconditions: dict[str, Any] | None,
) -> None:
    if entity_id is not None and (not isinstance(entity_id, str) or not entity_id):
        raise ValueError("entity_id 必须是非空字符串或 None")
    if expected_entity_version is not None:
        if (
            isinstance(expected_entity_version, bool)
            or not isinstance(expected_entity_version, int)
            or expected_entity_version < 0
        ):
            raise ValueError("expected_entity_version 必须是非负整数或 None")
        if entity_id is None:
            raise ValueError("expected_entity_version 必须绑定 entity_id")
    if business_preconditions is not None:
        if not isinstance(business_preconditions, dict):
            raise TypeError("business_preconditions 必须是对象或 None")
        try:
            json.dumps(
                business_preconditions,
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("business_preconditions 必须是有效 JSON") from error


def hash_scoped_idempotency_key(
    value: str,
    *,
    session_id: str,
    principal_id: str,
    tool_name: str,
) -> str:
    """Hash an idempotency key inside one trusted execution scope.

    The raw key is never persisted.  Including Session, principal and Tool
    prevents one conversation/user/tool from discovering or blocking another
    scope merely by choosing the same caller-provided key.
    """

    if not value:
        raise ValueError("Idempotency Key 不能为空")
    if not session_id or not principal_id or not tool_name:
        raise ValueError("Idempotency Scope 必须包含 Session、Principal 和 Tool")
    canonical = json.dumps(
        {
            "version": _SCOPED_IDEMPOTENCY_HASH_VERSION,
            "sessionId": session_id,
            "principalId": principal_id,
            "toolName": tool_name,
            "key": value,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _idempotency_hash_for_record(
    record: WriteOperation,
    value: str,
) -> str:
    if record.idempotency_hash_version >= _SCOPED_IDEMPOTENCY_HASH_VERSION:
        return hash_scoped_idempotency_key(
            value,
            session_id=record.session_id,
            principal_id=record.requester_id,
            tool_name=record.tool_name,
        )
    return hash_idempotency_key(value)


def idempotency_key_matches(record: WriteOperation, value: str) -> bool:
    """Return whether a caller key matches this write without exposing it."""

    return record.idempotency_key_hash == _idempotency_hash_for_record(
        record,
        value,
    )


def is_outcome_unknown_error(error: BaseException) -> bool:
    """识别 Adapter 的通用“结果不确定”合同，不依赖任何业务异常类型。"""

    return isinstance(error, (OutcomeUnknownToolError, WriteOutcomeUnknownError)) or (
        getattr(error, "outcome_unknown", False) is True
    )


def _outcome_unknown_event_data(
    write_id: str,
    error: BaseException,
) -> dict[str, Any]:
    data: dict[str, Any] = {"writeId": write_id}
    for attribute, field_name in (
        ("operation_id", "operationId"),
        ("reconciliation_name", "reconciliationName"),
        ("code", "errorCode"),
    ):
        value = getattr(error, attribute, None)
        if isinstance(value, str) and value:
            data[field_name] = value
    return data


# 内部兼容别名。
_hash_secret = hash_idempotency_key
