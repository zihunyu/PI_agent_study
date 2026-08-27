"""模型 Assistant 错误是否可重试的保守分类器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .types import ModelRetryPolicy

_RETRYABLE_CODES = {
    "provider_timeout_error",
    "provider_http_error",
    "provider_rate_limit_error",
    "provider_transport_error",
}
_NON_RETRYABLE_TEXT = (
    "insufficient_quota",
    "quota exceeded",
    "billing",
    "out of budget",
    "context overflow",
    "context length",
)
_RETRYABLE_TEXT = (
    "rate limit",
    "too many requests",
    "overloaded",
    "service unavailable",
    "connection reset",
    "connection refused",
    "network error",
    "timed out",
    "timeout",
    "stream ended",
)


@dataclass(frozen=True, slots=True)
class ModelRetryDecision:
    retryable: bool
    code: str
    status_code: int | None = None
    retry_after_seconds: float | None = None


def classify_model_error(
    message: dict[str, Any],
    policy: ModelRetryPolicy,
) -> ModelRetryDecision:
    """结构化字段优先；文本只作为旧 Provider 的保守 fallback。"""

    if message.get("stopReason") != "error":
        return ModelRetryDecision(False, "not_error")
    details = message.get("providerError")
    if isinstance(details, dict):
        code = str(details.get("code", "provider_error"))
        status = details.get("statusCode")
        status_code = (
            status
            if isinstance(status, int) and not isinstance(status, bool)
            else None
        )
        retry_after_ms = details.get("retryAfterMs")
        retry_after = (
            float(retry_after_ms) / 1000
            if isinstance(retry_after_ms, (int, float))
            and not isinstance(retry_after_ms, bool)
            and retry_after_ms >= 0
            else None
        )
        retryable_flag = details.get("retryable")
        if isinstance(retryable_flag, bool):
            retryable = retryable_flag
        else:
            retryable = (
                status_code in policy.retryable_statuses
                if status_code is not None
                else code in _RETRYABLE_CODES
            )
        return ModelRetryDecision(
            retryable,
            code,
            status_code,
            retry_after,
        )

    text = str(message.get("errorMessage", "")).casefold()
    if any(pattern in text for pattern in _NON_RETRYABLE_TEXT):
        return ModelRetryDecision(False, "non_retryable_provider_limit")
    if any(pattern in text for pattern in _RETRYABLE_TEXT):
        return ModelRetryDecision(True, "legacy_transient_error")
    return ModelRetryDecision(False, "unclassified_error")
