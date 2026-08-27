"""具体项目可复用的业务实体状态机基础。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    approved: bool = False


@dataclass(frozen=True, slots=True)
class EntityState:
    entity_id: str
    state: str
    version: int = 0
    last_event: str | None = None


class DomainStateMachine:
    """根据可信 Domain Event 转换实体状态；不读取用户自然语言。"""

    def __init__(
        self,
        *,
        initial_state: str,
        transitions: list[DomainTransition],
    ) -> None:
        if not initial_state:
            raise ValueError("initial_state 不能为空")
        self.initial_state = initial_state
        self._transitions = list(transitions)
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
        if transition.requires_approval and not event.approved:
            raise DomainTransitionError(
                "approval_required",
                f"事件 {event.type} 需要审批",
            )
        return EntityState(
            entity_id=current.entity_id,
            state=transition.to_state,
            version=current.version + 1,
            last_event=event.type,
        )
