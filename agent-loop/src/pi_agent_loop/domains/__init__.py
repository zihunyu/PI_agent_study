"""项目业务实体状态机基础。"""

from .state_machine import (
    DomainEvent,
    DomainStateMachine,
    DomainTransition,
    DomainTransitionError,
    EntityState,
)

__all__ = [
    "DomainEvent",
    "DomainStateMachine",
    "DomainTransition",
    "DomainTransitionError",
    "EntityState",
]
