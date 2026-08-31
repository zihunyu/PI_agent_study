"""Trusted bridge from durable Plan steps to :class:`ToolDispatchRuntime`.

The generic planner never executes Python callbacks or selects tools directly.
Applications bind a trusted intent to an already registered tool here; schema
validation, identity authorization, hooks, timeout, retry policy and resource
locking therefore stay on the same boundary as ordinary Agent tool calls.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..cancellation import CancellationToken
from ..messages import assistant_message, public_error_message
from ..planning import (
    IntentPlanPolicy,
    PlanApprovalReceipt,
    PlanExecutionError,
    PlanStep,
)
from ..routing import CapabilityRegistry
from ..tool_contract import ToolSecurityContract
from ..tool_runtime import ToolDispatchRuntime
from ..types import AgentContext, AgentTool, Model, ToolDispatchContext
from ..writes import (
    TrustedWriteAuthorization,
    WriteOperation,
    WriteOperationService,
    is_outcome_unknown_error,
)


@dataclass(frozen=True, slots=True)
class PlanWriteMetadata:
    """Application-owned durable metadata for one Plan write.

    The framework deliberately does not derive business idempotency keys, entity
    identifiers or optimistic-lock versions from model arguments.  A trusted
    application adapter must provide them explicitly for every write step.
    """

    operation_id: str
    idempotency_key: str
    entity_id: str | None = None
    expected_entity_version: int | None = None
    business_preconditions: Mapping[str, Any] | None = None
    reconcile_handler: Callable[[WriteOperation], Awaitable[dict[str, Any]]] | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("operation_id", self.operation_id),
            ("idempotency_key", self.idempotency_key),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Plan Write Metadata {name} 不能为空")
        if self.reconcile_handler is not None and not callable(self.reconcile_handler):
            raise TypeError("Plan Write reconcile_handler 必须可调用或为 None")
        if self.business_preconditions is not None:
            object.__setattr__(
                self,
                "business_preconditions",
                copy.deepcopy(dict(self.business_preconditions)),
            )


PlanWriteMetadataProvider = Callable[..., PlanWriteMetadata | Awaitable[PlanWriteMetadata]]


class PlanToolDispatchError(PlanExecutionError):
    """A plan-bound Tool call was rejected or returned an error result."""

    def __init__(
        self,
        message: str,
        *,
        outcome_unknown: bool = False,
        durably_failed: bool = False,
        definitely_not_committed: bool = False,
        error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.public_message = message
        self.outcome_unknown = outcome_unknown
        self.durably_failed = durably_failed
        self.definitely_not_committed = definitely_not_committed
        self.error_type = error_type


class ToolRuntimePlanStepExecutor:
    """Execute Plan steps through a fixed, trusted intent-to-tool mapping."""

    trusted_plan_step_executor = True
    counts_as_tool_call = True
    supports_tool_attempt_admission = True

    @property
    def durable_write_boundary_configured(self) -> bool:
        """Whether write steps must enter the full durable Write state machine."""

        return (
            self._write_service_provider is not None
            and self._write_metadata_provider is not None
        )

    def __init__(
        self,
        *,
        runtime_provider: Callable[[], ToolDispatchRuntime],
        tools: list[AgentTool],
        intent_tools: Mapping[str, str],
        model: Model,
        session_id: str,
        dispatch_context_provider: Callable[[], ToolDispatchContext],
        policies: Mapping[str, IntentPlanPolicy] | None = None,
        capabilities: CapabilityRegistry | None = None,
        write_service_provider: Callable[[], WriteOperationService] | None = None,
        write_metadata_provider: PlanWriteMetadataProvider | None = None,
    ) -> None:
        if not session_id or not session_id.strip():
            raise ValueError("Plan Tool Runtime session_id 不能为空")
        by_name = {tool.name: tool for tool in tools}
        if len(by_name) != len(tools):
            raise ValueError("Plan Tool Runtime 不接受重复 Tool 名称")
        normalized: dict[str, str] = {}
        for intent, tool_name in intent_tools.items():
            if not isinstance(intent, str) or not intent.strip():
                raise ValueError("Plan Tool Binding intent 必须是非空字符串")
            if not isinstance(tool_name, str) or not tool_name.strip():
                raise ValueError("Plan Tool Binding tool_name 必须是非空字符串")
            if tool_name not in by_name:
                raise ValueError(
                    f"Plan Intent {intent} 绑定了未注册工具：{tool_name}"
                )
            normalized[intent] = tool_name
        if not normalized:
            raise ValueError("Plan Tool Binding 不能为空")
        self._runtime_provider = runtime_provider
        self._tools = by_name
        self._tool_contracts = {
            name: ToolSecurityContract.capture(tool)
            for name, tool in by_name.items()
        }
        self._intent_tools = normalized
        self._model = model
        self._session_id = session_id.strip()
        self._dispatch_context_provider = dispatch_context_provider
        self._write_service_provider = write_service_provider
        self._write_metadata_provider = write_metadata_provider
        self._approval_required_intents: set[str] = set()
        entries = {
            entry.tool.name: entry
            for entry in (() if capabilities is None else capabilities.all_entries())
        }
        for intent, tool_name in normalized.items():
            policy = None if policies is None else policies.get(intent)
            if policies is not None and policy is None:
                raise ValueError(f"Plan Tool Binding 缺少 Intent Policy：{intent}")
            tool = by_name[tool_name]
            entry = entries.get(tool_name)
            if entry is not None and entry.tool is not tool:
                raise ValueError(
                    "Plan Tool Binding 与 CapabilityRegistry 使用了同名但不同的 "
                    f"Tool 实例：{tool_name}"
                )
            effective_approval = tool.requires_approval or (
                entry.approval_required if entry is not None else False
            )
            if effective_approval:
                self._approval_required_intents.add(intent)
            effective_side_effect = entry.has_side_effect if entry is not None else False
            effective_never_replay = (
                tool.replay_policy == "never" or effective_side_effect
            )
            if policy is None:
                if effective_approval or effective_never_replay:
                    raise ValueError(
                        "危险 Plan Tool 必须提供可信 Intent Policy："
                        f"{intent}"
                    )
                continue
            dangerous = (
                policy.write
                or policy.requires_approval
                or policy.replay_policy == "never"
            )
            if dangerous and tool.execute_with_context is None:
                raise ValueError(
                    "危险 Plan Tool 必须实现 execute_with_context，"
                    f"以接收身份和 fencing：{tool_name}"
                )
            if policy.write and tool.replay_policy != "never":
                raise ValueError(
                    f"写 Plan Intent 绑定的 Tool 必须 replay_policy=never：{intent}"
                )
            if effective_never_replay and policy.replay_policy != "never":
                raise ValueError(
                    "Plan Policy 降低了 Tool/Capability 的 replay_policy："
                    f"{intent}"
                )
            if effective_approval and not policy.requires_approval:
                raise ValueError(
                    f"Plan Policy 降低了 Tool/Capability 审批要求：{intent}"
                )
            if entry is not None:
                if not set(policy.capabilities).issubset(entry.capabilities):
                    raise ValueError(
                        f"Plan Policy Capability 与绑定 Tool 不匹配：{intent}"
                    )
                if policy.write != entry.has_side_effect:
                    raise ValueError(
                        f"Plan Policy 副作用属性与绑定 Tool 不匹配：{intent}"
                    )

    async def __call__(
        self,
        step: PlanStep,
        cancellation: CancellationToken,
        *,
        fencing_token: int | None = None,
        context: Any | None = None,
    ) -> Any:
        cancellation.throw_if_cancelled()
        tool_name = self._intent_tools.get(step.intent)
        if tool_name is None:
            raise PlanToolDispatchError(
                f"Plan Intent 未绑定可信 Tool：{step.intent}"
            )
        tool = self._tools[tool_name]
        runtime = self._trusted_runtime_for(tool)
        contract = self._tool_contracts[tool_name]
        if step.replay_policy != contract.replay_policy:
            raise PlanToolDispatchError(
                f"Plan Step 与 Tool replay_policy 不一致：{tool_name}"
            )
        if step.write and contract.replay_policy != "never":
            raise PlanToolDispatchError(
                f"写 Plan Step 不能绑定可重放 Tool：{tool_name}"
            )
        resolved_arguments = getattr(
            context, "resolved_arguments", step.arguments
        )
        approval_receipt = getattr(
            context, "approval_receipt", None
        )
        plan_id = getattr(context, "plan_id", None)
        authorization_action_hash = getattr(
            context, "authorization_action_hash", None
        )
        fenced_claim = getattr(context, "fenced_claim", None)
        fenced_claim_lease_seconds = getattr(
            context, "fenced_claim_lease_seconds", None
        )
        tool_attempt_reserver = getattr(context, "tool_attempt_reserver", None)
        if (
            step.requires_approval
            or step.intent in self._approval_required_intents
        ) and approval_receipt is None:
            raise PlanToolDispatchError("审批 Plan Step 缺少可验证 Receipt")

        base = self._dispatch_context_provider()
        if not isinstance(base, ToolDispatchContext):
            raise TypeError("dispatch_context_provider 必须返回 ToolDispatchContext")
        effective_fence = (
            getattr(context, "fencing_token", None)
            if context is not None
            else fencing_token
        )
        if effective_fence is None:
            effective_fence = fencing_token
        execution_identity = getattr(context, "identity", None)
        if (
            execution_identity is not None
            and base.identity is not None
            and execution_identity != base.identity
        ):
            raise PlanToolDispatchError(
                "Plan Execution Identity 与 Host Tool Identity 不一致"
            )
        effective_scope = getattr(context, "fencing_scope", None)
        scope_plan_id = plan_id or "unknown-plan"
        dispatch_context = ToolDispatchContext(
            identity=execution_identity or base.identity,
            approval=copy.deepcopy(approval_receipt),
            tenant_id=base.tenant_id,
            fencing_token=effective_fence,
            fencing_scope=(
                effective_scope
                or f"plan_execution:{self._session_id}:plan:{scope_plan_id}"
                if effective_fence is not None
                else None
            ),
        )
        if step.write:
            return await self._dispatch_write(
                step,
                tool,
                runtime,
                resolved_arguments,
                approval_receipt,
                plan_id,
                authorization_action_hash,
                fenced_claim,
                fenced_claim_lease_seconds,
                tool_attempt_reserver,
                dispatch_context,
                cancellation,
            )
        return await self._dispatch_once(
            step,
            tool,
            runtime,
            resolved_arguments,
            dispatch_context,
            cancellation,
            tool_attempt_reserver=tool_attempt_reserver,
        )

    async def build_authorization_action(
        self,
        step: PlanStep,
        context: Any,
    ) -> dict[str, Any]:
        """Return the exact trusted action hashed by the Approval Barrier."""

        tool_name = self._intent_tools.get(step.intent)
        if tool_name is None:
            raise PlanToolDispatchError(
                f"Plan Intent 未绑定可信 Tool：{step.intent}"
            )
        self._trusted_runtime_for(self._tools[tool_name])
        resolved = copy.deepcopy(dict(context.resolved_arguments))
        action: dict[str, Any] = {
            "planId": context.plan_id,
            "stepId": step.step_id,
            "intent": step.intent,
            "tool": tool_name,
            "arguments": resolved,
            "write": step.write,
            "replayPolicy": step.replay_policy,
            "capabilities": list(step.capabilities),
        }
        if step.write:
            metadata = await self._resolve_write_metadata(step, resolved)
            self._bind_write_metadata(action, metadata)
        return action

    async def _dispatch_write(
        self,
        step: PlanStep,
        tool: AgentTool,
        runtime: ToolDispatchRuntime,
        resolved_arguments: Mapping[str, Any],
        approval_receipt: Any,
        plan_id: str | None,
        authorization_action_hash: str | None,
        fenced_claim: Any | None,
        fenced_claim_lease_seconds: float | None,
        tool_attempt_reserver: Callable[[int], Any] | None,
        dispatch_context: ToolDispatchContext,
        cancellation: CancellationToken,
    ) -> Any:
        if self._write_service_provider is None or self._write_metadata_provider is None:
            raise PlanToolDispatchError(
                "写 Plan Step 缺少 WriteOperationService 或可信 Write Metadata Provider"
            )
        identity = dispatch_context.identity
        if identity is None:
            raise PlanToolDispatchError("写 Plan Step 缺少可信执行身份")
        if (
            not isinstance(approval_receipt, PlanApprovalReceipt)
            or approval_receipt.consumed_at is None
        ):
            raise PlanToolDispatchError("写 Plan Step 缺少已消费 Approval Receipt")
        if (
            plan_id is None
            or approval_receipt.plan_id != plan_id
            or approval_receipt.step_id != step.step_id
            or authorization_action_hash is None
            or approval_receipt.action_hash != authorization_action_hash
        ):
            raise PlanToolDispatchError("写 Plan Step 与 Approval Receipt 绑定不一致")
        if step.approval_roles and set(step.approval_roles).isdisjoint(
            approval_receipt.approver_roles
        ):
            raise PlanToolDispatchError("写 Plan Step Approval Receipt 缺少所需角色")
        metadata = await self._resolve_write_metadata(step, resolved_arguments)
        current_action = {
            "planId": plan_id,
            "stepId": step.step_id,
            "intent": step.intent,
            "tool": tool.name,
            "arguments": copy.deepcopy(dict(resolved_arguments)),
            "write": step.write,
            "replayPolicy": step.replay_policy,
            "capabilities": list(step.capabilities),
        }
        self._bind_write_metadata(current_action, metadata)
        if self._action_hash(current_action) != authorization_action_hash:
            raise PlanToolDispatchError(
                "Write Metadata Provider 在审批后发生变化，禁止执行"
            )
        service = self._write_service_provider()
        if not isinstance(service, WriteOperationService):
            raise PlanToolDispatchError(
                "write_service_provider 必须返回 WriteOperationService"
            )
        if service.store.supports_cross_process_claims and fenced_claim is None:
            raise PlanToolDispatchError(
                "跨进程 Plan Write 缺少完整 Plan Execution ClaimLease"
            )
        fenced_options = (
            {}
            if fenced_claim is None
            else {
                "fenced_claim": fenced_claim,
                "fenced_claim_lease_seconds": fenced_claim_lease_seconds,
                "fenced_plan_id": plan_id,
                "fenced_step_id": step.step_id,
            }
        )
        authorization = TrustedWriteAuthorization(
            receipt_id=approval_receipt.receipt_id,
            action_hash=authorization_action_hash,
            verification_id=approval_receipt.verification_id,
            consumed_at=approval_receipt.consumed_at,
            expires_at=approval_receipt.expires_at,
            subject={
                "source": "plan_approval_receipt",
                "approvalId": approval_receipt.approval_id,
                "planId": approval_receipt.plan_id,
                "stepId": approval_receipt.step_id,
                "approverId": approval_receipt.approver_id,
                "approverRoles": sorted(approval_receipt.approver_roles),
                "approverIssuer": approval_receipt.approver_issuer,
                "identityVerificationId": approval_receipt.identity_verification_id,
            },
        )
        try:
            write = await service.prepare(
                session_id=self._session_id,
                operation_id=metadata.operation_id,
                tool_name=tool.name,
                arguments=copy.deepcopy(dict(resolved_arguments)),
                idempotency_key=metadata.idempotency_key,
                requester=identity,
                requires_approval=False,
                entity_id=metadata.entity_id,
                expected_entity_version=metadata.expected_entity_version,
                business_preconditions=(
                    None
                    if metadata.business_preconditions is None
                    else copy.deepcopy(dict(metadata.business_preconditions))
                ),
                trusted_authorization=authorization,
                authorization_action_hash=authorization_action_hash,
                **fenced_options,
            )
        except Exception as error:
            raise PlanToolDispatchError(
                public_error_message(
                    error,
                    fallback="Plan write preparation failed",
                ),
                definitely_not_committed=True,
                error_type=type(error).__name__,
            ) from error
        if write.state in {"outcome_unknown", "reconciling"}:
            if metadata.reconcile_handler is None:
                raise PlanToolDispatchError(
                    "Plan Write 结果未知且未配置 Reconciliation Adapter",
                    outcome_unknown=True,
                )
            write = await service.reconcile(write.write_id, metadata.reconcile_handler)
        if write.state == "succeeded":
            return copy.deepcopy(write.result)
        if write.state == "failed":
            raise PlanToolDispatchError(
                "Plan Write 已持久化为失败",
                durably_failed=True,
            )
        if write.state not in {"approved"}:
            raise PlanToolDispatchError(
                f"Plan Write 当前状态不可自动执行：{write.state}",
                outcome_unknown=write.state in {"submitting", "outcome_unknown", "reconciling"},
                definitely_not_committed=write.state
                in {"prepared", "waiting_approval", "approved"},
            )

        async def execute_write(write_context):
            fenced = ToolDispatchContext(
                identity=dispatch_context.identity,
                approval=copy.deepcopy(dispatch_context.approval),
                tenant_id=dispatch_context.tenant_id,
                fencing_token=write_context.fencing_token,
                fencing_scope=write_context.fencing_scope,
            )
            return await self._dispatch_once(
                step,
                tool,
                runtime,
                write_context.arguments,
                fenced,
                cancellation,
                tool_attempt_reserver=tool_attempt_reserver,
            )

        try:
            completed = await service.execute(
                write.write_id,
                actor=identity,
                idempotency_key=metadata.idempotency_key,
                context_handler=execute_write,
                fencing_token=dispatch_context.fencing_token,
                **fenced_options,
            )
        except Exception as error:
            try:
                durable = await service.get(write.write_id)
            except Exception:
                durable = None
            if durable is not None and durable.state == "failed":
                raise PlanToolDispatchError(
                    public_error_message(
                        error,
                        fallback="Plan write execution failed",
                    ),
                    durably_failed=True,
                    error_type=type(error).__name__,
                ) from error
            outcome_unknown = durable is None or is_outcome_unknown_error(error) or (
                durable.state
                in {"submitting", "outcome_unknown", "reconciling", "succeeded"}
            )
            raise PlanToolDispatchError(
                public_error_message(
                    error,
                    fallback=(
                        "Plan write outcome is unknown"
                        if outcome_unknown
                        else "Plan write execution failed"
                    ),
                ),
                outcome_unknown=outcome_unknown,
                definitely_not_committed=not outcome_unknown,
                error_type=type(error).__name__,
            ) from error
        return copy.deepcopy(completed.result)

    async def _resolve_write_metadata(
        self,
        step: PlanStep,
        resolved_arguments: Mapping[str, Any],
    ) -> PlanWriteMetadata:
        if self._write_metadata_provider is None:
            raise PlanToolDispatchError(
                "写 Plan Step 缺少可信 Write Metadata Provider"
            )
        value = self._write_metadata_provider(
            step,
            copy.deepcopy(dict(resolved_arguments)),
        )
        metadata = await value if inspect.isawaitable(value) else value
        if not isinstance(metadata, PlanWriteMetadata):
            raise PlanToolDispatchError(
                "Write Metadata Provider 必须返回 PlanWriteMetadata"
            )
        return metadata

    @staticmethod
    def _bind_write_metadata(
        action: dict[str, Any],
        metadata: PlanWriteMetadata,
    ) -> None:
        action.update(
            {
                "operationId": metadata.operation_id,
                "idempotencyKey": metadata.idempotency_key,
                "entityId": metadata.entity_id,
                "expectedEntityVersion": metadata.expected_entity_version,
                "businessPreconditions": (
                    None
                    if metadata.business_preconditions is None
                    else copy.deepcopy(dict(metadata.business_preconditions))
                ),
            }
        )

    @staticmethod
    def _action_hash(action: Mapping[str, Any]) -> str:
        try:
            encoded = json.dumps(
                dict(action),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise PlanToolDispatchError(
                "Plan Tool authorization action 必须是严格 JSON"
            ) from error
        return hashlib.sha256(encoded).hexdigest()

    async def _dispatch_once(
        self,
        step: PlanStep,
        tool: AgentTool,
        runtime: ToolDispatchRuntime,
        resolved_arguments: Mapping[str, Any],
        dispatch_context: ToolDispatchContext,
        cancellation: CancellationToken,
        *,
        tool_attempt_reserver: Callable[[int], Any] | None = None,
    ) -> Any:
        tool_call = {
            "type": "toolCall",
            "id": f"plan-{step.action_hash[:24]}",
            "name": tool.name,
            "arguments": copy.deepcopy(dict(resolved_arguments)),
        }
        agent_context = AgentContext(
            system_prompt="durable plan tool dispatch",
            messages=[],
            tools=[tool],
        )
        async def admit_attempt(actual_tool: AgentTool, attempt: int) -> None:
            if actual_tool is not tool:
                raise PlanToolDispatchError(
                    "Tool Attempt Admission 收到非绑定 Tool"
                )
            if tool_attempt_reserver is None:
                return
            value = tool_attempt_reserver(attempt)
            if inspect.isawaitable(value):
                await value

        outcome = await runtime.dispatch(
            tool_call,
            context=agent_context,
            assistant_message=assistant_message(
                model=self._model,
                content=[copy.deepcopy(tool_call)],
                stop_reason="toolUse",
            ),
            dispatch_context=dispatch_context,
            cancellation=cancellation,
            emit_messages=False,
            attempt_admission=admit_attempt,
        )
        if outcome.is_error:
            raise PlanToolDispatchError(
                _tool_error_text(outcome.result),
                outcome_unknown=_is_outcome_unknown(outcome.result.details),
            )
        return {
            "content": copy.deepcopy(outcome.result.content),
            "details": copy.deepcopy(outcome.result.details),
            "usage": copy.deepcopy(outcome.result.usage),
        }

    def _trusted_runtime_for(self, tool: AgentTool) -> ToolDispatchRuntime:
        expected = self._tool_contracts[tool.name]
        if ToolSecurityContract.capture(tool) != expected:
            raise PlanToolDispatchError(
                f"Plan Tool {tool.name} 的安全合同在装配后发生变化"
            )
        runtime = self._runtime_provider()
        if not isinstance(runtime, ToolDispatchRuntime):
            raise PlanToolDispatchError(
                "runtime_provider 必须返回 ToolDispatchRuntime"
            )
        try:
            runtime.assert_registered_tool(
                tool,
                expected_contract_digest=expected.digest,
            )
        except Exception as error:
            raise PlanToolDispatchError(
                f"Plan Tool {tool.name} 与 Runtime 受信注册不一致"
            ) from error
        return runtime


def _tool_error_text(result: Any) -> str:
    for block in getattr(result, "content", ()):
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            return block["text"]
    return "Plan Tool 执行失败"


def _is_outcome_unknown(details: Any) -> bool:
    if not isinstance(details, Mapping):
        return False
    normalized = {
        str(key).replace("_", "").casefold(): value
        for key, value in details.items()
    }
    return normalized.get("outcomeunknown") is True or normalized.get("code") == "outcome_unknown"


__all__ = [
    "PlanWriteMetadata",
    "PlanWriteMetadataProvider",
    "PlanToolDispatchError",
    "ToolRuntimePlanStepExecutor",
]
