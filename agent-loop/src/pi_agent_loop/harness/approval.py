"""Durable Host 的审批规划与恢复工作流。"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ..approval.state_machine import ApprovalError, build_approval_request_event
from ..durable_action import DurableActionEnvelope
from ..messages import assistant_message, user_message
from ..model_policy import ModelRequestPolicy, ModelRequestPolicyError
from ..security import VerifiedIdentity
from ..session.operation_state import replay_operation, replay_operation_with_specs
from ..session.operation_store import (
    ClaimLease,
    OperationStoreConflictError,
    OperationStoreFencedClaimLostError,
    operation_last_sequence,
)
from ..writes import (
    hash_idempotency_key,
    hash_scoped_idempotency_key,
    idempotency_key_matches,
    is_outcome_unknown_error,
)
from .approval_gateway import _callback_accepts_keyword, invoke_fenced_callback


@dataclass(frozen=True, slots=True)
class DurableApprovalAction:
    """批量审批中的一个独立、精确写动作。"""

    tool_name: str
    arguments: dict[str, Any]
    idempotency_key: str
    action_summary: str
    required_role: str = "approver"
    entity_id: str | None = None
    expected_entity_version: int | None = None
    business_preconditions: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.tool_name or not self.idempotency_key:
            raise ValueError("批量 Approval Action 缺少工具名或 Idempotency Key")
        if not self.action_summary or not self.required_role:
            raise ValueError("批量 Approval Action 缺少摘要或审批角色")
        if not isinstance(self.arguments, dict):
            raise TypeError("批量 Approval Action arguments 必须是对象")
        # 复用统一 Envelope 校验并冻结调用者传入的可变业务条件。
        validated = DurableActionEnvelope(
            operation_id="validation",
            tool_call_id="validation",
            tool_name=self.tool_name,
            arguments=self.arguments,
            write_id="validation",
            entity_id=self.entity_id,
            expected_entity_version=self.expected_entity_version,
            business_preconditions=self.business_preconditions,
        )
        object.__setattr__(self, "arguments", validated.arguments)
        object.__setattr__(
            self,
            "business_preconditions",
            validated.business_preconditions,
        )


@dataclass(frozen=True, slots=True)
class DurableApprovalBatchItem:
    approval_id: str
    envelope: DurableActionEnvelope


@dataclass(frozen=True, slots=True)
class DurableApprovalBatch:
    operation_id: str
    items: tuple[DurableApprovalBatchItem, ...]
    batch_id: str | None = None
    batch_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.operation_id or not self.items:
            raise ValueError("Durable Approval Batch 缺少 Operation 或 Item")
        approval_ids = [item.approval_id for item in self.items]
        tool_call_ids = [item.envelope.tool_call_id for item in self.items]
        write_ids = [item.envelope.write_id for item in self.items]
        if (
            len(approval_ids) != len(set(approval_ids))
            or len(tool_call_ids) != len(set(tool_call_ids))
            or len(write_ids) != len(set(write_ids))
        ):
            raise ValueError("Durable Approval Batch 的关联 ID 必须逐项唯一")
        if any(
            item.envelope.operation_id != self.operation_id for item in self.items
        ):
            raise ValueError("Durable Approval Batch Item 不属于当前 Operation")
        if (self.batch_id is None) != (self.batch_hash is None):
            raise ValueError("Durable Approval Batch ID 和 Hash 必须同时存在")
        if self.batch_id is not None:
            if not self.batch_id or not self.batch_hash:
                raise ValueError("Durable Approval Batch ID 或 Hash 不能为空")
            expected = _approval_batch_digest(
                self.operation_id,
                self.batch_id,
                self.items,
            )
            if self.batch_hash != expected:
                raise ValueError("Durable Approval Batch Hash 不匹配")

    @property
    def approval_ids(self) -> tuple[str, ...]:
        return tuple(item.approval_id for item in self.items)


@dataclass(frozen=True, slots=True)
class DurableApprovalExecution:
    approval_id: str
    approver: VerifiedIdentity
    consumer: VerifiedIdentity
    idempotency_key: str
    write_handler: Callable[..., Any] | None = None
    context_handler: Callable[..., Any] | None = None

    def __post_init__(self) -> None:
        _require_exactly_one_write_handler(
            self.write_handler,
            self.context_handler,
        )


@dataclass(frozen=True, slots=True)
class DurableApprovalItemResult:
    approval_id: str
    tool_name: str
    status: str
    result: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DurableApprovalBatchResult:
    operation_id: str
    items: tuple[DurableApprovalItemResult, ...]
    pending_approval_ids: tuple[str, ...] = ()


class DurableApprovalWorkflow:
    """把 Approval Plan/Resume 从 Host Facade 中移出的应用服务。"""

    def __init__(self, host: Any) -> None:
        self.host = host

    async def request_many(
        self,
        *,
        text: str,
        actions: tuple[DurableApprovalAction, ...],
        requester: VerifiedIdentity,
    ) -> DurableApprovalBatch:
        """经 Host Facade 原子规划多个独立写动作。

        保留该入口是为了兼容早期调用方；它不再直接执行工作流，避免绕过
        Host 生命周期和 Session Writer Lease。新代码应优先调用
        ``DurableAgentHost.request_approval_batch``。
        """

        return await self.host.request_approval_batch(
            text=text,
            actions=actions,
            requester=requester,
        )

    async def _request_many_impl(
        self,
        *,
        text: str,
        actions: tuple[DurableApprovalAction, ...],
        requester: VerifiedIdentity,
    ) -> DurableApprovalBatch:
        """Host 边界内原子规划；任何一项校验失败时持久事件为零。"""

        host = self.host
        requester = host.approvals.assert_trusted_identity(
            requester,
            purpose="Approval batch requester",
        )
        if not actions:
            raise ValueError("批量 Approval 至少需要一个 Action")
        operation_id = host.operation_recorder.operation_id
        if operation_id is None:
            raise RuntimeError("批量 Approval 必须属于活动 Durable Operation")
        if not host.operation_store.supports_atomic_transactions:
            raise RuntimeError("批量 Approval 必须使用支持原子事务的 Operation Store")
        if host.routed_agent is None:
            raise RuntimeError("批量 Approval 参数校验缺少 Routed Agent")

        if len({action.idempotency_key for action in actions}) != len(actions):
            raise ValueError("批量 Approval 的 Idempotency Key 必须逐项唯一")
        key_hashes = tuple(
            hash_scoped_idempotency_key(
                action.idempotency_key,
                session_id=host.session_id,
                principal_id=requester.principal_id,
                tool_name=action.tool_name,
            )
            for action in actions
        )
        existing_events = await host.operation_store.load()
        if any(
            event.type == "write_prepared"
            and event.session_id == host.session_id
            and event.data.get("requesterId") == requester.principal_id
            and any(
                event.data.get("toolName") == action.tool_name
                and event.data.get("idempotencyKeyHash")
                in {
                    key_hash,
                    hash_idempotency_key(action.idempotency_key),
                }
                for action, key_hash in zip(actions, key_hashes, strict=True)
            )
            for event in existing_events
        ):
            raise ValueError("批量 Approval 的 Idempotency Key 已被使用")

        tools = host.routed_agent.capabilities.tools_by_names(
            tuple(action.tool_name for action in actions)
        )
        validated: list[tuple[DurableApprovalAction, dict[str, Any]]] = []
        for action, tool in zip(actions, tools, strict=True):
            arguments = tool.validate_args(dict(action.arguments))
            if not isinstance(arguments, dict):
                raise TypeError("批量 Approval Tool 参数校验必须返回对象")
            validated.append((action, dict(arguments)))

        envelopes = tuple(
            DurableActionEnvelope(
                operation_id=operation_id,
                tool_call_id=str(uuid4()),
                tool_name=action.tool_name,
                arguments=arguments,
                write_id=str(uuid4()),
                entity_id=action.entity_id,
                expected_entity_version=action.expected_entity_version,
                business_preconditions=action.business_preconditions,
            )
            for action, arguments in validated
        )
        batch_id = str(uuid4())
        request_id = str(uuid4())
        visible_names = tuple(dict.fromkeys(item.tool_name for item in envelopes))
        policy = ModelRequestPolicy(
            visible_tool_names=visible_names,
            tool_choice="required",
            allowed_tool_names=visible_names,
            continuation_policy=ModelRequestPolicy.no_tools(),
        )
        planned_call = assistant_message(
            model=host.model,
            stop_reason="toolUse",
            content=[
                {
                    "type": "toolCall",
                    "id": envelope.tool_call_id,
                    "name": envelope.tool_name,
                    "arguments": envelope.arguments,
                }
                for envelope in envelopes
            ],
        )
        request_started_data: dict[str, Any] = {
            "requestId": request_id,
            "source": "host_approval_batch_plan",
            "requestPolicy": policy.to_dict(),
        }
        specs: list[tuple[str, dict[str, Any]]] = [
            ("message_appended", {"message": user_message(text)}),
            (
                "model_request_started",
                request_started_data,
            ),
            (
                "model_request_completed",
                {
                    "requestId": request_id,
                    "message": planned_call,
                    "source": "host_approval_batch_plan",
                },
            ),
            (
                "model_policy_selected",
                {"policy": ModelRequestPolicy.no_tools().to_dict()},
            ),
        ]
        items: list[DurableApprovalBatchItem] = []
        for index, ((action, arguments), envelope) in enumerate(
            zip(validated, envelopes, strict=True)
        ):
            approval_id, approval_data = build_approval_request_event(
                requester=requester,
                action=envelope.to_dict(),
                action_summary=action.action_summary,
                required_role=action.required_role,
            )
            items.append(DurableApprovalBatchItem(approval_id, envelope))
            specs.extend(
                [
                    (
                        "write_prepared",
                        {
                            "writeId": envelope.write_id,
                            "toolName": envelope.tool_name,
                            "arguments": arguments,
                            "actionHash": envelope.action_hash,
                            "idempotencyKeyHash": key_hashes[index],
                            "idempotencyHashVersion": 2,
                            "requesterId": requester.principal_id,
                            "requesterVerificationId": requester.verification_id,
                            "toolCallId": envelope.tool_call_id,
                            **_durable_action_binding(envelope),
                        },
                    ),
                    ("approval_requested", approval_data),
                    (
                        "approval_resume_registered",
                        {
                            "approvalId": approval_id,
                            "action": envelope.to_dict(),
                            "resumePayload": {"envelope": envelope.to_dict()},
                        },
                    ),
                    (
                        "write_waiting_approval",
                        {"writeId": envelope.write_id, "approvalId": approval_id},
                    ),
                    (
                        "tool_intent_recorded",
                        {
                            "toolCallId": envelope.tool_call_id,
                            "toolName": envelope.tool_name,
                            "arguments": arguments,
                            "replayPolicy": "never",
                            "source": "host_approval_batch_plan",
                        },
                    ),
                ]
            )

        batch_items = tuple(items)
        batch_hash = _approval_batch_digest(
            operation_id,
            batch_id,
            batch_items,
        )
        request_started_data["approvalBatch"] = _approval_batch_manifest(
            operation_id,
            batch_id,
            batch_hash,
            batch_items,
        )

        for _ in range(20):
            events = await host.operation_store.load(
                session_id=host.session_id,
                operation_id=operation_id,
            )
            replay_operation_with_specs(events, specs)
            try:
                await host.operation_store.append_batch_if_fenced_claim(
                    host.session_id,
                    operation_id,
                    specs,
                    host.session_writer_lease.claim_lease,
                    renew_lease_seconds=host.session_writer_lease.lease_seconds,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreFencedClaimLostError as error:
                raise RuntimeError(
                    "Session Writer Lease 已丢失，禁止规划 Approval Batch"
                ) from error
            except OperationStoreConflictError:
                continue
            return DurableApprovalBatch(
                operation_id,
                batch_items,
                batch_id=batch_id,
                batch_hash=batch_hash,
            )
        raise RuntimeError("批量 Approval 原子持久化发生并发冲突")

    async def approve_many(
        self,
        batch: DurableApprovalBatch,
        executions: tuple[DurableApprovalExecution, ...],
    ) -> DurableApprovalBatchResult:
        """经 Host Facade 全量确认并执行一批写动作。

        兼容入口也必须经过 Host 生命周期和租约围栏，不能通过公开的
        ``approval_workflow`` 属性绕过安全边界。
        """

        return await self.host.approve_approval_batch(batch, executions)

    async def _approve_many_impl(
        self,
        batch: DurableApprovalBatch,
        executions: tuple[DurableApprovalExecution, ...],
    ) -> DurableApprovalBatchResult:
        """Host 边界内全量确认；少一个确认时整批 Handler 调用为零。"""

        host = self.host
        batch = await self._load_canonical_batch(batch)
        expected_ids = batch.approval_ids
        supplied = {item.approval_id: item for item in executions}
        if len(supplied) != len(executions) or set(supplied) - set(expected_ids):
            raise ValueError("批量 Approval Execution 包含重复或未知 Approval ID")
        missing = tuple(item for item in expected_ids if item not in supplied)
        if missing:
            return DurableApprovalBatchResult(
                operation_id=batch.operation_id,
                items=(),
                pending_approval_ids=missing,
            )

        # 在任何业务 Handler 运行前验证所有 Envelope、幂等键、角色、自审和 TTL。
        records = []
        now_ms = int(time.time() * 1000)
        for item in batch.items:
            execution = supplied[item.approval_id]
            pending = await host.approval_resume.get_pending(item.approval_id)
            envelope = DurableActionEnvelope.from_dict(
                pending.resume_payload.get("envelope")
            )
            if envelope != item.envelope or envelope.operation_id != batch.operation_id:
                raise RuntimeError("批量 Approval Envelope 绑定不一致")
            approval = await host.approvals.get(item.approval_id)
            write = await host.writes.get(envelope.write_id)
            envelope.assert_execution(
                operation_id=write.operation_id,
                tool_call_id=write.tool_call_id or "",
                tool_name=write.tool_name,
                arguments=write.arguments,
                write_id=write.write_id,
                entity_id=write.entity_id,
                expected_entity_version=write.expected_entity_version,
                business_preconditions=write.business_preconditions,
                validate_business_binding=True,
            )
            if not idempotency_key_matches(write, execution.idempotency_key):
                raise PermissionError("批量 Approval 的 Idempotency Key 不匹配")
            if approval.state not in {"waiting", "approved"}:
                raise PermissionError(
                    f"批量 Approval 当前状态 {approval.state} 不能执行"
                )
            if approval.required_role not in execution.approver.roles:
                raise PermissionError("批量 Approval 审批人缺少所需角色")
            if approval.requester_id == execution.approver.principal_id:
                raise PermissionError("批量 Approval 禁止申请人自审")
            if (
                approval.state == "approved"
                and approval.approver_id != execution.approver.principal_id
            ):
                raise PermissionError("批量 Approval 的审批人和持久事实不一致")
            if approval.state == "waiting" and now_ms >= approval.expires_at:
                raise PermissionError("批量 Approval 已过期")
            records.append((item, execution, approval))

        for _item, execution, _approval in records:
            if (
                execution.write_handler is not None
                and not _callback_accepts_keyword(
                    execution.write_handler,
                    "fenced_claim",
                )
            ):
                raise ValueError(
                    "危险批量 Write Handler 必须显式接收 fenced_claim；"
                    "或改用 WriteExecutionContext context_handler"
                )

        # 全部 Grant 只改变审批事实，没有业务副作用。任何 Grant 失败都会在
        # 第一个 Handler 前停止，因此不会出现“前一项已扣款、后一项没获批”。
        for item, execution, approval in records:
            await host._verify_writer_lease()
            if approval.state == "waiting":
                await host.approvals.grant(
                    item.approval_id,
                    execution.approver,
                    fenced_claim=host.session_writer_lease.claim_lease,
                    renew_lease_seconds=host.session_writer_lease.lease_seconds,
                )
            await host._verify_writer_lease()

        results: list[DurableApprovalItemResult] = []
        persistence_pending = False
        for item, execution, _approval in records:
            await host._verify_writer_lease()
            envelope = item.envelope
            item_persistence_pending = False

            async def invoke_context(
                context,
                handler=execution.context_handler,
                legacy_handler=execution.write_handler,
            ):
                if handler is not None:
                    value = handler(context)
                    return await value if hasattr(value, "__await__") else value
                assert legacy_handler is not None
                return await invoke_fenced_callback(
                    legacy_handler,
                    context.arguments,
                    context.idempotency_key,
                    context.actor,
                    fencing_token=context.fencing_token,
                    fenced_claim=host.session_writer_lease.claim_lease,
                )

            try:
                write = await host.writes.execute(
                    envelope.write_id,
                    actor=execution.consumer,
                    idempotency_key=execution.idempotency_key,
                    context_handler=invoke_context,
                    approval_resume_id=item.approval_id,
                    fencing_token=host.session_writer_lease.fencing_token,
                    fenced_claim=host.session_writer_lease.claim_lease,
                    fenced_claim_lease_seconds=(
                        host.session_writer_lease.lease_seconds
                    ),
                )
            except Exception as error:
                unknown = is_outcome_unknown_error(error)
                try:
                    await self.materialize_write_tool_result(
                        batch.operation_id,
                        _failed_write_tool_result(envelope, error),
                    )
                    if not unknown:
                        await self._finish_batch_resume(
                            batch.operation_id,
                            item.approval_id,
                            succeeded=False,
                        )
                except Exception:
                    item_persistence_pending = True
                    persistence_pending = True
                results.append(
                    DurableApprovalItemResult(
                        item.approval_id,
                        envelope.tool_name,
                        "outcome_unknown" if unknown else "failed",
                    )
                )
                continue

            # Lease loss is a fencing failure, not a business failure. Keep a
            # successfully persisted Write recoverable and let the new owner
            # materialize its Tool Result instead of forging write_failed.
            await host._verify_writer_lease()

            tool_result = {
                "role": "toolResult",
                "toolCallId": envelope.tool_call_id,
                "toolName": envelope.tool_name,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(write.result or {}, ensure_ascii=False),
                    }
                ],
                "details": write.result or {},
                "isError": False,
            }
            try:
                await self.materialize_write_tool_result(
                    batch.operation_id,
                    tool_result,
                )
                await self._finish_batch_resume(
                    batch.operation_id,
                    item.approval_id,
                    succeeded=True,
                )
            except Exception:
                item_persistence_pending = True
                persistence_pending = True
            await host._verify_writer_lease()
            results.append(
                DurableApprovalItemResult(
                    item.approval_id,
                    envelope.tool_name,
                    "succeeded"
                    if not item_persistence_pending
                    else "recovery_required",
                    write.result,
                )
            )

        await host._verify_writer_lease()
        statuses = {item.status for item in results}
        if not persistence_pending and "outcome_unknown" not in statuses:
            outcome = "failed" if "failed" in statuses else "completed"
            if host.operation_recorder.operation_id == batch.operation_id:
                await host.operation_recorder.finish_operation(outcome)
            else:
                await self.finish_persisted_operation(batch.operation_id, outcome)
            if (
                host.runtime_tracker.state.run_id is not None
                and not host.runtime_tracker.state.terminal
            ):
                await host.runtime_tracker.record_external(
                    "run_finished", {"outcome": outcome}
                )
        elif (
            "outcome_unknown" in statuses
            and host.runtime_tracker.state.run_id is not None
            and not host.runtime_tracker.state.terminal
        ):
            await host.runtime_tracker.record_external("outcome_unknown")

        await host._verify_writer_lease()
        context = await host.context_projection.project(host.session_id)
        host.agent.state.messages = context.copy_messages()
        return DurableApprovalBatchResult(batch.operation_id, tuple(results))

    async def _load_canonical_batch(
        self,
        supplied: DurableApprovalBatch,
    ) -> DurableApprovalBatch:
        """Resolve one batch exclusively from durable facts before any grant."""

        events = await self.host.operation_store.load(
            session_id=self.host.session_id,
            operation_id=supplied.operation_id,
        )
        if not events:
            raise RuntimeError("Durable Approval Batch 对应的 Operation 不存在")
        replay_operation(events)
        candidates = _persisted_approval_batches(events, supplied.operation_id)
        if supplied.batch_id is not None:
            matches = [
                candidate
                for candidate in candidates
                if candidate.batch_id == supplied.batch_id
            ]
        else:
            # Legacy batches did not expose an ID. They remain recoverable only
            # when their full ordered item set matches one pre-manifest request.
            matches = [
                candidate
                for candidate in candidates
                if candidate.batch_id is None and candidate.items == supplied.items
            ]
        if len(matches) != 1 or matches[0] != supplied:
            raise RuntimeError(
                "Durable Approval Batch 与持久 canonical batch 不一致"
            )
        return matches[0]

    async def _finish_batch_resume(
        self,
        operation_id: str,
        approval_id: str,
        *,
        succeeded: bool,
        fenced_claim: ClaimLease | None = None,
    ) -> None:
        lease = fenced_claim or self.host.session_writer_lease.claim_lease
        lease_seconds = (
            self.host.approval_resume.claim_lease_seconds
            if fenced_claim is not None
            else self.host.session_writer_lease.lease_seconds
        )
        event_type = (
            "approval_resume_completed" if succeeded else "approval_resume_failed"
        )
        for _ in range(20):
            events = await self.host.operation_store.load(
                session_id=self.host.session_id,
                operation_id=operation_id,
            )
            if any(
                event.type == event_type
                and event.data.get("approvalId") == approval_id
                for event in events
            ):
                return
            specs = [
                (
                    event_type,
                    {
                        "approvalId": approval_id,
                        "fencingToken": lease.fencing_token,
                    },
                )
            ]
            replay_operation_with_specs(events, specs)
            try:
                await self.host.operation_store.append_batch_if_fenced_claim(
                    self.host.session_id,
                    operation_id,
                    specs,
                    lease,
                    renew_lease_seconds=lease_seconds,
                    expected_last_sequence=operation_last_sequence(events),
                    expected_claim_entity_id=(
                        approval_id
                        if lease.claim_type == "approval_resume"
                        else None
                    ),
                )
            except OperationStoreFencedClaimLostError as error:
                raise RuntimeError(
                    "Approval Resume Lease 已丢失，禁止提交批量终态"
                ) from error
            except OperationStoreConflictError:
                continue
            return
        raise RuntimeError("批量 Approval Resume 终态持久化冲突")

    async def prepare(
        self,
        *,
        text: str,
        routed_result: Any,
        requester: VerifiedIdentity | None,
        approval_role: str,
        idempotency_key: str | None,
        entity_id: str | None = None,
        expected_entity_version: int | None = None,
        business_preconditions: dict[str, Any] | None = None,
    ) -> str:
        host = self.host
        if requester is None:
            await self._fail_active_operation()
            raise PermissionError("该请求需要 Approval，必须提供 VerifiedIdentity")
        try:
            requester = host.approvals.assert_trusted_identity(
                requester,
                purpose="Host approval requester",
            )
        except ApprovalError:
            await self._fail_active_operation()
            raise
        if len(routed_result.decision.selected_tools) != 1:
            await self._fail_active_operation()
            raise RuntimeError(
                "单 Action prepare 只接受一个明确写工具；组合写操作必须调用 "
                "request_many 并逐项提供参数、幂等键和审批角色"
            )
        operation_id = host.operation_recorder.operation_id
        if operation_id is None:
            raise RuntimeError("Approval Required 但 Durable Operation 未保持活动")
        if not host.operation_store.supports_atomic_transactions:
            await self._fail_active_operation()
            raise RuntimeError("Approval 写请求必须使用支持原子事务的 Operation Store")
        if idempotency_key is None:
            await self._fail_active_operation()
            raise ValueError("Approval 写请求必须在初始 Prompt 提供 Idempotency Key")

        tool_name = routed_result.decision.selected_tools[0]
        raw_arguments = dict(routed_result.decision.extracted_fields)
        try:
            if host.routed_agent is None:
                raise RuntimeError("Approval 参数校验缺少 Routed Agent")
            selected_tool = host.routed_agent.capabilities.tools_by_names(
                (tool_name,)
            )[0]
            validated_arguments = selected_tool.validate_args(raw_arguments)
            if not isinstance(validated_arguments, dict):
                raise TypeError("Approval Tool 参数校验必须返回对象")
            arguments = dict(validated_arguments)
        except Exception:
            await self._fail_active_operation()
            raise
        envelope = DurableActionEnvelope(
            operation_id=operation_id,
            tool_call_id=str(uuid4()),
            tool_name=tool_name,
            arguments=arguments,
            write_id=str(uuid4()),
            entity_id=entity_id,
            expected_entity_version=expected_entity_version,
            business_preconditions=business_preconditions,
        )
        request_id = str(uuid4())
        planned_call = assistant_message(
            model=host.model,
            stop_reason="toolUse",
            content=[
                {
                    "type": "toolCall",
                    "id": envelope.tool_call_id,
                    "name": tool_name,
                    "arguments": arguments,
                }
            ],
        )
        policy = ModelRequestPolicy(
            visible_tool_names=(tool_name,),
            tool_choice={"type": "function", "function": {"name": tool_name}},
            required_capabilities=tuple(
                routed_result.decision.required_capabilities
            ),
            allowed_tool_names=(tool_name,),
            expected_tool_arguments=arguments,
            continuation_policy=ModelRequestPolicy.no_tools(),
        )
        approval_id, approval_data = build_approval_request_event(
            requester=requester,
            action=envelope.to_dict(),
            action_summary=(
                f"执行 {routed_result.decision.intent or tool_name}"
            ),
            required_role=approval_role,
        )
        specs: list[tuple[str, dict[str, Any]]] = [
            ("message_appended", {"message": user_message(text)}),
            (
                "model_request_started",
                {
                    "requestId": request_id,
                    "source": "host_approval_plan",
                    "requestPolicy": policy.to_dict(),
                },
            ),
            (
                "model_request_completed",
                {
                    "requestId": request_id,
                    "message": planned_call,
                    "source": "host_approval_plan",
                },
            ),
            (
                "model_policy_selected",
                {"policy": ModelRequestPolicy.no_tools().to_dict()},
            ),
            (
                "write_prepared",
                {
                    "writeId": envelope.write_id,
                    "toolName": tool_name,
                    "arguments": arguments,
                    "actionHash": envelope.action_hash,
                    "idempotencyKeyHash": hash_scoped_idempotency_key(
                        idempotency_key,
                        session_id=host.session_id,
                        principal_id=requester.principal_id,
                        tool_name=tool_name,
                    ),
                    "idempotencyHashVersion": 2,
                    "requesterId": requester.principal_id,
                    "requesterVerificationId": requester.verification_id,
                    "toolCallId": envelope.tool_call_id,
                    **_durable_action_binding(envelope),
                },
            ),
            ("approval_requested", approval_data),
            (
                "approval_resume_registered",
                {
                    "approvalId": approval_id,
                    "action": envelope.to_dict(),
                    "resumePayload": {"envelope": envelope.to_dict()},
                },
            ),
            (
                "write_waiting_approval",
                {"writeId": envelope.write_id, "approvalId": approval_id},
            ),
            (
                "tool_intent_recorded",
                {
                    "toolCallId": envelope.tool_call_id,
                    "toolName": tool_name,
                    "arguments": arguments,
                    "replayPolicy": "never",
                    "source": "host_approval_plan",
                },
            ),
        ]
        for _ in range(20):
            events = await host.operation_store.load(
                session_id=host.session_id,
                operation_id=operation_id,
            )
            replay_operation_with_specs(events, specs)
            try:
                await host.operation_store.append_batch_if_fenced_claim(
                    host.session_id,
                    operation_id,
                    specs,
                    host.session_writer_lease.claim_lease,
                    renew_lease_seconds=host.session_writer_lease.lease_seconds,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreFencedClaimLostError as error:
                raise RuntimeError(
                    "Session Writer Lease 已丢失，禁止规划 Approval Write"
                ) from error
            except OperationStoreConflictError:
                continue
            pending = await host.approval_resume.get_pending(approval_id)
            return pending.approval.approval_id
        raise RuntimeError("Approval/Write/Tool Intent 原子持久化冲突")

    async def approve_and_resume(
        self,
        approval_id: str,
        *,
        approver: VerifiedIdentity,
        consumer: VerifiedIdentity,
        idempotency_key: str,
        write_handler: Callable[..., Any] | None = None,
        context_handler: Callable[..., Any] | None = None,
    ) -> Any:
        _require_exactly_one_write_handler(write_handler, context_handler)
        if write_handler is not None and not _callback_accepts_keyword(
            write_handler,
            "fenced_claim",
        ):
            raise ValueError(
                "危险 Write Handler 必须显式接收 fenced_claim；"
                "或改用 WriteExecutionContext context_handler"
            )
        host = self.host
        pending = await host.approval_resume.get_pending(approval_id)
        approval_events = await host.operation_store.load(
            session_id=host.session_id,
            operation_id=pending.approval.operation_id,
        )
        requested_at = next(
            (
                event.timestamp
                for event in approval_events
                if event.type == "approval_requested"
                and event.data.get("approvalId") == approval_id
            ),
            None,
        )
        if requested_at is not None:
            host.telemetry.record_approval_wait(
                outcome="resume_attempt",
                duration_ms=max(0.0, time.time() * 1000 - requested_at),
            )
        pending_envelope = DurableActionEnvelope.from_dict(
            pending.resume_payload.get("envelope")
        )
        resumed_operation_id: str | None = pending_envelope.operation_id
        active_fenced_claim: ClaimLease | None = None

        async def resume(
            payload: dict[str, Any],
            *,
            fencing_token: int,
            fenced_claim: ClaimLease | None = None,
        ) -> Any:
            nonlocal resumed_operation_id, active_fenced_claim
            if fenced_claim is None:
                raise RuntimeError(
                    "Approval Resume 缺少完整 Fenced Claim，禁止执行写操作"
                )
            active_fenced_claim = fenced_claim
            envelope = DurableActionEnvelope.from_dict(payload.get("envelope"))
            operation_id = envelope.operation_id
            resumed_operation_id = operation_id
            if (
                host.runtime_tracker.state.run_id is not None
                and not host.runtime_tracker.state.terminal
                and host.runtime_tracker.state.phase == "waiting_approval"
            ):
                await host.runtime_tracker.record_external("approval_granted")
            write = await host.writes.get(envelope.write_id)
            envelope.assert_execution(
                operation_id=write.operation_id,
                tool_call_id=write.tool_call_id or "",
                tool_name=write.tool_name,
                arguments=write.arguments,
                write_id=write.write_id,
                entity_id=write.entity_id,
                expected_entity_version=write.expected_entity_version,
                business_preconditions=write.business_preconditions,
                validate_business_binding=True,
            )
            if write.action_hash != envelope.action_hash:
                raise RuntimeError("Approval Envelope 与 Write Action Hash 不一致")
            if write.approval_id != approval_id:
                raise RuntimeError("Approval Envelope 与 Write Approval ID 不一致")

            async def invoke_context(context):
                if context_handler is not None:
                    value = context_handler(context)
                    return await value if hasattr(value, "__await__") else value
                assert write_handler is not None
                return await invoke_fenced_callback(
                    write_handler,
                    context.arguments,
                    context.idempotency_key,
                    context.actor,
                    fencing_token=context.fencing_token,
                    fenced_claim=fenced_claim,
                )

            write = await host.writes.execute(
                write.write_id,
                actor=consumer,
                idempotency_key=idempotency_key,
                context_handler=invoke_context,
                approval_resume_id=approval_id,
                fencing_token=fencing_token,
                fenced_claim=fenced_claim,
            )
            tool_result = {
                "role": "toolResult",
                "toolCallId": envelope.tool_call_id,
                "toolName": envelope.tool_name,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(write.result or {}, ensure_ascii=False),
                    }
                ],
                "details": write.result or {},
                "isError": False,
            }
            await self.materialize_write_tool_result(
                operation_id,
                tool_result,
                approval_id=approval_id,
                fenced_claim=fenced_claim,
            )
            return await self.request_final_model(
                operation_id=operation_id,
                approval_id=approval_id,
                policy=ModelRequestPolicy.no_tools(),
                fenced_claim=fenced_claim,
            )

        try:
            result = await host.approval_resume.approve_and_resume(
                approval_id,
                approver=approver,
                consumer=consumer,
                resume=resume,
                atomic_write_start=True,
            )
        except Exception as error:
            outcome_unknown = is_outcome_unknown_error(error)
            if resumed_operation_id is not None:
                cleanup_error: Exception | None = None
                try:
                    await self.materialize_write_tool_result(
                        resumed_operation_id,
                        _failed_write_tool_result(pending_envelope, error),
                        approval_id=approval_id,
                        fenced_claim=active_fenced_claim,
                    )
                    # 结果不确定仍是可恢复状态，绝不能用 Operation Failed
                    # 把 Reconciliation 入口永久封死。
                    if not outcome_unknown:
                        if host.operation_recorder.operation_id == resumed_operation_id:
                            await host.operation_recorder.finish_operation("failed")
                        else:
                            await self.finish_persisted_operation(
                                resumed_operation_id, "failed"
                            )
                except Exception as caught:
                    cleanup_error = caught
                if cleanup_error is not None:
                    error.add_note(
                        "写失败后的 Tool Result/Operation 闭合也失败："
                        + str(cleanup_error)
                    )
            if (
                host.runtime_tracker.state.run_id is not None
                and not host.runtime_tracker.state.terminal
            ):
                if outcome_unknown:
                    await host.runtime_tracker.record_external("outcome_unknown")
                else:
                    await host.runtime_tracker.record_external(
                        "run_finished", {"outcome": "failed"}
                    )
            raise
        if resumed_operation_id is not None:
            if host.operation_recorder.operation_id == resumed_operation_id:
                await host.operation_recorder.finish_operation("completed")
            else:
                await self.finish_persisted_operation(
                    resumed_operation_id, "completed"
                )
        if (
            host.runtime_tracker.state.run_id is not None
            and not host.runtime_tracker.state.terminal
        ):
            await host.runtime_tracker.record_external(
                "run_finished", {"outcome": "completed"}
            )
        return result

    async def reject_and_finish(
        self,
        approval_id: str,
        *,
        rejector: VerifiedIdentity,
        reason: str,
    ) -> DurableActionEnvelope:
        """拒绝待确认写操作，并以错误 Tool Result 闭合持久 Transcript。"""

        host = self.host
        pending = await host.approval_resume.get_pending(approval_id)
        envelope = DurableActionEnvelope.from_dict(
            pending.resume_payload.get("envelope")
        )
        approval = await host.approvals.get(approval_id)
        if approval.state == "waiting":
            await host.approvals.reject(
                approval_id,
                rejector,
                reason=reason,
                fenced_claim=host.session_writer_lease.claim_lease,
                renew_lease_seconds=host.session_writer_lease.lease_seconds,
            )
        elif approval.state != "rejected":
            raise PermissionError("只有等待确认的 Approval 可以拒绝")

        details = {
            "code": "approval_rejected",
            "message": reason,
        }
        tool_result = {
            "role": "toolResult",
            "toolCallId": envelope.tool_call_id,
            "toolName": envelope.tool_name,
            "content": [{"type": "text", "text": reason}],
            "details": details,
            "isError": True,
        }
        operation_id = envelope.operation_id
        for _ in range(20):
            events = await host.operation_store.load(
                session_id=host.session_id,
                operation_id=operation_id,
            )
            operation = replay_operation(events)
            specs: list[tuple[str, dict[str, Any]]] = []
            write = operation.writes.get(envelope.write_id)
            if write is None:
                raise RuntimeError("拒绝 Approval 时缺少对应 Write")
            if write.state == "waiting_approval":
                specs.append(
                    (
                        "write_failed",
                        {
                            "writeId": envelope.write_id,
                            "reason": "approval_not_granted",
                            "result": details,
                        },
                    )
                )
            elif write.state != "failed":
                raise RuntimeError(f"Write 状态 {write.state} 不能拒绝")
            invocation = operation.tools.get(envelope.tool_call_id)
            if invocation is None:
                raise RuntimeError("拒绝 Approval 时缺少 Tool Intent")
            if invocation.phase == "intent_recorded":
                specs.append(
                    (
                        "tool_completed",
                        {
                            "toolCallId": envelope.tool_call_id,
                            "result": {
                                "content": tool_result["content"],
                                "details": details,
                                "isError": True,
                            },
                        },
                    )
                )
            elif invocation.phase != "completed":
                raise RuntimeError(
                    f"Tool 状态 {invocation.phase} 不能因 Approval 拒绝而结束"
                )
            if not any(
                message.get("role") == "toolResult"
                and message.get("toolCallId") == envelope.tool_call_id
                for message in operation.messages
            ):
                specs.append(("message_appended", {"message": tool_result}))
            if not specs:
                break
            replay_operation_with_specs(events, specs)
            try:
                await host.operation_store.append_batch_if_fenced_claim(
                    host.session_id,
                    operation_id,
                    specs,
                    host.session_writer_lease.claim_lease,
                    renew_lease_seconds=host.session_writer_lease.lease_seconds,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreFencedClaimLostError as error:
                raise RuntimeError(
                    "Session Writer Lease 已丢失，禁止闭合拒绝结果"
                ) from error
            except OperationStoreConflictError:
                continue
            break
        else:
            raise RuntimeError("拒绝 Approval 的 Tool/Write 闭合发生并发冲突")

        if host.operation_recorder.operation_id == operation_id:
            await host.operation_recorder.finish_operation("cancelled")
        else:
            await self.finish_persisted_operation(operation_id, "cancelled")
        if (
            host.runtime_tracker.state.run_id is not None
            and not host.runtime_tracker.state.terminal
        ):
            await host.runtime_tracker.record_external("approval_rejected")
            await host.runtime_tracker.record_external(
                "run_finished",
                {"outcome": "cancelled"},
            )
        context = await host.context_projection.project(host.session_id)
        host.agent.state.messages = context.copy_messages()
        return envelope

    async def request_final_model(
        self,
        *,
        operation_id: str,
        approval_id: str,
        policy: ModelRequestPolicy,
        fenced_claim: ClaimLease | None = None,
    ) -> dict[str, Any]:
        """Resume 重入时复用同一条最终 Model Request。"""

        host = self.host
        commit_claim = fenced_claim or host.session_writer_lease.claim_lease
        commit_lease_seconds = (
            host.approval_resume.claim_lease_seconds
            if fenced_claim is not None
            else host.session_writer_lease.lease_seconds
        )
        request_id: str | None = None
        for _ in range(20):
            events = await host.operation_store.load(
                session_id=host.session_id,
                operation_id=operation_id,
            )
            current = replay_operation(events)
            matching = _approval_final_request_ids(events, approval_id)
            if any(current.model_requests[item].policy != policy for item in matching):
                raise RuntimeError("Approval 最终 Model Request 的持久策略不匹配")
            pending = [
                item
                for item in matching
                if current.model_requests[item].phase == "started"
            ]
            completed = [
                item
                for item in matching
                if current.model_requests[item].phase == "completed"
            ]
            if len(pending) > 1 or len(completed) > 1 or (pending and completed):
                raise RuntimeError("Approval 最终 Model Request 持久状态冲突")
            if completed:
                return _persisted_model_response(events, completed[-1])
            interrupted_request_id = pending[0] if pending else None
            request_id = str(uuid4())
            specs: list[tuple[str, dict[str, Any]]] = []
            if interrupted_request_id is not None:
                specs.append(
                    (
                        "model_request_failed",
                        {
                            "requestId": interrupted_request_id,
                            "source": "approval_resume",
                            "approvalId": approval_id,
                            "errorCode": "process_interrupted",
                            "fencingToken": (
                                commit_claim.fencing_token
                            ),
                        },
                    )
                )
            specs.append(
                (
                    "model_request_started",
                    {
                        "requestId": request_id,
                        "source": "approval_resume",
                        "approvalId": approval_id,
                        "requestPolicy": policy.to_dict(),
                        "fencingToken": (
                            commit_claim.fencing_token
                        ),
                    },
                )
            )
            operation = replay_operation_with_specs(events, specs)
            try:
                await host.operation_store.append_batch_if_fenced_claim(
                    host.session_id,
                    operation_id,
                    specs,
                    commit_claim,
                    renew_lease_seconds=commit_lease_seconds,
                    expected_last_sequence=operation_last_sequence(events),
                    expected_claim_entity_id=(
                        approval_id
                        if commit_claim.claim_type == "approval_resume"
                        else None
                    ),
                )
            except OperationStoreFencedClaimLostError as error:
                raise RuntimeError(
                    "Approval Resume Lease 已丢失，禁止请求最终模型"
                ) from error
            except OperationStoreConflictError:
                continue
            break
        else:
            raise RuntimeError("Approval 最终 Model Request 开始并发冲突")

        if request_id is None:
            raise RuntimeError("Approval 最终 Model Request ID 缺失")
        try:
            final = await host.model_runtime.request(
                list(operation.messages),
                policy=policy,
                request_id=request_id,
                durable_metadata={
                    "sessionId": host.session_id,
                    "operationId": operation_id,
                    **(
                        {"runId": host.runtime_tracker.state.run_id}
                        if isinstance(host.runtime_tracker.state.run_id, str)
                        and host.runtime_tracker.state.run_id
                        else {}
                    ),
                    **(
                        {
                            "fencingToken": str(
                                commit_claim.fencing_token
                            )
                        }
                        if commit_claim is not None
                        else {}
                    ),
                    "fencingScope": commit_claim.resource_id,
                },
                source="approval_resume",
            )
            tool_calls = [
                block
                for block in final.get("content", [])
                if isinstance(block, dict) and block.get("type") == "toolCall"
            ]
            if tool_calls:
                raise RuntimeError("Approval 完成后的最终模型响应禁止包含 Tool Call")
            if final.get("stopReason", "stop") != "stop":
                raise RuntimeError("Approval 完成后的最终模型响应没有正常结束")
        except Exception as error:
            persisted = await self.finish_model_request(
                operation_id=operation_id,
                approval_id=approval_id,
                request_id=request_id,
                event_type="model_request_failed",
                data={
                    "requestId": request_id,
                    "source": "approval_resume",
                    "approvalId": approval_id,
                    "errorCode": "approval_final_response_invalid",
                    "error": "Approval 最终模型响应无效",
                    "fencingToken": (
                        commit_claim.fencing_token
                    ),
                },
                fenced_claim=fenced_claim,
            )
            if persisted is not None:
                return persisted
            if isinstance(error, ModelRequestPolicyError) and "工具" in str(error):
                raise RuntimeError(
                    "Approval 完成后的最终模型响应禁止使用工具"
                ) from error
            raise
        persisted = await self.finish_model_request(
            operation_id=operation_id,
            approval_id=approval_id,
            request_id=request_id,
            event_type="model_request_completed",
            data={
                "requestId": request_id,
                "source": "approval_resume",
                "approvalId": approval_id,
                "message": final,
                "fencingToken": (
                    commit_claim.fencing_token
                ),
            },
            fenced_claim=fenced_claim,
        )
        return persisted if persisted is not None else final

    async def finish_model_request(
        self,
        *,
        operation_id: str,
        approval_id: str,
        request_id: str,
        event_type: str,
        data: dict[str, Any],
        fenced_claim: ClaimLease | None = None,
    ) -> dict[str, Any] | None:
        host = self.host
        commit_claim = fenced_claim or host.session_writer_lease.claim_lease
        commit_lease_seconds = (
            host.approval_resume.claim_lease_seconds
            if fenced_claim is not None
            else host.session_writer_lease.lease_seconds
        )
        for _ in range(20):
            events = await host.operation_store.load(
                session_id=host.session_id,
                operation_id=operation_id,
            )
            operation = replay_operation(events)
            request = operation.model_requests.get(request_id)
            if request is None or request_id not in _approval_final_request_ids(
                events, approval_id
            ):
                raise RuntimeError("Approval 最终 Model Request 绑定丢失")
            if request.phase == "completed":
                return _persisted_model_response(events, request_id)
            if request.phase == "failed":
                if event_type == "model_request_completed":
                    raise RuntimeError("Approval 最终 Model Request 已持久为 Failed")
                return None
            specs = [(event_type, data)]
            replay_operation_with_specs(events, specs)
            try:
                await host.operation_store.append_batch_if_fenced_claim(
                    host.session_id,
                    operation_id,
                    specs,
                    commit_claim,
                    renew_lease_seconds=commit_lease_seconds,
                    expected_last_sequence=operation_last_sequence(events),
                    expected_claim_entity_id=(
                        approval_id
                        if commit_claim.claim_type == "approval_resume"
                        else None
                    ),
                )
            except OperationStoreFencedClaimLostError as error:
                raise RuntimeError(
                    "Approval Resume Lease 已丢失，禁止结束最终模型请求"
                ) from error
            except OperationStoreConflictError:
                continue
            return copy.deepcopy(data.get("message"))
        raise RuntimeError("Approval 最终 Model Request 结束并发冲突")

    async def materialize_write_tool_result(
        self,
        operation_id: str,
        tool_result: dict[str, Any],
        *,
        approval_id: str | None = None,
        fenced_claim: ClaimLease | None = None,
    ) -> Any:
        host = self.host
        commit_claim = fenced_claim or host.session_writer_lease.claim_lease
        commit_lease_seconds = (
            host.approval_resume.claim_lease_seconds
            if fenced_claim is not None
            else host.session_writer_lease.lease_seconds
        )
        tool_call_id = str(tool_result["toolCallId"])
        for _ in range(20):
            events = await host.operation_store.load(
                session_id=host.session_id,
                operation_id=operation_id,
            )
            operation = replay_operation(events)
            invocation = operation.tools.get(tool_call_id)
            if invocation is None:
                raise RuntimeError("Write Tool Result 缺少持久 Tool Intent")
            specs: list[tuple[str, dict[str, Any]]] = []
            is_error = bool(tool_result.get("isError", False))
            details = tool_result.get("details")
            outcome_unknown = (
                isinstance(details, dict)
                and bool(details.get("outcomeUnknown", False))
            )
            if invocation.phase in {"intent_recorded", "dispatch_started"}:
                event_type = (
                    "tool_outcome_unknown"
                    if outcome_unknown and invocation.phase == "dispatch_started"
                    else "tool_completed"
                )
                specs.append(
                    (
                        event_type,
                        {
                            "toolCallId": tool_call_id,
                            "result": {
                                "content": tool_result["content"],
                                "details": details,
                                "isError": is_error,
                            },
                        },
                    )
                )
            elif invocation.phase not in {"completed", "outcome_unknown"}:
                raise RuntimeError(
                    f"Write Tool 当前状态 {invocation.phase} 不能物化结果"
                )
            if not any(
                message.get("role") == "toolResult"
                and message.get("toolCallId") == tool_call_id
                for message in operation.messages
            ):
                specs.append(("message_appended", {"message": tool_result}))
            if not specs:
                return operation
            replay_operation_with_specs(events, specs)
            try:
                await host.operation_store.append_batch_if_fenced_claim(
                    host.session_id,
                    operation_id,
                    specs,
                    commit_claim,
                    renew_lease_seconds=commit_lease_seconds,
                    expected_last_sequence=operation_last_sequence(events),
                    expected_claim_entity_id=(
                        approval_id
                        if commit_claim.claim_type == "approval_resume"
                        else None
                    ),
                )
            except OperationStoreFencedClaimLostError as error:
                raise RuntimeError(
                    "Approval Resume Lease 已丢失，禁止物化 Tool Result"
                ) from error
            except OperationStoreConflictError:
                continue
            return replay_operation(
                await host.operation_store.load(
                    session_id=host.session_id,
                    operation_id=operation_id,
                )
            )
        raise RuntimeError("Write Tool Result 物化并发冲突")

    async def finish_persisted_operation(
        self,
        operation_id: str,
        outcome: str,
        *,
        fenced_claim: ClaimLease | None = None,
        approval_id: str | None = None,
    ) -> None:
        host = self.host
        commit_claim = fenced_claim or host.session_writer_lease.claim_lease
        commit_lease_seconds = (
            host.approval_resume.claim_lease_seconds
            if fenced_claim is not None
            else host.session_writer_lease.lease_seconds
        )
        specs = [("operation_finished", {"outcome": outcome})]
        for _ in range(20):
            events = await host.operation_store.load(
                session_id=host.session_id,
                operation_id=operation_id,
            )
            state = replay_operation(events)
            if state.phase in {"completed", "failed", "cancelled"}:
                return
            replay_operation_with_specs(events, specs)
            try:
                await host.operation_store.append_batch_if_fenced_claim(
                    host.session_id,
                    operation_id,
                    specs,
                    commit_claim,
                    renew_lease_seconds=commit_lease_seconds,
                    expected_last_sequence=operation_last_sequence(events),
                    expected_claim_entity_id=(
                        approval_id
                        if commit_claim.claim_type == "approval_resume"
                        else None
                    ),
                )
            except OperationStoreFencedClaimLostError as error:
                raise RuntimeError(
                    "Operation Finish Claim 已丢失，禁止闭合 Operation"
                ) from error
            except OperationStoreConflictError:
                continue
            return
        raise RuntimeError("Operation Finished 并发冲突")

    async def _fail_active_operation(self) -> None:
        host = self.host
        if (
            host.runtime_tracker.state.run_id is not None
            and not host.runtime_tracker.state.terminal
        ):
            await host.runtime_tracker.record_external(
                "run_finished", {"outcome": "failed"}
            )
        if host.operation_recorder.operation_id is not None:
            await host.operation_recorder.finish_operation("failed")


def _approval_batch_digest(
    operation_id: str,
    batch_id: str,
    items: tuple[DurableApprovalBatchItem, ...],
) -> str:
    value = {
        "version": 1,
        "operationId": operation_id,
        "batchId": batch_id,
        "items": [
            {
                "approvalId": item.approval_id,
                "envelope": item.envelope.to_dict(),
            }
            for item in items
        ],
    }
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _approval_batch_manifest(
    operation_id: str,
    batch_id: str,
    batch_hash: str,
    items: tuple[DurableApprovalBatchItem, ...],
) -> dict[str, Any]:
    return {
        "version": 1,
        "operationId": operation_id,
        "batchId": batch_id,
        "batchHash": batch_hash,
        "items": [
            {
                "approvalId": item.approval_id,
                "envelope": item.envelope.to_dict(),
            }
            for item in items
        ],
    }


def _persisted_approval_batches(
    events: list[Any],
    operation_id: str,
) -> tuple[DurableApprovalBatch, ...]:
    """Rebuild every batch from persisted model calls and resume envelopes."""

    completed_by_request: dict[str, list[Any]] = {}
    for event in events:
        if (
            event.type == "model_request_completed"
            and event.data.get("source") == "host_approval_batch_plan"
        ):
            request_id = event.data.get("requestId")
            if isinstance(request_id, str) and request_id:
                completed_by_request.setdefault(request_id, []).append(event)

    result: list[DurableApprovalBatch] = []
    seen_batch_ids: set[str] = set()
    for event in events:
        if (
            event.type != "model_request_started"
            or event.data.get("source") != "host_approval_batch_plan"
        ):
            continue
        request_id = event.data.get("requestId")
        completed = (
            completed_by_request.get(request_id, [])
            if isinstance(request_id, str)
            else []
        )
        if len(completed) != 1:
            raise RuntimeError(
                "Durable Approval Batch 缺少唯一持久 Model Response"
            )
        derived_items = _approval_batch_items_from_response(
            events,
            operation_id,
            completed[0],
        )
        raw_manifest = event.data.get("approvalBatch")
        if raw_manifest is None:
            # Compatibility for batches persisted before batchId/hash existed.
            result.append(DurableApprovalBatch(operation_id, derived_items))
            continue
        persisted = _approval_batch_from_manifest(operation_id, raw_manifest)
        if persisted.items != derived_items:
            raise RuntimeError(
                "Durable Approval Batch Manifest 与持久 Tool Call 不一致"
            )
        assert persisted.batch_id is not None
        if persisted.batch_id in seen_batch_ids:
            raise RuntimeError("Durable Approval Batch ID 在 Operation 中重复")
        seen_batch_ids.add(persisted.batch_id)
        result.append(persisted)
    if not result:
        raise RuntimeError("Operation 中不存在持久 Durable Approval Batch")
    return tuple(result)


def _approval_batch_items_from_response(
    events: list[Any],
    operation_id: str,
    completed: Any,
) -> tuple[DurableApprovalBatchItem, ...]:
    message = completed.data.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        raise RuntimeError("Durable Approval Batch Model Response 无效")
    calls = [
        item
        for item in content
        if isinstance(item, Mapping) and item.get("type") == "toolCall"
    ]
    if not calls:
        raise RuntimeError("Durable Approval Batch Model Response 缺少 Tool Call")

    registrations: dict[str, list[DurableApprovalBatchItem]] = {}
    for event in events:
        if event.type != "approval_resume_registered":
            continue
        envelope = DurableActionEnvelope.from_dict(event.data.get("action"))
        if envelope.operation_id != operation_id:
            continue
        approval_id = event.data.get("approvalId")
        if not isinstance(approval_id, str) or not approval_id:
            raise RuntimeError("Durable Approval Batch Resume 缺少 Approval ID")
        registrations.setdefault(envelope.tool_call_id, []).append(
            DurableApprovalBatchItem(approval_id, envelope)
        )

    items: list[DurableApprovalBatchItem] = []
    seen_tool_call_ids: set[str] = set()
    for call in calls:
        tool_call_id = call.get("id")
        tool_name = call.get("name")
        arguments = call.get("arguments")
        if (
            not isinstance(tool_call_id, str)
            or not tool_call_id
            or tool_call_id in seen_tool_call_ids
            or not isinstance(tool_name, str)
            or not tool_name
            or not isinstance(arguments, dict)
        ):
            raise RuntimeError("Durable Approval Batch Tool Call 结构无效")
        seen_tool_call_ids.add(tool_call_id)
        matches = registrations.get(tool_call_id, [])
        if len(matches) != 1:
            raise RuntimeError(
                "Durable Approval Batch Tool Call 缺少唯一 Approval Resume"
            )
        item = matches[0]
        expected = DurableActionEnvelope(
            operation_id=operation_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            write_id=item.envelope.write_id,
            entity_id=item.envelope.entity_id,
            expected_entity_version=item.envelope.expected_entity_version,
            business_preconditions=item.envelope.business_preconditions,
        )
        if expected != item.envelope:
            raise RuntimeError(
                "Durable Approval Batch Tool Call 与 Resume Envelope 不一致"
            )
        items.append(item)
    return tuple(items)


def _durable_action_binding(
    envelope: DurableActionEnvelope,
) -> dict[str, Any]:
    """把审批所见的并发/业务条件同步写入 Write Intent。"""

    value: dict[str, Any] = {}
    if envelope.entity_id is not None:
        value["entityId"] = envelope.entity_id
    if envelope.expected_entity_version is not None:
        value["expectedEntityVersion"] = envelope.expected_entity_version
    if envelope.business_preconditions is not None:
        value["businessPreconditions"] = copy.deepcopy(
            envelope.business_preconditions
        )
    return value


def _approval_batch_from_manifest(
    operation_id: str,
    value: Any,
) -> DurableApprovalBatch:
    expected_fields = {
        "version",
        "operationId",
        "batchId",
        "batchHash",
        "items",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise RuntimeError("Durable Approval Batch Manifest 字段无效")
    if value.get("version") != 1 or value.get("operationId") != operation_id:
        raise RuntimeError("Durable Approval Batch Manifest 身份无效")
    batch_id = value.get("batchId")
    batch_hash = value.get("batchHash")
    raw_items = value.get("items")
    if (
        not isinstance(batch_id, str)
        or not batch_id
        or not isinstance(batch_hash, str)
        or not batch_hash
        or not isinstance(raw_items, list)
        or not raw_items
    ):
        raise RuntimeError("Durable Approval Batch Manifest 内容无效")
    items: list[DurableApprovalBatchItem] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping) or set(raw_item) != {
            "approvalId",
            "envelope",
        }:
            raise RuntimeError("Durable Approval Batch Manifest Item 无效")
        approval_id = raw_item.get("approvalId")
        if not isinstance(approval_id, str) or not approval_id:
            raise RuntimeError("Durable Approval Batch Manifest Approval ID 无效")
        items.append(
            DurableApprovalBatchItem(
                approval_id,
                DurableActionEnvelope.from_dict(raw_item.get("envelope")),
            )
        )
    return DurableApprovalBatch(
        operation_id,
        tuple(items),
        batch_id=batch_id,
        batch_hash=batch_hash,
    )


def _approval_final_request_ids(events: list[Any], approval_id: str) -> list[str]:
    exact: list[str] = []
    legacy: list[str] = []
    active_approvals: set[str] = set()
    terminal_types = {
        "approval_resume_completed",
        "approval_resume_failed",
        "approval_resume_cancelled",
    }
    for event in sorted(events, key=lambda item: item.sequence):
        if event.type == "approval_resume_started":
            value = event.data.get("approvalId")
            if isinstance(value, str) and value:
                active_approvals.add(value)
        elif event.type in terminal_types:
            value = event.data.get("approvalId")
            if isinstance(value, str):
                active_approvals.discard(value)
        if event.type != "model_request_started" or event.data.get("source") != "approval_resume":
            continue
        request_id = event.data.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            continue
        bound = event.data.get("approvalId")
        if bound == approval_id:
            exact.append(request_id)
        elif bound is None and active_approvals == {approval_id}:
            legacy.append(request_id)
    return exact or legacy


def _failed_write_tool_result(
    envelope: DurableActionEnvelope,
    error: Exception,
) -> dict[str, Any]:
    """把写异常转换成可持久化且不会泄露内部信息的失败 Tool Result。"""

    code = getattr(error, "code", None)
    if not isinstance(code, str) or not code.strip():
        code = "write_execution_failed"
    public_message = getattr(error, "public_message", None)
    if not isinstance(public_message, str) or not public_message.strip():
        outcome_unknown = is_outcome_unknown_error(error)
        public_message = (
            "写操作结果未知，需要人工核对"
            if outcome_unknown
            else "写操作执行失败"
        )
    else:
        outcome_unknown = is_outcome_unknown_error(error)
    details = {
        "code": code,
        "message": public_message,
        "outcomeUnknown": outcome_unknown,
    }
    return {
        "role": "toolResult",
        "toolCallId": envelope.tool_call_id,
        "toolName": envelope.tool_name,
        "content": [{"type": "text", "text": public_message}],
        "details": details,
        "isError": True,
    }


def _persisted_model_response(events: list[Any], request_id: str) -> dict[str, Any]:
    matches = [
        event
        for event in events
        if event.type == "model_request_completed"
        and event.data.get("requestId") == request_id
    ]
    if len(matches) != 1 or not isinstance(matches[0].data.get("message"), dict):
        raise RuntimeError("持久 Model Request Completed 缺少唯一响应")
    return copy.deepcopy(matches[0].data["message"])


def _require_exactly_one_write_handler(
    write_handler: Callable[..., Any] | None,
    context_handler: Callable[..., Any] | None,
) -> None:
    if (write_handler is None) == (context_handler is None):
        raise ValueError(
            "write_handler 与 context_handler 必须且只能提供一个"
        )
    selected = write_handler if write_handler is not None else context_handler
    if not callable(selected):
        raise TypeError("Write Handler 必须可调用")


__all__ = [
    "DurableApprovalAction",
    "DurableApprovalBatch",
    "DurableApprovalBatchItem",
    "DurableApprovalBatchResult",
    "DurableApprovalExecution",
    "DurableApprovalItemResult",
    "DurableApprovalWorkflow",
]
