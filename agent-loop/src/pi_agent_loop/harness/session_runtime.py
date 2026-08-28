"""Session configuration identity and renewable single-writer lease."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from typing import Any
from uuid import uuid4

from ..session import OperationEventStore
from ..types import AgentTool, Model


class SessionAlreadyOpenError(RuntimeError):
    """Another Host currently owns the product-session writer lease."""


class SessionWriterLeaseLostError(RuntimeError):
    """The Host lost its session lease and must stop accepting prompts."""


def agent_configuration_hash(
    *,
    model: Model,
    system_prompt: str,
    tools: list[AgentTool],
) -> str:
    """Return a stable digest for inputs that change transcript semantics."""

    value = {
        "model": {
            "id": model.id,
            "provider": model.provider,
            "api": model.api,
            "contextWindow": model.context_window,
            "maxTokens": model.max_tokens,
            "reasoning": model.reasoning,
        },
        "systemPrompt": system_prompt,
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": _json_value(tool.parameters),
                "executionMode": tool.execution_mode,
                "timeoutSeconds": tool.timeout_seconds,
                "replayPolicy": tool.replay_policy,
                "priority": tool.priority,
                "resourceAccess": tool.resource_access,
                "lockTimeoutSeconds": tool.lock_timeout_seconds,
                "retryPolicy": _json_value(tool.retry_policy),
            }
            for tool in tools
        ],
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SessionWriterLease:
    """Renewable cross-process claim preventing divergent session histories."""

    claim_type = "conversation_session_writer"

    def __init__(
        self,
        store: OperationEventStore,
        session_id: str,
        *,
        lease_seconds: float,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("session_writer_lease_seconds 必须大于 0")
        self.store = store
        self.session_id = session_id
        self.lease_seconds = lease_seconds
        self.owner_token = uuid4().hex
        self._renew_task: asyncio.Task[None] | None = None
        self._closed = False
        self._lost_error: BaseException | None = None
        self._loss_callback: Callable[[], None] | None = None

    @classmethod
    async def acquire(
        cls,
        store: OperationEventStore,
        session_id: str,
        *,
        lease_seconds: float = 30,
    ) -> "SessionWriterLease":
        lease = cls(store, session_id, lease_seconds=lease_seconds)
        acquired = await store.try_acquire_claim(
            lease.claim_type,
            session_id,
            lease.owner_token,
            lease_seconds=lease_seconds,
        )
        if not acquired:
            raise SessionAlreadyOpenError(
                f"Session {session_id} 已被另一个 DurableAgentHost 打开"
            )
        lease._renew_task = asyncio.create_task(
            lease._renew_loop(),
            name=f"session-writer-lease:{session_id}",
        )
        return lease

    def assert_owned(self) -> None:
        if self._closed:
            raise SessionWriterLeaseLostError("Session Writer Lease 已关闭")
        if self._lost_error is not None:
            raise SessionWriterLeaseLostError(
                f"Session Writer Lease 已丢失：{self._lost_error}"
            ) from self._lost_error

    def set_loss_callback(self, callback: Callable[[], None]) -> None:
        self._loss_callback = callback
        if self._lost_error is not None:
            callback()

    def _mark_lost(self, error: BaseException) -> None:
        if self._lost_error is not None:
            return
        self._lost_error = error
        callback = self._loss_callback
        if callback is not None:
            try:
                callback()
            except BaseException:
                # The lease is already fail-closed; callback failures must not
                # keep the heartbeat alive or hide the original loss reason.
                pass

    async def _renew_loop(self) -> None:
        interval = max(0.05, min(self.lease_seconds / 3, 30))
        try:
            while True:
                await asyncio.sleep(interval)
                renewed = await self.store.try_acquire_claim(
                    self.claim_type,
                    self.session_id,
                    self.owner_token,
                    lease_seconds=self.lease_seconds,
                )
                if not renewed:
                    self._mark_lost(RuntimeError("Lease 已被其他 Owner 接管"))
                    return
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self._mark_lost(error)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._renew_task
        self._renew_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.store.release_claim(
            self.claim_type,
            self.session_id,
            self.owner_token,
        )


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if is_dataclass(value):
        return _json_value(asdict(value))
    return repr(value)


__all__ = [
    "SessionAlreadyOpenError",
    "SessionWriterLease",
    "SessionWriterLeaseLostError",
    "agent_configuration_hash",
]
