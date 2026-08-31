"""Durable Host Recovery Callback 装配。"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from ..session import RecoveryCallbacks
from ..writes import WriteOperationService


def build_recovery_callbacks(
    *,
    model_runtime: Any,
    tool_runtime: Any,
    reconcile_tool: Callable[..., Any] | None,
    write_service: WriteOperationService | None = None,
    reconcile_write: Callable[..., Any] | None = None,
) -> RecoveryCallbacks:
    async def reconcile(action):
        if reconcile_tool is None:
            raise RuntimeError(
                f"工具 {action.tool_name} 需要 Reconciliation Adapter"
            )
        value = reconcile_tool(action)
        return await value if inspect.isawaitable(value) else value

    async def reconcile_durable_write(action):
        if reconcile_write is None or write_service is None:
            raise RuntimeError(
                f"Write {action.write_id} 需要 Reconciliation Adapter"
            )
        if action.write_id is None:
            raise RuntimeError("Write Reconciliation 缺少 Write ID")

        async def check_external_state(write):
            # ``WriteOperationService`` supplies its own fenced generation on
            # ``write.fencing_token``.  The business adapter must forward that
            # token to the external system (or compare it there) before it
            # returns an authoritative result.
            value = reconcile_write(write)
            return await value if inspect.isawaitable(value) else value

        return await write_service.reconcile(
            action.write_id,
            check_external_state,
        )

    return RecoveryCallbacks(
        request_model=lambda messages, policy: model_runtime.request(
            messages,
            policy=policy,
        ),
        execute_tool=lambda action: tool_runtime.execute(action),
        reconcile_tool=reconcile,
        reconcile_write=(
            reconcile_durable_write if reconcile_write is not None else None
        ),
        request_model_with_context=lambda messages, policy, identity: (
            model_runtime.request(
                messages,
                policy=policy,
                request_id=identity["requestId"],
                durable_metadata={
                    key: value
                    for key, value in identity.items()
                    if key
                    in {
                        "sessionId",
                        "operationId",
                        "runId",
                        "fencingToken",
                        "fencingScope",
                    }
                },
            )
        ),
    )


__all__ = ["build_recovery_callbacks"]
