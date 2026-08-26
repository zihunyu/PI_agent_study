"""第三方模型 Provider 的结构化错误。

错误消息绝不能包含 API Key、Authorization Header 或其他认证材料。
"""

from __future__ import annotations


class ProviderError(Exception):
    """全部 Provider 错误的基类。"""

    code = "provider_error"

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderConfigError(ProviderError):
    """Provider 本地配置无效。"""

    code = "provider_config_error"


class ProviderAuthenticationError(ProviderError):
    """API Key 无效或没有访问权限。"""

    code = "provider_authentication_error"


class ProviderRateLimitError(ProviderError):
    """第三方 API 拒绝了过多请求。"""

    code = "provider_rate_limit_error"


class ProviderTimeoutError(ProviderError):
    """连接或读取模型响应超时。"""

    code = "provider_timeout_error"


class ProviderModelNotFoundError(ProviderError):
    """配置的模型或请求 Endpoint 不存在。"""

    code = "provider_model_not_found"


class ProviderHTTPError(ProviderError):
    """第三方 API 返回其他 HTTP 错误。"""

    code = "provider_http_error"


class ProviderProtocolError(ProviderError):
    """响应不符合 OpenAI-compatible/SSE 协议。"""

    code = "provider_protocol_error"
