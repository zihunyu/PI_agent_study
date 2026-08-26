"""业务配置和路由层错误。"""

from __future__ import annotations


class BusinessConfigError(ValueError):
    """简化 business.toml 不符合业务配置契约。"""

    code = "business_config_error"
