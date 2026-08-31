"""Domain transitions consume verifiable, action-bound approval receipts."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeAlias


@dataclass(frozen=True, slots=True)
class ApprovalReceipt:
    """Opaque proof issued and consumed by a trusted Approval service.

    ``verification_id`` is deliberately opaque: it identifies the already
    verified Approval-service proof without putting a credential in Domain
    state.  The reducer-side verifier must be pure (for example signature or
    immutable snapshot validation); external Store lookups happen before the
    Domain event is constructed.
    """

    receipt_id: str
    action_hash: str
    approver_id: str
    entity_id: str
    event_type: str
    issued_at: float
    expires_at: float
    consumed_at: float
    verification_id: str

    def __post_init__(self) -> None:
        for label, value in (
            ("receipt_id", self.receipt_id),
            ("action_hash", self.action_hash),
            ("approver_id", self.approver_id),
            ("entity_id", self.entity_id),
            ("event_type", self.event_type),
            ("verification_id", self.verification_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"ApprovalReceipt.{label} 必须是非空字符串")
        if len(self.action_hash) != 64 or any(
            character not in "0123456789abcdefABCDEF"
            for character in self.action_hash
        ):
            raise ValueError("ApprovalReceipt.action_hash 必须是 SHA-256 十六进制摘要")
        for label, timestamp_value in (
            ("issued_at", self.issued_at),
            ("expires_at", self.expires_at),
            ("consumed_at", self.consumed_at),
        ):
            if (
                isinstance(timestamp_value, bool)
                or not isinstance(timestamp_value, (int, float))
                or not math.isfinite(float(timestamp_value))
            ):
                raise ValueError(f"ApprovalReceipt.{label} 必须是有限时间戳")
        if not self.issued_at <= self.consumed_at <= self.expires_at:
            raise ValueError("ApprovalReceipt 时间顺序必须满足 issued <= consumed <= expires")


# This callback is part of the pure reducer boundary and must not perform I/O.
ApprovalReceiptVerifier: TypeAlias = Callable[[ApprovalReceipt], bool]


def domain_action_hash(
    *,
    entity_id: str,
    event_type: str,
    data: dict[str, Any],
) -> str:
    """Derive the exact approval action from trusted Domain event fields."""

    try:
        encoded = json.dumps(
            {
                "entityId": entity_id,
                "eventType": event_type,
                "data": data,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError(f"Domain Event data 必须是严格 JSON：{error}") from error
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["ApprovalReceipt", "ApprovalReceiptVerifier", "domain_action_hash"]
