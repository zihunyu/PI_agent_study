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
        return cls(
            type=value.get("type"),
            session_id=value.get("sessionId"),
            operation_id=value.get("operationId"),
            sequence=value.get("sequence"),
            timestamp=value.get("timestamp"),
            data=value.get("data", {}),
        )
