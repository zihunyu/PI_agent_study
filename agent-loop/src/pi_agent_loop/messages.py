"""消息构造与复制辅助函数。

为了让示例无需依赖 Pydantic、TypeBox 等第三方库，本项目使用普通 ``dict``
表示消息和事件。它与模型 API 常见的 JSON 结构接近，也允许应用增加自定义
role。核心循环只要求 assistant、user、toolResult 三种标准角色满足约定字段。
"""

from __future__ import annotations

import base64
import binascii
import copy
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .types import AgentToolResult


_SENSITIVE_KEY_NAMES = frozenset(
    {
        "apikey",
        "authorization",
        "authorizationheader",
        "authtoken",
        "bearertoken",
        "clientsecret",
        "clienttoken",
        "cookie",
        "credential",
        "idempotencykey",
        "password",
        "privatekey",
        "proxyauthorization",
        "refreshtoken",
        "secret",
        "secretkey",
        "sessiontoken",
        "token",
        "xapikey",
        "accesstoken",
    }
)
_SENSITIVE_KEY_SUFFIXES = (
    "accesstoken",
    "apikey",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "clienttoken",
    "cookie",
    "credential",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "secretkey",
    "sessiontoken",
)
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+[^\s,;]+")
_LABELED_SECRET_RE = re.compile(
    r"(?ix)"
    r"\b("
    r"authorization|proxy[-_\s]?authorization|"
    r"api[-_\s]?key|x[-_\s]?api[-_\s]?key|"
    r"access[-_\s]?token|refresh[-_\s]?token|"
    r"auth[-_\s]?token|bearer[-_\s]?token|session[-_\s]?token|"
    r"client[-_\s]?secret|private[-_\s]?key|secret[-_\s]?key|"
    r"password|passwd|credential|cookie|secret|token"
    r")\b(\s*[:=]\s*)(?:bearer\s+|basic\s+)?([^\s,;]+)"
)
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}\b"
)
_COMMON_SECRET_TOKEN_RE = re.compile(
    r"\b(?:sk|rk|pk)-(?:live|test|proj)?-?[A-Za-z0-9_-]{8,}\b",
    re.IGNORECASE,
)
_AWS_ACCESS_KEY_RE = re.compile(
    r"\b(?:AKIA|ASIA|A3T[A-Z0-9]|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA)"
    r"[A-Z0-9]{16}\b"
)
_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})\b"
)
_SLACK_TOKEN_RE = re.compile(r"\bxox[A-Za-z]-[A-Za-z0-9-]{10,}\b")
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 ]*PRIVATE KEY)-----"
    r"[\s\S]*?"
    r"-----END (?P=label)-----"
)
_DATA_IMAGE_URL_RE = re.compile(
    r"data:image/(?P<format>png|jpeg|webp|gif);base64,"
    r"(?P<payload>[A-Za-z0-9+/]+={0,2})\Z",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MessageInputLimits:
    """Finite limits for one user message accepted by the public Agent API."""

    max_text_bytes: int = 256 * 1024
    max_images: int = 8
    max_image_url_bytes: int = 8 * 1024 * 1024
    max_total_image_url_bytes: int = 16 * 1024 * 1024
    max_content_blocks: int = 16

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_images, bool)
            or not isinstance(self.max_images, int)
            or self.max_images < 0
        ):
            raise ValueError("max_images 必须是非负整数")
        for name in (
            "max_text_bytes",
            "max_image_url_bytes",
            "max_total_image_url_bytes",
            "max_content_blocks",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须是正整数")


DEFAULT_MESSAGE_INPUT_LIMITS = MessageInputLimits()


def _normalized_sensitive_key(value: Any) -> str:
    return "".join(
        character for character in str(value).casefold() if character.isalnum()
    )


def is_sensitive_key(value: Any) -> bool:
    """Return whether a structured field name conventionally carries a secret."""

    normalized = _normalized_sensitive_key(value)
    return normalized in _SENSITIVE_KEY_NAMES or normalized.endswith(
        _SENSITIVE_KEY_SUFFIXES
    )


def redact_sensitive_text(
    value: str,
    *,
    replacement: str = "<redacted>",
) -> str:
    """Conservatively redact common credentials embedded in free-form text."""

    redacted = _PEM_PRIVATE_KEY_RE.sub(replacement, value)
    redacted = _LABELED_SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{replacement}",
        redacted,
    )
    redacted = _AUTH_SCHEME_RE.sub(replacement, redacted)
    redacted = _JWT_RE.sub(replacement, redacted)
    redacted = _AWS_ACCESS_KEY_RE.sub(replacement, redacted)
    redacted = _GITHUB_TOKEN_RE.sub(replacement, redacted)
    redacted = _SLACK_TOKEN_RE.sub(replacement, redacted)
    return _COMMON_SECRET_TOKEN_RE.sub(replacement, redacted)


