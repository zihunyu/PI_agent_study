"""具体项目可复用的业务实体状态机基础。"""

from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import math
from typing import Any

from .approval_receipt import (
    ApprovalReceipt,
    ApprovalReceiptVerifier,
    domain_action_hash,
)


class DomainTransitionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DomainTransition:
    event_type: str
    from_states: frozenset[str]
    to_state: str
    allowed_sources: frozenset[str]
    requires_approval: bool = False

    def __post_init__(self) -> None:
        if not self.event_type or not self.from_states or not self.to_state:
            raise ValueError("DomainTransition 必须提供事件、来源状态和目标状态")
        if not self.allowed_sources:
            raise ValueError("DomainTransition 必须声明可信事实来源")


@dataclass(frozen=True, slots=True)
class DomainEvent:
    entity_id: str
    type: str
    source: str
    expected_version: int
    data: dict[str, Any] = field(default_factory=dict)
    occurred_at: float | None = None
    approval_receipt: ApprovalReceipt | None = None


@dataclass(frozen=True, slots=True)
class EntityState:
    entity_id: str
    state: str
    version: int = 0
    last_event: str | None = None
    consumed_approval_receipts: tuple[str, ...] = ()


class DomainStateMachine:
    """根据可信 Domain Event 转换实体状态；不读取用户自然语言。"""

    def __init__(
        self,
        *,
        initial_state: str,
        transitions: list[DomainTransition],
        approval_receipt_verifier: ApprovalReceiptVerifier | None = None,
    ) -> None:
        if not initial_state:
            raise ValueError("initial_state 不能为空")
        self.initial_state = initial_state
        self._transitions = list(transitions)
        self._approval_receipt_verifier = approval_receipt_verifier
        if (
            any(transition.requires_approval for transition in transitions)
            and approval_receipt_verifier is None
        ):
            raise ValueError(
                "含审批转换的 DomainStateMachine 必须提供可信 ApprovalReceipt verifier"
            )
        seen: set[tuple[str, str]] = set()
        for transition in transitions:
            for source_state in transition.from_states:
                key = (source_state, transition.event_type)
                if key in seen:
                    raise ValueError(
                        f"状态 {source_state} 的事件 {transition.event_type} 存在重复转换"
                    )
                seen.add(key)

    def initial(self, entity_id: str) -> EntityState:
        if not entity_id:
            raise ValueError("entity_id 不能为空")
        return EntityState(entity_id=entity_id, state=self.initial_state)

    def apply(self, current: EntityState, event: DomainEvent) -> EntityState:
        if event.entity_id != current.entity_id:
            raise DomainTransitionError("entity_mismatch", "事件实体与当前实体不一致")
        if event.expected_version != current.version:
            raise DomainTransitionError(
                "stale_version",
                f"事件版本 {event.expected_version} 与当前版本 {current.version} 不一致",
            )
        transition = next(
            (
                item
                for item in self._transitions
                if item.event_type == event.type
                and current.state in item.from_states
            ),
            None,
        )
        if transition is None:
            raise DomainTransitionError(
                "invalid_transition",
                f"状态 {current.state} 不允许事件 {event.type}",
            )
        if event.source not in transition.allowed_sources:
            raise DomainTransitionError(
                "untrusted_source",
                f"事件来源 {event.source} 不能驱动 {event.type}",
            )
        consumed_receipts = current.consumed_approval_receipts
        if transition.requires_approval:
            receipt = self._validated_approval_receipt(current, event)
            consumed_receipts = (*consumed_receipts, receipt.receipt_id)
        return EntityState(
            entity_id=current.entity_id,
            state=transition.to_state,
            version=current.version + 1,
            last_event=event.type,
            consumed_approval_receipts=consumed_receipts,
        )

    def _validated_approval_receipt(
        self,
        current: EntityState,
        event: DomainEvent,
    ) -> ApprovalReceipt:
        receipt = event.approval_receipt
        if receipt is None:
            raise DomainTransitionError(
                "approval_required",
                f"事件 {event.type} 需要已消费的 ApprovalReceipt",
            )
        if receipt.receipt_id in current.consumed_approval_receipts:
            raise DomainTransitionError(
                "approval_already_consumed",
                f"审批凭证 {receipt.receipt_id} 已经驱动过 Domain 转换",
            )
        verifier = self._approval_receipt_verifier
        try:
            verified = verifier is not None and verifier(receipt) is True
        except Exception as error:
            raise DomainTransitionError(
                "approval_verification_failed",
                "审批凭证验证器执行失败",
            ) from error
        if not verified:
            raise DomainTransitionError(
                "approval_verification_failed",
                "审批凭证无法由可信 Approval 服务验证",
            )
        if receipt.entity_id != event.entity_id or receipt.event_type != event.type:
            raise DomainTransitionError(
                "approval_action_mismatch",
                "审批凭证绑定的实体或事件不匹配",
            )
        expected_hash = domain_action_hash(
            entity_id=event.entity_id,
            event_type=event.type,
            data=event.data,
        )
        if not hmac.compare_digest(receipt.action_hash.casefold(), expected_hash):
            raise DomainTransitionError(
                "approval_action_mismatch",
                "审批凭证绑定的 Action Hash 不匹配",
            )
        occurred_at = event.occurred_at
        if (
            isinstance(occurred_at, bool)
            or not isinstance(occurred_at, (int, float))
            or not math.isfinite(float(occurred_at))
        ):
            raise DomainTransitionError(
                "approval_event_time_required",
                "审批转换必须提供可信事件时间",
            )
        if occurred_at < receipt.consumed_at:
            raise DomainTransitionError(
                "approval_not_consumed",
                "Domain 事件发生在审批凭证消费之前",
            )
        if occurred_at > receipt.expires_at:
            raise DomainTransitionError(
                "approval_expired",
                "审批凭证已经过期",
            )
        return receipt
