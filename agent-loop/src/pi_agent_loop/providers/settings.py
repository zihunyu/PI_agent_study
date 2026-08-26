"""第三方大模型配置加载与严格校验。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

from .errors import ProviderConfigError

_SUPPORTED_PROTOCOL = "openai_chat_completions"
_SUPPORTED_ENDPOINT = "/chat/completions"
_PLACEHOLDERS = {
    "REPLACE_WITH_YOUR_API_KEY",
    "REPLACE_WITH_PROVIDER_MODEL_ID",
}
_PROFILE_FIELDS = {
    "protocol",
    "base_url",
    "endpoint",
    "auth_type",
    "api_key",
    "model",
    "stream",
    "connect_timeout_seconds",
    "request_timeout_seconds",
    "allow_insecure_http",
}
_REQUIRED_PROFILE_FIELDS = set(_PROFILE_FIELDS)


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """一个可直接建立 Provider 的配置档案。"""

    name: str
    protocol: str
    base_url: str
    endpoint: str
    auth_type: str
    # repr=False 防止调试输出 dataclass 时泄露 API Key。
    api_key: str = field(repr=False)
    model: str
    stream: bool
    connect_timeout_seconds: float
    request_timeout_seconds: float
    allow_insecure_http: bool

    @property
    def request_url(self) -> str:
        """安全拼接 Base URL 和 Endpoint，避免双斜线。"""

        return self.base_url.rstrip("/") + "/" + self.endpoint.lstrip("/")


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """配置文件中的全部档案以及当前选中的档案。"""

    active_profile: str
    profiles: Mapping[str, ProviderProfile]

    @property
    def active(self) -> ProviderProfile:
        return self.profiles[self.active_profile]


def _require_table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderConfigError(f"Provider 配置必须包含 {label} 表")
    return value


def _require_string(data: dict[str, Any], field: str, profile: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProviderConfigError(f"Profile {profile} 的 {field} 必须是非空字符串")
    return value.strip()


def _require_bool(data: dict[str, Any], field: str, profile: str) -> bool:
    value = data.get(field)
    if not isinstance(value, bool):
        raise ProviderConfigError(f"Profile {profile} 的 {field} 必须是布尔值")
    return value


def _require_positive_number(
    data: dict[str, Any], field: str, profile: str
) -> float:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ProviderConfigError(f"Profile {profile} 的 {field} 必须是大于 0 的数")
    return float(value)


def _validate_url(
    base_url: str, *, allow_insecure_http: bool, profile: str
) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderConfigError(
            f"Profile {profile} 的 base_url 必须是完整 HTTP/HTTPS URL"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderConfigError(
            f"Profile {profile} 的 base_url 不能包含认证信息、查询参数或 fragment"
        )
    if base_url.rstrip("/").endswith(_SUPPORTED_ENDPOINT):
        raise ProviderConfigError(
            f"Profile {profile} 的 base_url 不应包含 {_SUPPORTED_ENDPOINT}"
        )

    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if (
        parsed.scheme == "http"
        and parsed.hostname not in local_hosts
        and not allow_insecure_http
    ):
        raise ProviderConfigError(
            f"Profile {profile} 的远程 base_url 必须使用 HTTPS；"
            "如确有需要，请显式设置 allow_insecure_http=true"
        )


def _parse_profile(name: str, raw: Any) -> ProviderProfile:
    data = _require_table(raw, f"[profiles.{name}]")
    missing = sorted(_REQUIRED_PROFILE_FIELDS - data.keys())
    if missing:
        raise ProviderConfigError(
            f"Profile {name} 缺少字段：{', '.join(missing)}"
        )
    unknown = sorted(data.keys() - _PROFILE_FIELDS)
    if unknown:
        raise ProviderConfigError(
            f"Profile {name} 包含未知字段：{', '.join(unknown)}"
        )

    protocol = _require_string(data, "protocol", name)
    if protocol != _SUPPORTED_PROTOCOL:
        raise ProviderConfigError(
            f"Profile {name} 暂不支持协议：{protocol}；"
            f"当前只支持 {_SUPPORTED_PROTOCOL}"
        )

    endpoint = _require_string(data, "endpoint", name)
    if endpoint != _SUPPORTED_ENDPOINT:
        raise ProviderConfigError(
            f"Profile {name} 的 endpoint 必须是 {_SUPPORTED_ENDPOINT}"
        )

    auth_type = _require_string(data, "auth_type", name)
    if auth_type != "bearer":
        raise ProviderConfigError(
            f"Profile {name} 的 auth_type 必须是 bearer"
        )

    api_key = _require_string(data, "api_key", name)
    model = _require_string(data, "model", name)
    if api_key in _PLACEHOLDERS or model in _PLACEHOLDERS:
        raise ProviderConfigError(
            f"Profile {name} 仍包含 .example 占位值，请填写本地真实配置"
        )

    stream = _require_bool(data, "stream", name)
    if not stream:
        raise ProviderConfigError(
            f"Profile {name} 当前必须设置 stream=true"
        )
    allow_insecure_http = _require_bool(
        data, "allow_insecure_http", name
    )
    base_url = _require_string(data, "base_url", name).rstrip("/")
    _validate_url(
        base_url,
        allow_insecure_http=allow_insecure_http,
        profile=name,
    )

    return ProviderProfile(
        name=name,
        protocol=protocol,
        base_url=base_url,
        endpoint=endpoint,
        auth_type=auth_type,
        api_key=api_key,
        model=model,
        stream=stream,
        connect_timeout_seconds=_require_positive_number(
            data, "connect_timeout_seconds", name
        ),
        request_timeout_seconds=_require_positive_number(
            data, "request_timeout_seconds", name
        ),
        allow_insecure_http=allow_insecure_http,
    )


def load_provider_settings(path: str | Path) -> ProviderSettings:
    """加载本地 Provider TOML，错误信息不会包含 API Key。"""

    config_path = Path(path)
    try:
        with config_path.open("rb") as file:
            root = tomllib.load(file)
    except FileNotFoundError as error:
        raise FileNotFoundError(
            f"Provider 配置不存在：{config_path}。"
            "请先复制 config/providers.toml.example 为 config/providers.toml"
        ) from error
    except tomllib.TOMLDecodeError as error:
        raise ProviderConfigError(f"Provider 配置 TOML 格式错误：{error}") from error

    allowed_root = {"active", "profiles"}
    unknown_root = sorted(root.keys() - allowed_root)
    if unknown_root:
        raise ProviderConfigError(
            f"Provider 配置包含未知顶层字段：{', '.join(unknown_root)}"
        )

    active_table = _require_table(root.get("active"), "[active]")
    unknown_active = sorted(active_table.keys() - {"profile"})
    if unknown_active:
        raise ProviderConfigError(
            f"[active] 包含未知字段：{', '.join(unknown_active)}"
        )
    active_profile = active_table.get("profile")
    if not isinstance(active_profile, str) or not active_profile.strip():
        raise ProviderConfigError("[active].profile 必须是非空字符串")
    active_profile = active_profile.strip()

    raw_profiles = _require_table(root.get("profiles"), "[profiles]")
    if not raw_profiles:
        raise ProviderConfigError("Provider 配置至少需要一个 profile")
    profiles = {
        str(name): _parse_profile(str(name), raw)
        for name, raw in raw_profiles.items()
    }
    if active_profile not in profiles:
        raise ProviderConfigError(
            f"Active profile {active_profile} 不存在于 [profiles]"
        )

    return ProviderSettings(
        active_profile=active_profile,
        profiles=MappingProxyType(profiles),
    )