def redact_sensitive_data(
    value: Any,
    *,
    replacement: str = "<redacted>",
) -> Any:
    """Return a JSON-safe public view without invoking arbitrary ``repr`` hooks."""

    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            output[key] = (
                replacement
                if is_sensitive_key(key)
                else redact_sensitive_data(item, replacement=replacement)
            )
        return output
    if isinstance(value, (list, tuple)):
        return [redact_sensitive_data(item, replacement=replacement) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value, replacement=replacement)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


def public_error_message(
    value: str | BaseException,
    *,
    fallback: str = "操作执行失败",
    max_length: int = 500,
) -> str:
    """Build a model/UI-safe error message without stringifying exceptions.

    Exception text is internal by default.  An exception may opt in to a public
    message through a string ``public_message`` attribute; that text is still
    redacted and bounded before crossing the public boundary.
    """

    if isinstance(value, BaseException):
        candidate = getattr(value, "public_message", None)
        if not isinstance(candidate, str) or not candidate.strip():
            candidate = fallback
    elif isinstance(value, str):
        candidate = value or fallback
    else:  # Defensive runtime guard for callers outside the annotated API.
        candidate = fallback
    message = redact_sensitive_text(candidate.strip(), replacement="<redacted>")
    if not message:
        message = fallback
    if (
        isinstance(max_length, bool)
        or not isinstance(max_length, int)
        or max_length <= 0
    ):
        raise ValueError("max_length 必须是正整数")
    if len(message) > max_length:
        message = message[:max_length] + "<truncated>"
    return message


def now_ms() -> int:
    """返回 Unix 毫秒时间戳。"""

    return int(time.time() * 1000)


def empty_usage() -> dict[str, Any]:
    """创建一份全零用量，避免多个消息共享同一个可变字典。"""

    return {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 0,
        "cost": {
            "input": 0.0,
            "output": 0.0,
            "cacheRead": 0.0,
            "cacheWrite": 0.0,
            "total": 0.0,
        },
    }


def user_message(
    text: str,
    images: list[dict] | None = None,
    *,
    limits: MessageInputLimits | None = None,
) -> dict:
    """创建一个经过规范化和资源限制校验的标准用户消息。

    图片采用 OpenAI Chat Completions 的 ``image_url`` content block。URL 只
    接受 HTTPS，或严格的 ``data:image/...;base64,...``。不支持的媒体类型会在
    Agent API 边界被拒绝，而不是等到远端 Provider 才静默丢失。
    """

    if not isinstance(text, str):
        raise TypeError("用户消息 text 必须是字符串")
    if images is not None and not isinstance(images, list):
        raise TypeError("用户消息 images 必须是列表或 None")
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    content.extend(copy.deepcopy(images or []))
    normalized = normalize_user_content(content, limits=limits)
    return {"role": "user", "content": normalized, "timestamp": now_ms()}


def normalize_user_message(
    message: Mapping[str, Any],
    *,
    limits: MessageInputLimits | None = None,
) -> dict[str, Any]:
    """Copy and validate a caller-supplied user message."""

    if not isinstance(message, Mapping):
        raise TypeError("用户消息必须是 Mapping")
    if message.get("role") != "user":
        raise ValueError("normalize_user_message 只接受 role=user")
    normalized = copy.deepcopy(dict(message))
    normalized["content"] = normalize_user_content(
        message.get("content"),
        limits=limits,
    )
    return normalized


def normalize_user_content(
    content: Any,
    *,
    limits: MessageInputLimits | None = None,
) -> list[dict[str, Any]]:
    """Normalize one user content list and enforce cumulative byte limits."""

    effective = limits or DEFAULT_MESSAGE_INPUT_LIMITS
    if not isinstance(effective, MessageInputLimits):
        raise TypeError("limits 必须是 MessageInputLimits 或 None")
    if not isinstance(content, list):
        raise TypeError("用户消息 content 必须是列表")
    if len(content) > effective.max_content_blocks:
        raise ValueError("用户消息 content block 数量超过限制")

    normalized: list[dict[str, Any]] = []
    text_bytes = 0
    image_count = 0
    total_image_url_bytes = 0
    for block in content:
        if not isinstance(block, Mapping):
            raise TypeError("用户消息包含无效 content block")
        block_type = block.get("type")
        if block_type == "text":
            if set(block) != {"type", "text"}:
                raise ValueError("text content block 只能包含 type 和 text")
            text = block.get("text")
            if not isinstance(text, str):
                raise TypeError("text content block 的 text 必须是字符串")
            text_bytes += len(text.encode("utf-8"))
            if text_bytes > effective.max_text_bytes:
                raise ValueError("用户消息文本字节数超过限制")
            normalized.append({"type": "text", "text": text})
            continue
        if block_type != "image_url":
            raise ValueError(f"不支持的用户 content 类型：{block_type!r}")
        image_count += 1
        if image_count > effective.max_images:
            raise ValueError("用户消息图片数量超过限制")
        image = normalize_image_url_block(
            block,
            max_url_bytes=effective.max_image_url_bytes,
        )
        total_image_url_bytes += len(image["image_url"]["url"].encode("utf-8"))
        if total_image_url_bytes > effective.max_total_image_url_bytes:
            raise ValueError("用户消息 image URL 累计字节数超过限制")
        normalized.append(image)
    return normalized


