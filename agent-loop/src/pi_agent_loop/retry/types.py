"""模型和工具重试策略类型。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .circuit_breaker import CircuitBreakerPolicy

_DEFAULT_RETRYABLE_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class ModelRetryPolicy:
    """一个逻辑模型 Turn 内的请求重试策略。"""

    enabled: bool = False
    max_retries: int = 0
    initial_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0
    jitter_ratio: float = 0.2
    retryable_statuses: frozenset[int] = field(
        default_factory=lambda: _DEFAULT_RETRYABLE_STATUSES
    )
    max_elapsed_seconds: float = 120.0
    circuit_breaker: CircuitBreakerPolicy = field(
        default_factory=CircuitBreakerPolicy
    )

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or self.max_retries < 0
        ):
            raise ValueError("model retry max_retries 必须是大于等于 0 的整数")
        if self.enabled and self.max_retries < 1:
            raise ValueError("启用 model retry 时 max_retries 必须大于 0")
        if (
            not math.isfinite(self.initial_delay_seconds)
            or not math.isfinite(self.max_delay_seconds)
            or self.initial_delay_seconds < 0
            or self.max_delay_seconds <= 0
        ):
            raise ValueError("model retry delay 配置无效")
        if self.initial_delay_seconds > self.max_delay_seconds:
            raise ValueError("initial_delay_seconds 不能超过 max_delay_seconds")
        if not math.isfinite(self.max_elapsed_seconds) or self.max_elapsed_seconds <= 0:
            raise ValueError("model retry max_elapsed_seconds 必须大于 0")
        if not math.isfinite(self.jitter_ratio) or not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio 必须在 0 到 1 之间")
        if any(
            isinstance(status, bool)
            or not isinstance(status, int)
            or status < 100
            or status > 599
            for status in self.retryable_statuses
        ):
            raise ValueError("retryable_statuses 必须是合法 HTTP 状态码")


@dataclass(frozen=True, slots=True)
class ToolRetryPolicy:
    """单个逻辑 Tool Call 的幂等重试策略。"""

    max_retries: int
    retryable_codes: frozenset[str]
    idempotent: bool
    initial_delay_seconds: float = 0.2
    max_delay_seconds: float = 5.0
    jitter_ratio: float = 0.2
    max_elapsed_seconds: float = 30.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or self.max_retries < 1
        ):
            raise ValueError("tool retry max_retries 必须是大于 0 的整数")
        if not self.idempotent:
            raise ValueError("自动 Tool Retry 只允许显式声明幂等的工具")
        if not self.retryable_codes or any(
            not code.strip() for code in self.retryable_codes
        ):
            raise ValueError("tool retry 必须声明非空 retryable_codes")
        if (
            not math.isfinite(self.initial_delay_seconds)
            or not math.isfinite(self.max_delay_seconds)
            or self.initial_delay_seconds < 0
            or self.max_delay_seconds <= 0
        ):
            raise ValueError("tool retry delay 配置无效")
        if self.initial_delay_seconds > self.max_delay_seconds:
            raise ValueError("initial_delay_seconds 不能超过 max_delay_seconds")
        if not math.isfinite(self.max_elapsed_seconds) or self.max_elapsed_seconds <= 0:
            raise ValueError("tool retry max_elapsed_seconds 必须大于 0")
        if not math.isfinite(self.jitter_ratio) or not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio 必须在 0 到 1 之间")
