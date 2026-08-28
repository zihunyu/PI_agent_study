"""根据配置创建 Model 与具体 Provider。"""

from __future__ import annotations

import httpx

from ..types import Model
from .openai_compatible import OpenAICompatibleProvider
from .settings import ProviderSettings


def create_provider(
    settings: ProviderSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    client: httpx.AsyncClient | None = None,
    owns_client: bool | None = None,
    limits: httpx.Limits | None = None,
) -> tuple[Model, OpenAICompatibleProvider]:
    """创建当前 active profile 对应的模型和 Provider。"""

    profile = settings.active
    model = Model(
        id=profile.model,
        provider=profile.name,
        api=profile.protocol,
        name=profile.model,
    )
    return model, OpenAICompatibleProvider(
        profile,
        transport=transport,
        client=client,
        owns_client=owns_client,
        limits=limits,
    )
