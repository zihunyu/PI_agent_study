"""合作式取消工具。

JavaScript/TypeScript 版本通过 AbortController 与 AbortSignal 传播取消。
Python 标准库没有完全同名的通用对象，因此这里使用 asyncio.Event
实现一个语义接近的 CancellationToken。

注意：取消是“合作式”的。模型适配器和工具必须主动检查 token，或者
等待 token.wait()；如果某段同步代码完全不检查它，循环无法强制终止该代码。
"""

from __future__ import annotations

import asyncio


class OperationCancelledError(Exception):
    """表示调用者主动取消了当前 Agent 操作。"""


class CancellationToken:
    """可在模型、工具、钩子和等待逻辑之间共享的取消令牌。"""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason = "操作已取消"

    @property
    def cancelled(self) -> bool:
        """当前是否已经收到取消请求。"""

        return self._event.is_set()

    @property
    def reason(self) -> str:
        """取消原因；主要用于生成面向用户的错误消息。"""

        return self._reason

    def cancel(self, reason: str = "操作已取消") -> None:
        """发出取消请求；重复调用是安全的，第一次原因会被保留。"""

        if self._event.is_set():
            return
        self._reason = reason
        self._event.set()

    def throw_if_cancelled(self) -> None:
        """若已取消则立即抛出统一异常。"""

        if self.cancelled:
            raise OperationCancelledError(self._reason)

    async def wait(self) -> None:
        """等待取消发生。"""

        await self._event.wait()
