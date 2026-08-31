"""状态机使用的稳定 Runtime Event。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias, cast

RuntimeEventType: TypeAlias = Literal[
    "run_started",
    "run_finished",
    "run_interrupted",
    "routing_started",
    "routing_finished",
    "turn_started",
    "turn_finished",
    "model_request_started",
    "model_response_finished",
    "model_retry_scheduled",
    "model_retry_attempt_started",
    "model_retry_finished",
    "context_compaction_started",
    "context_compaction_finished",
    "tool_started",
    "tool_dispatch_started",
    "tool_retry_scheduled",
    "tool_retry_attempt_started",
    "tool_retry_finished",
    "tool_finished",
    "approval_required",
    "approval_granted",
    "approval_rejected",
    "outcome_unknown",
    "reconciliation_started",
    "reconciliation_finished",
    "budget_exceeded",
]

_RUNTIME_EVENT_TYPES = frozenset(
    {
        "run_started",
        "run_finished",
        "run_interrupted",
        "routing_started",
        "routing_finished",
        "turn_started",
        "turn_finished",
        "model_request_started",
        "model_response_finished",
        "model_retry_scheduled",
        "model_retry_attempt_started",
        "model_retry_finished",
        "context_compaction_started",
        "context_compaction_finished",
        "tool_started",
        "tool_dispatch_started",
        "tool_retry_scheduled",
        "tool_retry_attempt_started",
        "tool_retry_finished",
        "tool_finished",
        "approval_required",
        "approval_granted",
        "approval_rejected",
        "outcome_unknown",
        "reconciliation_started",
        "reconciliation_finished",
        "budget_exceeded",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    type: RuntimeEventType
    run_id: str
    sequence: int
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("RuntimeEvent.run_id 不能为空")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("RuntimeEvent.sequence 必须是非负整数")
        if isinstance(self.timestamp, bool) or not isinstance(self.timestamp, int) or self.timestamp < 0:
            raise ValueError("RuntimeEvent.timestamp 必须是非负整数")
        if not isinstance(self.data, dict):
            raise ValueError("RuntimeEvent.data 必须是对象")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "runId": self.run_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RuntimeEvent":
        if not isinstance(value, dict):
            raise ValueError("Runtime Event 必须是对象")
        event_type = value.get("type")
        run_id = value.get("runId")
        sequence = value.get("sequence")
        timestamp = value.get("timestamp")
        data = value.get("data", {})
        if not isinstance(event_type, str) or event_type not in _RUNTIME_EVENT_TYPES:
            raise ValueError("Runtime Event type 无效")
        if not isinstance(run_id, str):
            raise ValueError("Runtime Event runId 无效")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValueError("Runtime Event sequence 无效")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise ValueError("Runtime Event timestamp 无效")
        if not isinstance(data, dict):
            raise ValueError("Runtime Event data 无效")
        return cls(
            type=cast(RuntimeEventType, event_type),
            run_id=run_id,
            sequence=sequence,
            timestamp=timestamp,
            data=data,
        )