def normalize_image_url_block(
    block: Mapping[str, Any],
    *,
    max_url_bytes: int = DEFAULT_MESSAGE_INPUT_LIMITS.max_image_url_bytes,
) -> dict[str, Any]:
    """Return one canonical OpenAI ``image_url`` block."""

    if not isinstance(block, Mapping):
        raise TypeError("image_url content block 必须是 Mapping")
    if block.get("type") != "image_url":
        raise ValueError("图片 content block.type 必须是 image_url")
    if set(block) != {"type", "image_url"}:
        raise ValueError("image_url content block 只能包含 type 和 image_url")
    raw_image_url = block.get("image_url")
    if isinstance(raw_image_url, str):
        url = raw_image_url
        detail: str | None = None
    elif isinstance(raw_image_url, Mapping):
        if not set(raw_image_url).issubset({"url", "detail"}):
            raise ValueError("image_url 对象只能包含 url 和 detail")
        raw_url = raw_image_url.get("url")
        detail = raw_image_url.get("detail")
        if not isinstance(raw_url, str):
            raise TypeError("image_url.url 必须是字符串")
        url = raw_url
    else:
        raise TypeError("image_url 必须是 URL 字符串或包含 url 的对象")
    if not isinstance(url, str) or not url:
        raise ValueError("image_url.url 必须是非空字符串")
    if isinstance(max_url_bytes, bool) or not isinstance(max_url_bytes, int) or max_url_bytes <= 0:
        raise ValueError("max_url_bytes 必须是正整数")
    if len(url.encode("utf-8")) > max_url_bytes:
        raise ValueError("image URL 字节数超过限制")
    if not (url.startswith("https://") or _is_valid_data_image_url(url)):
        raise ValueError("image_url.url 只支持 HTTPS 或 base64 data:image URL")
    if detail is not None and detail not in {"auto", "low", "high"}:
        raise ValueError("image_url.detail 必须是 auto、low 或 high")
    image_url: dict[str, str] = {"url": url}
    if isinstance(detail, str):
        image_url["detail"] = detail
    return {"type": "image_url", "image_url": image_url}


def _is_valid_data_image_url(value: str) -> bool:
    match = _DATA_IMAGE_URL_RE.fullmatch(value)
    if match is None:
        return False
    try:
        return bool(base64.b64decode(match.group("payload"), validate=True))
    except (ValueError, binascii.Error):
        return False


def assistant_message(
    *,
    model: "ModelLike",
    content: list[dict] | None = None,
    stop_reason: str = "stop",
    error_message: str | BaseException | None = None,
    usage: dict | None = None,
) -> dict:
    """创建标准 assistant 消息；主要供 Provider 和测试使用。"""

    message = {
        "role": "assistant",
        "content": copy.deepcopy(content or []),
        "api": model.api,
        "provider": model.provider,
        "model": model.id,
        "usage": copy.deepcopy(usage or empty_usage()),
        "stopReason": stop_reason,
        "timestamp": now_ms(),
    }
    if error_message is not None:
        message["errorMessage"] = public_error_message(
            error_message,
            fallback="模型请求失败",
        )
    return message


def error_tool_result(
    message: str | BaseException,
    *,
    terminate: bool = False,
    details: dict | None = None,
) -> "AgentToolResult":
    """把工具准备或执行异常规范成模型可读取的文本结果。"""

    # 延迟导入用于避免 messages.py 与 types.py 的循环导入。
    from .types import AgentToolResult

    public_message = public_error_message(
        message,
        fallback="工具执行失败",
    )
    return AgentToolResult(
        content=[{"type": "text", "text": public_message}],
        details=redact_sensitive_data(details or {}),
        terminate=True if terminate else None,
    )


def clone_message(message: dict) -> dict:
    """生成深复制事件快照，避免 Provider 后续修改污染旧事件。"""

    return copy.deepcopy(message)


class ModelLike(Protocol):
    """仅用于类型提示的最小模型结构。"""

    id: str
    provider: str
    api: str
