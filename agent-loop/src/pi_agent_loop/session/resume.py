"""根据持久 Operation State 规划安全恢复动作。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4

from ..durable_action import DurableActionEnvelope, DurableActionEnvelopeError
from ..model_policy import (
    ModelRequestPolicy,
    ModelRequestPolicyError,
    validate_recoverable_model_response,
)
from .operation_state import (
    ApprovalSnapshot,
    OperationState,
    ToolInvocationState,
    WriteSnapshot,
    replay_operation,
    replay_operation_with_specs,
)
from .operation_store import (
    ClaimLease,
    OperationEventStore,
    OperationStoreConflictError,
    OperationStoreFencedClaimLostError,
    fenced_claim_resource_id,
    operation_last_sequence,
)


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    kind: str
    request_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    stored_result: dict[str, Any] | None = None
    approval_id: str | None = None
    write_id: str | None = None
    request_policy: ModelRequestPolicy | None = None
    expected_replay_policy: str | None = None
    expected_tool_contract_digest: str | None = None
    # Monotonic Operation-Recovery ownership epoch. External adapters should
    # persist/compare this token and reject lower, stale generations.
    fencing_token: int | None = None
    # Generation is only monotonic inside this canonical Claim resource.
    # Downstream fencing must compare the pair, never the integer alone.
    fencing_scope: str | None = None

    def __post_init__(self) -> None:
        if (self.fencing_token is None) != (self.fencing_scope is None):
            raise ValueError(
                "RecoveryAction fencing_token 与 fencing_scope 必须同时提供"
            )


@dataclass(frozen=True, slots=True)
class OperationRecoveryPlan:
    operation: OperationState
    actions: tuple[RecoveryAction, ...]


class OperationRecoveryPlanner:
    """只根据持久事实制定恢复动作，不执行模型或工具。"""

    def plan(self, operation: OperationState) -> OperationRecoveryPlan:
        if operation.phase in {"completed", "failed", "cancelled"}:
            return OperationRecoveryPlan(operation, ())

        guarded_actions = self._approval_write_actions(operation)
        if guarded_actions:
            return OperationRecoveryPlan(operation, guarded_actions)
        if (
            operation.phase == "waiting_approval"
            and not operation.approvals
            and not operation.writes
        ):
            return OperationRecoveryPlan(
                operation,
                (
                    RecoveryAction(
                        kind="manual_intervention",
                        reason=(
                            "Operation 标记 waiting_approval 但缺少 Approval/Write "
                            "持久事实，禁止恢复执行 Tool"
                        ),
                    ),
                ),
            )

        pending_requests = [
            request
            for request in operation.model_requests.values()
            if request.phase == "started"
        ]
        if pending_requests:
            pending = pending_requests[-1]
            if pending.policy is None:
                return OperationRecoveryPlan(
                    operation,
                    (
                        RecoveryAction(
                            kind="manual_intervention",
                            request_id=pending.request_id,
                            reason=(
                                "未完成 Model Request 缺少持久化请求策略，"
                                "禁止使用全部工具和 auto 策略恢复"
                            ),
                        ),
                    ),
                )
            return OperationRecoveryPlan(
                operation,
                (
                    RecoveryAction(
                        kind="retry_model_request",
                        request_id=pending.request_id,
                        request_policy=pending.policy,
                        reason="模型请求已开始但没有完整 Assistant Message",
                    ),
                ),
            )

        last_assistant = next(
            (
                message
                for message in reversed(operation.messages)
                if message.get("role") == "assistant"
            ),
            None,
        )
        if last_assistant is not None and last_assistant.get("stopReason") in {
            "error",
            "aborted",
            "length",
        }:
            return OperationRecoveryPlan(
                operation,
                (
                    RecoveryAction(
                        kind="manual_intervention",
                        reason=(
                            "最后 Model Request 未成功结束："
                            + str(last_assistant.get("stopReason"))
                        ),
                    ),
                ),
            )
        tool_results = {
            str(message.get("toolCallId"))
            for message in operation.messages
            if message.get("role") == "toolResult"
        }
        actions: list[RecoveryAction] = []
        if last_assistant is not None:
            calls = [
                block
                for block in last_assistant.get("content", [])
                if isinstance(block, dict) and block.get("type") == "toolCall"
            ]
            for call in calls:
                call_id = str(call.get("id", ""))
                if not call_id or call_id in tool_results:
                    continue
                invocation = operation.tools.get(call_id)
                if invocation is None:
                    actions.append(
                        RecoveryAction(
                            kind="manual_intervention",
                            tool_call_id=call_id,
                            tool_name=str(call.get("name", "")),
                            arguments=dict(call.get("arguments", {})),
                            reason=(
                                "Assistant Tool Call 已持久化，但缺少可信 Dispatch "
                                "Intent；无法验证 Replay Policy 与 Action Binding"
                            ),
                        )
                    )
                    continue
                actions.append(self._tool_action(invocation))

        if actions:
            return OperationRecoveryPlan(operation, tuple(actions))
        if not operation.messages:
            return OperationRecoveryPlan(
                operation,
                (
                    RecoveryAction(
                        kind="manual_intervention",
                        reason="Operation 没有可恢复消息 Context",
                    ),
                ),
            )
        last_role = operation.messages[-1].get("role")
        if last_role in {"user", "toolResult"}:
            policy = operation.active_model_policy
            if (
                last_role == "toolResult"
                and policy is not None
                and policy.continuation_policy is not None
            ):
                policy = policy.continuation_policy
            elif (
                last_role == "toolResult"
                and policy is not None
                and policy.tool_choice != "auto"
                and policy.tool_choice != "none"
            ):
                policy = None
            if policy is None:
                action = RecoveryAction(
                    kind="manual_intervention",
                    reason=("待继续 Context 缺少持久化请求策略，禁止扩大工具可见范围"),
                )
            else:
                action = RecoveryAction(
                    kind="continue_model",
                    request_policy=policy,
                    reason="Context 与模型请求策略均完整，可以继续请求模型",
                )
        else:
            action = RecoveryAction(
                kind="finish_operation",
                reason="最后 Assistant 已完成且没有未解决 Tool Call",
            )
        return OperationRecoveryPlan(operation, (action,))

    @staticmethod
    def _write_recovery_action(
        write: WriteSnapshot,
        *,
        kind: str,
        reason: str,
    ) -> RecoveryAction:
        return RecoveryAction(
            kind=kind,
            reason=reason,
            write_id=write.write_id,
            tool_call_id=write.tool_call_id,
            tool_name=write.tool_name,
            arguments=write.arguments,
            approval_id=write.approval_id,
        )

    @staticmethod
    def _approval_recovery_action(
        approval: ApprovalSnapshot,
        *,
        kind: str,
        reason: str,
        tool_name: str | None,
        arguments: dict[str, Any],
    ) -> RecoveryAction:
        return RecoveryAction(
            kind=kind,
            reason=reason,
            approval_id=approval.approval_id,
            tool_call_id=approval.tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            write_id=approval.write_id,
        )

    @staticmethod
    def _tool_recovery_action(
        invocation: ToolInvocationState,
        *,
        kind: str,
        reason: str,
        stored_result: dict[str, Any] | None = None,
    ) -> RecoveryAction:
        return RecoveryAction(
            kind=kind,
            reason=reason,
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            arguments=invocation.arguments,
            stored_result=stored_result,
            expected_replay_policy=invocation.replay_policy,
            expected_tool_contract_digest=invocation.security_contract_digest,
        )

    def _approval_write_actions(
        self,
        operation: OperationState,
    ) -> tuple[RecoveryAction, ...]:
        actions: list[RecoveryAction] = []
        for write in operation.writes.values():
            if write.state in {"submitting", "outcome_unknown", "reconciling"}:
                actions.append(
                    self._write_recovery_action(
                        write,
                        kind="reconcile_write",
                        reason="写操作已经提交或结果不确定，必须核对状态",
                    )
                )
            elif write.state == "waiting_approval":
                approval = (
                    operation.approvals.get(write.approval_id)
                    if write.approval_id is not None
                    else None
                )
                if approval is None:
                    actions.append(
                        self._write_recovery_action(
                            write,
                            kind="manual_intervention",
                            reason="Waiting Write 缺少关联 Approval",
                        )
                    )
                elif approval.state == "waiting":
                    actions.append(
                        self._write_recovery_action(
                            write,
                            kind="wait_for_approval",
                            reason="写操作仍在等待审批",
                        )
                    )
                elif approval.state == "approved":
                    actions.append(
                        self._write_recovery_action(
                            write,
                            kind="consume_approval",
                            reason="关联 Approval 已批准，等待可信 Consumer 消费",
                        )
                    )
                elif approval.state in {"consumed", "resume_started"}:
                    actions.append(
                        self._write_recovery_action(
                            write,
                            kind="resume_approved_write",
                            reason="关联 Approval 已消费，可以恢复 Write Claim",
                        )
                    )
                elif approval.state in {
                    "rejected",
                    "expired",
                    "resume_cancelled",
                }:
                    actions.append(
                        self._write_recovery_action(
                            write,
                            kind="finalize_rejected_approval",
                            reason=f"关联 Approval 状态为 {approval.state}",
                        )
                    )
                else:
                    actions.append(
                        self._write_recovery_action(
                            write,
                            kind="manual_intervention",
                            reason=(
                                "Waiting Write 的 Approval 状态无法自动恢复："
                                + approval.state
                            ),
                        )
                    )
            elif write.state in {"prepared", "approved"}:
                actions.append(
                    self._write_recovery_action(
                        write,
                        kind="resume_approved_write",
                        reason="写操作已准备完成，可以由 Write Runtime 恢复",
                    )
                )
        if actions:
            return tuple(actions)

        for approval in operation.approvals.values():
            if approval.resume_state == "unregistered" and approval.state == "consumed":
                # 普通 WriteOperation Approval 已消费，无独立 Resume Workflow。
                continue
            action = approval.action
            try:
                envelope = DurableActionEnvelope.from_dict(action)
            except DurableActionEnvelopeError:
                envelope = None
            tool_name = (
                envelope.tool_name
                if envelope is not None
                else str(action.get("tool", "")) or None
            )
            arguments = (
                dict(envelope.arguments)
                if envelope is not None
                else dict(action.get("arguments", {}))
            )
            if approval.state == "waiting":
                actions.append(
                    self._approval_recovery_action(
                        approval,
                        kind="wait_for_approval",
                        reason="Approval 仍在等待人工批准",
                        tool_name=tool_name,
                        arguments=arguments,
                    )
                )
            elif approval.state == "approved":
                actions.append(
                    self._approval_recovery_action(
                        approval,
                        kind="consume_approval",
                        reason="Approval 已批准，等待可信 Consumer 消费",
                        tool_name=tool_name,
                        arguments=arguments,
                    )
                )
            elif approval.state in {"consumed", "resume_started"}:
                actions.append(
                    self._approval_recovery_action(
                        approval,
                        kind="resume_approved_write",
                        reason="Approval 已消费，可以恢复已批准写操作",
                        tool_name=tool_name,
                        arguments=arguments,
                    )
                )
            elif approval.state in {"rejected", "expired"}:
                actions.append(
                    self._approval_recovery_action(
                        approval,
                        kind="finalize_rejected_approval",
                        reason=f"Approval 状态为 {approval.state}",
                        tool_name=tool_name,
                        arguments=arguments,
                    )
                )
            elif approval.state == "resume_failed":
                actions.append(
                    self._approval_recovery_action(
                        approval,
                        kind="manual_intervention",
                        reason="Approval Resume 已失败，需要人工核对",
                        tool_name=tool_name,
                        arguments=arguments,
                    )
                )
        return tuple(actions)

    def _tool_action(self, invocation: ToolInvocationState) -> RecoveryAction:
        if invocation.phase == "completed":
            return self._tool_recovery_action(
                invocation,
                kind="materialize_tool_result",
                stored_result=invocation.result,
                reason="工具结果已持久化，但 ToolResult Message 尚未写入 Context",
            )
        if invocation.phase == "outcome_unknown":
            return self._tool_recovery_action(
                invocation,
                kind="reconcile_tool",
                reason="写操作结果不确定，必须核对状态，不能直接重放",
            )
        if invocation.phase == "intent_recorded":
            return self._tool_recovery_action(
                invocation,
                kind="execute_tool",
                reason="工具尚未进入 Dispatch，可以安全执行",
            )
        if invocation.phase == "dispatch_started":
            if invocation.replay_policy == "safe":
                if invocation.security_contract_digest is None:
                    return self._tool_recovery_action(
                        invocation,
                        kind="manual_intervention",
                        reason=(
                            "Safe Replay 缺少持久 Tool Security Contract；"
                            "无法证明当前 Handler 与原 Dispatch 相同"
                        ),
                    )
                return self._tool_recovery_action(
                    invocation,
                    kind="replay_safe_tool",
                    reason="工具声明 replay_policy=safe",
                )
            return self._tool_recovery_action(
                invocation,
                kind="reconcile_tool",
                reason="工具已进入 Dispatch 且不可安全重放",
            )
        return self._tool_recovery_action(
            invocation,
            kind="manual_intervention",
            reason="未知 Tool 状态",
        )


@dataclass(frozen=True, slots=True)
class RecoveryCallbacks:
    request_model: Callable[
        [list[dict[str, Any]], ModelRequestPolicy],
        Awaitable[dict[str, Any]],
    ]
    execute_tool: Callable[[RecoveryAction], Awaitable[dict[str, Any]]]
    reconcile_tool: Callable[[RecoveryAction], Awaitable[dict[str, Any]]]
    consume_approval: Callable[[RecoveryAction], Awaitable[Any]] | None = None
    resume_write: Callable[[RecoveryAction], Awaitable[Any]] | None = None
    reconcile_write: Callable[[RecoveryAction], Awaitable[Any]] | None = None
    request_model_with_context: (
        Callable[
            [list[dict[str, Any]], ModelRequestPolicy, dict[str, str]],
            Awaitable[dict[str, Any]],
        ]
        | None
    ) = None


@dataclass(frozen=True, slots=True)
class RecoveryExecutionResult:
    operation: OperationState
    status: str
    remaining_actions: tuple[RecoveryAction, ...] = ()


@dataclass(frozen=True, slots=True)
class _RecoveryClaim:
    lease: ClaimLease
    lost: asyncio.Event


class DurableSessionRecovery:
    """加载、规划并通过 Host 回调安全恢复一个 Operation。"""

    def __init__(
        self,
        store: OperationEventStore,
        *,
        claim_lease_seconds: float = 300,
        renew_interval: float | None = None,
    ) -> None:
        if claim_lease_seconds <= 0:
            raise ValueError("claim_lease_seconds 必须大于 0")
        resolved_renew_interval = (
            renew_interval
            if renew_interval is not None
            else min(60.0, claim_lease_seconds / 3)
        )
        if (
            resolved_renew_interval <= 0
            or resolved_renew_interval >= claim_lease_seconds
        ):
            raise ValueError("renew_interval 必须大于 0 且小于 Lease 时长")
        self.store = store
        self.planner = OperationRecoveryPlanner()
        self.claim_lease_seconds = claim_lease_seconds
        self.renew_interval = resolved_renew_interval

    async def plan(
        self,
        *,
        session_id: str,
        operation_id: str,
    ) -> OperationRecoveryPlan:
        events = await self.store.load(
            session_id=session_id,
            operation_id=operation_id,
        )
        return self.planner.plan(replay_operation(events))

    async def resume(
        self,
        *,
        session_id: str,
        operation_id: str,
        callbacks: RecoveryCallbacks,
        max_cycles: int = 20,
    ) -> RecoveryExecutionResult:
        """使用跨进程 Lease Claim 串行恢复同一 Operation。"""

        owner_token = str(uuid4())
        resource_id = fenced_claim_resource_id(
            "operation_recovery",
            session_id=session_id,
            operation_id=operation_id,
        )
        acquire_fenced = getattr(self.store, "acquire_fenced_claim", None)
        if not callable(acquire_fenced):
            raise RuntimeError("Operation Recovery Store 缺少 Fenced Claim Generation")
        lease = await acquire_fenced(
            "operation_recovery",
            resource_id,
            owner_token,
            lease_seconds=self.claim_lease_seconds,
        )
        if lease is None:
            plan = await self.plan(
                session_id=session_id,
                operation_id=operation_id,
            )
            return RecoveryExecutionResult(
                plan.operation,
                "recovery_claimed",
                plan.actions,
            )
        claim = _RecoveryClaim(lease, asyncio.Event())
        heartbeat = asyncio.create_task(
            self._renew_claim(claim),
            name=f"operation-recovery-heartbeat:{resource_id}",
        )
        try:
            return await self._resume_claimed(
                session_id=session_id,
                operation_id=operation_id,
                callbacks=callbacks,
                max_cycles=max_cycles,
                claim=claim,
                recovery_run_id=str(uuid4()),
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            await self.store.release_fenced_claim(lease)

    async def _resume_claimed(
        self,
        *,
        session_id: str,
        operation_id: str,
        callbacks: RecoveryCallbacks,
        max_cycles: int,
        claim: _RecoveryClaim,
        recovery_run_id: str,
    ) -> RecoveryExecutionResult:
        for _ in range(max_cycles):
            plan = await self.plan(
                session_id=session_id,
                operation_id=operation_id,
            )
            if not plan.actions:
                return RecoveryExecutionResult(plan.operation, plan.operation.phase)
            if any(action.kind == "manual_intervention" for action in plan.actions):
                return RecoveryExecutionResult(
                    plan.operation,
                    "manual_intervention",
                    plan.actions,
                )
            if any(action.kind == "wait_for_approval" for action in plan.actions):
                return RecoveryExecutionResult(
                    plan.operation,
                    "waiting_approval",
                    plan.actions,
                )
            if (
                any(action.kind == "consume_approval" for action in plan.actions)
                and callbacks.consume_approval is None
            ):
                return RecoveryExecutionResult(
                    plan.operation,
                    "approval_consumer_required",
                    plan.actions,
                )
            if (
                any(action.kind == "resume_approved_write" for action in plan.actions)
                and callbacks.resume_write is None
            ):
                return RecoveryExecutionResult(
                    plan.operation,
                    "approved_write_runtime_required",
                    plan.actions,
                )
            if (
                any(action.kind == "reconcile_write" for action in plan.actions)
                and callbacks.reconcile_write is None
            ):
                return RecoveryExecutionResult(
                    plan.operation,
                    "write_reconciliation_required",
                    plan.actions,
                )

            for action in plan.actions:
                if action.kind in {"retry_model_request", "continue_model"}:
                    await self._request_model(
                        plan.operation,
                        action,
                        callbacks,
                        claim,
                        recovery_run_id,
                    )
                elif action.kind in {"execute_tool", "replay_safe_tool"}:
                    await self._execute_tool(
                        plan.operation,
                        action,
                        callbacks,
                        claim,
                    )
                elif action.kind == "reconcile_tool":
                    await self._reconcile_tool(
                        plan.operation,
                        action,
                        callbacks,
                        claim,
                    )
                elif action.kind == "materialize_tool_result":
                    await self._materialize_result(
                        plan.operation,
                        action,
                        claim,
                    )
                elif action.kind == "consume_approval":
                    consume_approval = callbacks.consume_approval
                    if consume_approval is None:
                        return RecoveryExecutionResult(
                            plan.operation,
                            "approval_consumer_required",
                            plan.actions,
                        )
                    await self._assert_claim(claim)
                    await consume_approval(self._fenced_action(action, claim))
                    await self._assert_claim(claim)
                elif action.kind == "resume_approved_write":
                    resume_write = callbacks.resume_write
                    if resume_write is None:
                        return RecoveryExecutionResult(
                            plan.operation,
                            "approved_write_runtime_required",
                            plan.actions,
                        )
                    await self._assert_claim(claim)
                    await resume_write(self._fenced_action(action, claim))
                    await self._assert_claim(claim)
                elif action.kind == "reconcile_write":
                    reconcile_write = callbacks.reconcile_write
                    if reconcile_write is None:
                        return RecoveryExecutionResult(
                            plan.operation,
                            "write_reconciliation_required",
                            plan.actions,
                        )
                    await self._assert_claim(claim)
                    await reconcile_write(self._fenced_action(action, claim))
                    await self._assert_claim(claim)
                elif action.kind == "finalize_rejected_approval":
                    await self._finalize_rejected_approval(
                        plan.operation,
                        action,
                        claim,
                    )
                elif action.kind == "finish_operation":
                    await self._finish_operation(
                        plan.operation,
                        "completed",
                        claim,
                    )
                else:
                    return RecoveryExecutionResult(
                        plan.operation,
                        "manual_intervention",
                        (action,),
                    )
        plan = await self.plan(session_id=session_id, operation_id=operation_id)
        return RecoveryExecutionResult(
            plan.operation,
            "max_recovery_cycles",
            plan.actions,
        )

    async def _finish_operation(
        self,
        operation: OperationState,
        outcome: str,
        claim: _RecoveryClaim,
    ) -> None:
        specs = [("operation_finished", {"outcome": outcome})]
        await self._commit_specs(operation, specs, claim)

    async def _request_model(
        self,
        operation: OperationState,
        action: RecoveryAction,
        callbacks: RecoveryCallbacks,
        claim: _RecoveryClaim,
        recovery_run_id: str,
    ) -> None:
        if action.request_policy is None:
            raise RuntimeError("恢复模型请求缺少持久化策略")
        if (
            self.store.supports_cross_process_claims
            and callbacks.request_model_with_context is None
        ):
            raise RuntimeError(
                "跨进程 Operation Recovery 必须使用能接收 Fencing Identity "
                "的 request_model_with_context Callback"
            )
        request_id = str(uuid4())
        specs: list[tuple[str, dict[str, Any]]] = []
        if action.kind == "retry_model_request":
            if action.request_id is None:
                raise RuntimeError("恢复未完成模型请求时缺少 Request ID")
            specs.append(
                (
                    "model_request_failed",
                    {
                        "requestId": action.request_id,
                        "errorCode": "process_interrupted",
                        "recovery": True,
                    },
                )
            )
        specs.append(
            (
                "model_request_started",
                {
                    "requestId": request_id,
                    "requestPolicy": action.request_policy.to_dict(),
                    "recovery": True,
                    "fencingToken": claim.lease.fencing_token,
                },
            )
        )
        started = await self._commit_specs(operation, specs, claim)
        try:
            identity = {
                "requestId": request_id,
                "sessionId": operation.session_id,
                "operationId": operation.operation_id,
                "runId": recovery_run_id,
                "source": "recovery",
                "fencingToken": str(claim.lease.fencing_token),
                "fencingScope": claim.lease.resource_id,
            }
            if callbacks.request_model_with_context is not None:
                message = await callbacks.request_model_with_context(
                    list(started.messages),
                    action.request_policy,
                    identity,
                )
            else:
                message = await callbacks.request_model(
                    list(started.messages),
                    action.request_policy,
                )
        except asyncio.CancelledError:
            # Started 是合法恢复锚点；下一 Worker 会原子地将它标记为
            # process_interrupted 并创建新的 Request，不能在取消清理中裸写。
            raise
        except Exception:
            await self._fail_model_request_if_started(
                operation,
                request_id,
                "recovery_model_error",
                claim,
            )
            raise
        try:
            validate_recoverable_model_response(
                message,
                action.request_policy,
            )
        except ModelRequestPolicyError:
            await self._fail_model_request_if_started(
                operation,
                request_id,
                "invalid_recovery_model_response",
                claim,
            )
            raise
        await self._commit_specs(
            operation,
            [
                (
                    "model_request_completed",
                    {
                        "requestId": request_id,
                        "message": message,
                        "recovery": True,
                    },
                )
            ],
            claim,
        )

    async def _execute_tool(
        self,
        operation: OperationState,
        action: RecoveryAction,
        callbacks: RecoveryCallbacks,
        claim: _RecoveryClaim,
    ) -> None:
        specs: list[tuple[str, dict[str, Any]]] = []
        if action.tool_call_id not in operation.tools:
            specs.append(
                (
                    "tool_intent_recorded",
                    {
                        "toolCallId": action.tool_call_id,
                        "toolName": action.tool_name,
                        "arguments": action.arguments,
                        "replayPolicy": "never",
                        "recovery": True,
                    },
                )
            )
        specs.append(
            (
                "tool_dispatch_started",
                {
                    "toolCallId": action.tool_call_id,
                    "recovery": True,
                    "fencingToken": claim.lease.fencing_token,
                },
            )
        )
        await self._commit_specs(operation, specs, claim)
        result_message = await callbacks.execute_tool(
            self._fenced_action(action, claim)
        )
        await self._store_tool_result(
            operation,
            action,
            result_message,
            event_type="tool_completed",
            claim=claim,
        )

    async def _reconcile_tool(
        self,
        operation: OperationState,
        action: RecoveryAction,
        callbacks: RecoveryCallbacks,
        claim: _RecoveryClaim,
    ) -> None:
        await self._commit_specs(
            operation,
            [
                (
                    "tool_reconcile_started",
                    {
                        "toolCallId": action.tool_call_id,
                        "recovery": True,
                        "fencingToken": claim.lease.fencing_token,
                    },
                )
            ],
            claim,
        )
        result_message = await callbacks.reconcile_tool(
            self._fenced_action(action, claim)
        )
        await self._store_tool_result(
            operation,
            action,
            result_message,
            event_type="tool_reconciled",
            claim=claim,
        )

    async def _finalize_rejected_approval(
        self,
        operation: OperationState,
        action: RecoveryAction,
        claim: _RecoveryClaim,
    ) -> None:
        if (
            action.approval_id is None
            or action.tool_call_id is None
            or action.tool_name is None
        ):
            raise RuntimeError("Rejected Approval 缺少 Tool Call 关联")
        result_message = {
            "role": "toolResult",
            "toolCallId": action.tool_call_id,
            "toolName": action.tool_name,
            "content": [
                {
                    "type": "text",
                    "text": "工具调用未执行：Approval 已拒绝或过期。",
                }
            ],
            "details": {
                "code": "approval_not_granted",
                "approvalId": action.approval_id,
                "synthetic": True,
            },
            "isError": True,
        }
        for _ in range(20):
            await self._assert_claim(claim)
            events = await self.store.load(
                session_id=operation.session_id,
                operation_id=operation.operation_id,
            )
            current = replay_operation(events)
            specs: list[tuple[str, dict[str, Any]]] = []
            invocation = current.tools.get(action.tool_call_id)
            if invocation is None:
                specs.append(
                    (
                        "tool_intent_recorded",
                        {
                            "toolCallId": action.tool_call_id,
                            "toolName": action.tool_name,
                            "arguments": action.arguments,
                            "replayPolicy": "never",
                            "recovery": True,
                        },
                    )
                )
            if invocation is None or invocation.phase != "completed":
                specs.append(
                    (
                        "tool_completed",
                        {
                            "toolCallId": action.tool_call_id,
                            "result": {
                                "content": result_message["content"],
                                "details": result_message["details"],
                                "isError": True,
                            },
                            "recovery": True,
                        },
                    )
                )
            has_result_message = any(
                message.get("role") == "toolResult"
                and message.get("toolCallId") == action.tool_call_id
                for message in current.messages
            )
            if not has_result_message:
                specs.append(
                    (
                        "message_appended",
                        {"message": result_message, "recovery": True},
                    )
                )
            approval = current.approvals.get(action.approval_id)
            if approval is None:
                raise RuntimeError("Rejected Approval 已从 Operation 丢失")
            if approval.state in {"rejected", "expired"}:
                specs.append(
                    (
                        "approval_resume_cancelled",
                        {
                            "approvalId": action.approval_id,
                            "reason": "rejected_or_expired",
                            "recovery": True,
                        },
                    )
                )
            if action.write_id is not None:
                write = current.writes.get(action.write_id)
                if write is not None and write.state == "waiting_approval":
                    specs.append(
                        (
                            "write_failed",
                            {
                                "writeId": action.write_id,
                                "reason": "approval_not_granted",
                                "result": {
                                    "status": "rejected",
                                    "approvalId": action.approval_id,
                                },
                                "recovery": True,
                            },
                        )
                    )
            policy = current.active_model_policy
            if policy is not None and policy.continuation_policy is not None:
                specs.append(
                    (
                        "model_policy_selected",
                        {
                            "policy": policy.continuation_policy.to_dict(),
                            "recovery": True,
                        },
                    )
                )
            if not specs:
                return
            replay_operation_with_specs(events, specs)
            try:
                await self.store.append_batch_if_fenced_claim(
                    operation.session_id,
                    operation.operation_id,
                    specs,
                    claim.lease,
                    renew_lease_seconds=self.claim_lease_seconds,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreFencedClaimLostError:
                claim.lost.set()
                raise RuntimeError("Operation Recovery Lease 已丢失，禁止提交后续状态")
            except OperationStoreConflictError:
                continue
            return
        raise RuntimeError("Rejected Approval 终态提交并发冲突")

    async def _materialize_result(
        self,
        operation: OperationState,
        action: RecoveryAction,
        claim: _RecoveryClaim,
    ) -> None:
        result = action.stored_result or {}
        message = {
            "role": "toolResult",
            "toolCallId": action.tool_call_id,
            "toolName": action.tool_name,
            "content": result.get("content", []),
            "details": result.get("details", {}),
            "isError": bool(result.get("isError")),
        }
        specs: list[tuple[str, dict[str, Any]]] = [
            ("message_appended", {"message": message, "recovery": True})
        ]
        specs.extend(self._continuation_policy_specs(operation))
        await self._commit_specs(
            operation,
            specs,
            claim,
        )

    async def _store_tool_result(
        self,
        operation: OperationState,
        action: RecoveryAction,
        result_message: dict[str, Any],
        *,
        event_type: str,
        claim: _RecoveryClaim,
    ) -> None:
        _validate_recovery_tool_result(action, result_message)
        result = {
            "content": result_message.get("content", []),
            "details": result_message.get("details", {}),
            "isError": bool(result_message.get("isError")),
        }
        specs: list[tuple[str, dict[str, Any]]] = [
            (
                event_type,
                {
                    "toolCallId": action.tool_call_id,
                    "result": result,
                    "recovery": True,
                    **(
                        {"fencingToken": claim.lease.fencing_token}
                        if event_type == "tool_reconciled"
                        else {}
                    ),
                },
            ),
            (
                "message_appended",
                {"message": result_message, "recovery": True},
            ),
        ]
        specs.extend(self._continuation_policy_specs(operation))
        await self._commit_specs(
            operation,
            specs,
            claim,
        )

    @staticmethod
    def _continuation_policy_specs(
        operation: OperationState,
    ) -> list[tuple[str, dict[str, Any]]]:
        policy = operation.active_model_policy
        if policy is None or policy.continuation_policy is None:
            return []
        return [
            (
                "model_policy_selected",
                {
                    "policy": policy.continuation_policy.to_dict(),
                    "recovery": True,
                },
            )
        ]

    async def _fail_model_request_if_started(
        self,
        operation: OperationState,
        request_id: str,
        error_code: str,
        claim: _RecoveryClaim,
    ) -> None:
        events = await self.store.load(
            session_id=operation.session_id,
            operation_id=operation.operation_id,
        )
        current = replay_operation(events)
        request = current.model_requests.get(request_id)
        if request is None:
            raise RuntimeError("恢复 Model Request 已从 Operation 丢失")
        if request.phase != "started":
            return
        await self._commit_specs(
            operation,
            [
                (
                    "model_request_failed",
                    {
                        "requestId": request_id,
                        "errorCode": error_code,
                        "recovery": True,
                    },
                )
            ],
            claim,
        )

    async def _commit_specs(
        self,
        operation: OperationState,
        specs: list[tuple[str, dict[str, Any]]],
        claim: _RecoveryClaim,
    ) -> OperationState:
        """在仍持有 Recovery Claim 时，以 Reducer + CAS 提交状态转换。"""

        for _ in range(20):
            await self._assert_claim(claim)
            events = await self.store.load(
                session_id=operation.session_id,
                operation_id=operation.operation_id,
            )
            replay_operation_with_specs(events, specs)
            try:
                appended = await self.store.append_batch_if_fenced_claim(
                    operation.session_id,
                    operation.operation_id,
                    specs,
                    claim.lease,
                    renew_lease_seconds=self.claim_lease_seconds,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreFencedClaimLostError:
                claim.lost.set()
                raise RuntimeError("Operation Recovery Lease 已丢失，禁止提交后续状态")
            except OperationStoreConflictError:
                continue
            return replay_operation([*events, *appended])
        raise RuntimeError("Operation Recovery 状态提交并发冲突")

    async def _renew_claim(self, claim: _RecoveryClaim) -> None:
        while True:
            try:
                renewed = await self.store.renew_fenced_claim(
                    claim.lease,
                    lease_seconds=self.claim_lease_seconds,
                )
            except Exception:
                claim.lost.set()
                return
            if not renewed:
                claim.lost.set()
                return
            await asyncio.sleep(self.renew_interval)

    async def _assert_claim(self, claim: _RecoveryClaim) -> None:
        if claim.lost.is_set():
            raise RuntimeError("Operation Recovery Lease 已丢失，禁止提交后续状态")
        try:
            # Refresh at every durable/external boundary in addition to the
            # background heartbeat.  The renewal still matches the exact
            # generation, so this cannot resurrect an expired/taken-over ABA
            # lease; it only avoids losing a live lease while SQLite work or a
            # busy event loop delays the heartbeat task.
            held = await self.store.renew_fenced_claim(
                claim.lease,
                lease_seconds=self.claim_lease_seconds,
            )
        except Exception as error:
            claim.lost.set()
            raise RuntimeError(
                "Operation Recovery Lease 无法确认，禁止提交后续状态"
            ) from error
        if not held:
            claim.lost.set()
            raise RuntimeError("Operation Recovery Lease 已丢失，禁止提交后续状态")

    @staticmethod
    def _fenced_action(
        action: RecoveryAction,
        claim: _RecoveryClaim,
    ) -> RecoveryAction:
        """Bind every external recovery callback to the owned generation."""

        return replace(
            action,
            fencing_token=claim.lease.fencing_token,
            fencing_scope=claim.lease.resource_id,
        )


def _validate_recovery_tool_result(
    action: RecoveryAction,
    message: dict[str, Any],
) -> None:
    """Recovery Callback 只能闭合当前计划中的那个 Tool Call。"""

    if not isinstance(message, dict) or message.get("role") != "toolResult":
        raise RuntimeError("Recovery Tool Callback 必须返回 ToolResult Message")
    if message.get("toolCallId") != action.tool_call_id:
        raise RuntimeError("Recovery ToolResult 的 Tool Call ID 与计划不一致")
    if message.get("toolName") != action.tool_name:
        raise RuntimeError("Recovery ToolResult 的 Tool Name 与计划不一致")
    if not isinstance(message.get("content"), list):
        raise RuntimeError("Recovery ToolResult content 必须是数组")
    if not isinstance(message.get("details", {}), dict):
        raise RuntimeError("Recovery ToolResult details 必须是对象")
    if type(message.get("isError")) is not bool:
        raise RuntimeError("Recovery ToolResult isError 必须是布尔值")
