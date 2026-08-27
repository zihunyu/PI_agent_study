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

    def __init__(self, *, _parent: "CancellationToken | None" = None) -> None:
        self._event = asyncio.Event()
        self._reason = "操作已取消"
        self._parent = _parent
        self._children: set[CancellationToken] = set()

        # 子令牌创建后登记到父令牌。父令牌已经取消时，子令牌立即继承取消；
        # 子令牌自己取消时不会反向取消父令牌或其他并行工具。
        if _parent is not None:
            _parent._children.add(self)
            if _parent.cancelled:
                self.cancel(_parent.reason)

    @property
    def cancelled(self) -> bool:
        """当前是否已经收到取消请求。"""

        return self._event.is_set()

    @property
    def reason(self) -> str:
        """取消原因；主要用于生成面向用户的错误消息。"""

        return self._reason

    @property
    def child_count(self) -> int:
        """当前仍挂接的子令牌数量，供清理诊断和测试使用。"""

        return len(self._children)

    def cancel(self, reason: str = "操作已取消") -> None:
        """发出取消请求；重复调用是安全的，第一次原因会被保留。"""

        if self._event.is_set():
            return
        self._reason = reason
        self._event.set()

        # Agent 总令牌取消时向所有工具子令牌传播。先复制集合，避免子令牌
        # 在取消回调中 detach 导致遍历集合时发生变化。
        for child in list(self._children):
            child.cancel(reason)

    def throw_if_cancelled(self) -> None:
        """若已取消则立即抛出统一异常。"""

        if self.cancelled:
            raise OperationCancelledError(self._reason)

    async def wait(self) -> None:
        """等待取消发生。"""

        await self._event.wait()

    def create_child(self) -> "CancellationToken":
        """创建只受当前令牌控制的子取消令牌。

        典型用途是每个并行工具一个子令牌：用户取消 Agent 时全部取消，
        单个工具超时时只取消它自己的子令牌。
        """

        return CancellationToken(_parent=self)

    def detach(self) -> None:
        """工具结束后解除父子关系，防止父令牌长期保存已结束工具。"""

        if self._parent is None:
            return
        self._parent._children.discard(self)
        self._parent = None
