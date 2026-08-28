"""Durable Host Recovery Callback 装配。"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from ..session import RecoveryCallbacks


def build_recovery_callbacks(
    *,
    model_runtime: Any,
    tool_runtime: Any,
    reconcile_tool: Callable[..., Any] | None,
) -> RecoveryCallbacks:
    async def reconcile(action):
        if reconcile_tool is None:
            raise RuntimeError(
                f"工具 {action.tool_name} 需要 Reconciliation Adapter"
            )
        value = reconcile_tool(action)
        return await value if inspect.isawaitable(value) else value

    return RecoveryCallbacks(
        request_model=lambda messages, policy: model_runtime.request(
            messages,
            policy=policy,
        ),
        execute_tool=lambda action: tool_runtime.execute(action),
        reconcile_tool=reconcile,
        request_model_with_context=lambda messages, policy, identity: (
            model_runtime.request(
                messages,
                policy=policy,
                request_id=identity["requestId"],
                durable_metadata={
                    key: value
                    for key, value in identity.items()
                    if key in {"sessionId", "operationId", "runId"}
                },
            )
        ),
    )


__all__ = ["build_recovery_callbacks"]
