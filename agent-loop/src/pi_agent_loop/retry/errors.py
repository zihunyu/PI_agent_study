"""工具执行层可分类的重试错误。"""

from __future__ import annotations


class RetryableToolError(Exception):
    """工具主动声明一个可安全重试的瞬时失败。"""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        if not code.strip():
            raise ValueError("RetryableToolError code 不能为空")
        if retry_after_seconds is not None and retry_after_seconds < 0:
            raise ValueError("retry_after_seconds 不能小于 0")
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.attempts = 1
        self.retry_id: str | None = None


class OutcomeUnknownToolError(Exception):
    """写操作可能已提交，但调用方未收到确定结果，禁止直接重试。"""

    def __init__(
        self,
        message: str,
        *,
        operation_id: str,
        idempotency_key: str,
        reconciliation_name: str,
    ) -> None:
        super().__init__(message)
        if not operation_id or not idempotency_key or not reconciliation_name:
            raise ValueError("outcome_unknown 必须提供操作 ID、幂等键和核对器名称")
        self.operation_id = operation_id
        self.idempotency_key = idempotency_key
        self.reconciliation_name = reconciliation_name
