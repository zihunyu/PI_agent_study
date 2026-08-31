"""项目业务实体状态机基础。"""

from .approval_receipt import (
    ApprovalReceipt,
    ApprovalReceiptVerifier,
    domain_action_hash,
)

from .state_machine import (
    DomainEvent,
    DomainStateMachine,
    DomainTransition,
    DomainTransitionError,
    EntityState,
)

__all__ = [
    "ApprovalReceipt",
    "ApprovalReceiptVerifier",
    "DomainEvent",
    "DomainStateMachine",
    "DomainTransition",
    "DomainTransitionError",
    "EntityState",
    "domain_action_hash",
]
