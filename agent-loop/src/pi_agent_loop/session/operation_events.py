"""可恢复 Agent Operation 的持久事件。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class OperationEvent:
    type: str
    session_id: str
    operation_id: str
    sequence: int
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.type or not self.session_id or not self.operation_id:
            raise ValueError("OperationEvent 必须包含 type/session_id/operation_id")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("OperationEvent.sequence 必须是非负整数")
        if not isinstance(self.data, dict):
            raise ValueError("OperationEvent.data 必须是对象")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "sessionId": self.session_id,
            "operationId": self.operation_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "OperationEvent":
        if not isinstance(value, dict):
            raise ValueError("Operation Event 必须是对象")
        event_type = value.get("type")
        session_id = value.get("sessionId")
        operation_id = value.get("operationId")
        sequence = value.get("sequence")
        timestamp = value.get("timestamp")
        data = value.get("data", {})
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("Operation Event type 无效")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Operation Event sessionId 无效")
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("Operation Event operationId 无效")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValueError("Operation Event sequence 无效")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise ValueError("Operation Event timestamp 无效")
        if not isinstance(data, dict):
            raise ValueError("Operation Event data 无效")
        return cls(
            type=event_type,
            session_id=session_id,
            operation_id=operation_id,
            sequence=sequence,
            timestamp=timestamp,
            data=data,
        )
