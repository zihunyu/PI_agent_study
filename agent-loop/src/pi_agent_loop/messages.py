"""消息构造与复制辅助函数。

为了让示例无需依赖 Pydantic、TypeBox 等第三方库，本项目使用普通 ``dict``
表示消息和事件。它与模型 API 常见的 JSON 结构接近，也允许应用增加自定义
role。核心循环只要求 assistant、user、toolResult 三种标准角色满足约定字段。
"""

from __future__ import annotations

import copy
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .types import AgentToolResult


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


def user_message(text: str, images: list[dict] | None = None) -> dict:
    """创建标准用户消息。"""

    content = [{"type": "text", "text": text}]
    if images:
        content.extend(copy.deepcopy(images))
    return {"role": "user", "content": content, "timestamp": now_ms()}


def assistant_message(
    *,
    model: "ModelLike",
    content: list[dict] | None = None,
    stop_reason: str = "stop",
    error_message: str | None = None,
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
        message["errorMessage"] = error_message
    return message


def error_tool_result(
    message: str,
    *,
    terminate: bool = False,
    details: dict | None = None,
) -> "AgentToolResult":
    """把工具准备或执行异常规范成模型可读取的文本结果。"""

    # 延迟导入用于避免 messages.py 与 types.py 的循环导入。
    from .types import AgentToolResult

    return AgentToolResult(
        content=[{"type": "text", "text": message}],
        details=details or {},
        terminate=True if terminate else None,
    )


def clone_message(message: dict) -> dict:
    """生成深复制事件快照，避免 Provider 后续修改污染旧事件。"""

    return copy.deepcopy(message)


class ModelLike:
    """仅用于类型提示的最小模型结构。"""

    id: str
    provider: str
    api: str
