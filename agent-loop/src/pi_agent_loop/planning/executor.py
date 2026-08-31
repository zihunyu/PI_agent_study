"""依赖感知、审批感知且可恢复的 Multi Intent PlanExecutor。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, cast

from ..cancellation import CancellationToken, OperationCancelledError
from ..messages import public_error_message
from ..security import VerifiedIdentity
from ..session.operation_store import ClaimLease
from .approval import ApprovalBarrier
from .dataflow import (
    dependency_results,
    evaluate_preconditions,
    resolve_step_arguments,
    result_digest,
    validate_step_result,
)
from .graph import DependencyGraph, PlanValidator
from .resources import PlanStepResourceReserver
from .state_machine import TaskStateMachine
from .synthesizer import ResultSynthesizer
from .types import (
    IntentPlanPolicy,
    MultiIntentPlan,
    PlanEvent,
    PlanExecutionError,
    PlanExecutionResult,
    PlanExecutionState,
    PlanStep,
    PlanStepExecutionContext,
    PlanValidationError,
)


StepExecutor = Callable[..., Any]
PlanEventSink = Callable[[PlanEvent], Any]


class PlanExecutor:
    def __init__(
        self,
        plan: MultiIntentPlan,
        policies: dict[str, IntentPlanPolicy],
        step_executor: StepExecutor,
        *,
        approval_barrier: ApprovalBarrier | None = None,
        result_synthesizer: ResultSynthesizer | None = None,
        event_sink: PlanEventSink | None = None,
        max_parallel_steps: int = 4,
        fail_fast: bool = False,
        clock: Callable[[], float] = time.time,
        fencing_token: int | None = None,
        fencing_scope: str | None = None,
        identity: VerifiedIdentity | None = None,
        fenced_claim: ClaimLease | None = None,
        fenced_claim_lease_seconds: float | None = None,
        resource_reserver: PlanStepResourceReserver | None = None,
    ) -> None:
        if (
            isinstance(max_parallel_steps, bool)
            or not isinstance(max_parallel_steps, int)
            or max_parallel_steps < 1
        ):
            raise ValueError("max_parallel_steps 必须是大于 0 的整数")
        self.plan = plan
        self.validator = PlanValidator(policies)
        self.graph: DependencyGraph = self.validator.validate(plan)
        self.machine = TaskStateMachine(plan, self.graph)
        self.step_executor = step_executor
        self.approval_barrier = approval_barrier
        self.result_synthesizer = result_synthesizer or ResultSynthesizer()
        self.event_sink = event_sink
        self.max_parallel_steps = max_parallel_steps
        self.fail_fast = fail_fast
        self.clock = clock
        if fencing_token is not None and (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token < 1
        ):
            raise ValueError("Plan fencing_token 必须是正整数或 None")
        self.fencing_token = fencing_token
        if fencing_scope is not None and (
            not isinstance(fencing_scope, str) or not fencing_scope.strip()
        ):
            raise ValueError("Plan fencing_scope 必须是非空字符串或 None")
        self.fencing_scope = fencing_scope
        self.identity = identity
        if fenced_claim is not None:
            if not isinstance(fenced_claim, ClaimLease):
                raise TypeError("Plan fenced_claim 必须是 ClaimLease 或 None")
            if fencing_token != fenced_claim.fencing_token:
                raise ValueError("Plan fencing_token 与 fenced_claim 不一致")
            if (
                isinstance(fenced_claim_lease_seconds, bool)
                or not isinstance(fenced_claim_lease_seconds, (int, float))
                or fenced_claim_lease_seconds <= 0
            ):
                raise ValueError("Plan fenced_claim_lease_seconds 必须是正数")
        elif fenced_claim_lease_seconds is not None:
            raise ValueError("Plan lease_seconds 只能与 fenced_claim 一起使用")
        if resource_reserver is not None and not callable(resource_reserver):
            raise TypeError("Plan resource_reserver 必须可调用或为 None")
        self.fenced_claim = fenced_claim
        self.fenced_claim_lease_seconds = (
            None
            if fenced_claim_lease_seconds is None
            else float(fenced_claim_lease_seconds)
        )
        self.resource_reserver = resource_reserver
        self._transition_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        self._state = self.machine.initial_state()
        self._events: list[PlanEvent] = []

    @property
    def state(self) -> PlanExecutionState:
        return self._state

    async def execute(
        self,
        *,
        initial_state: PlanExecutionState | None = None,
        cancellation: CancellationToken | None = None,
    ) -> PlanExecutionResult:
        token = cancellation or CancellationToken()
        async with self._run_lock:
            # Plan 可能来自持久化恢复，也可能在构造 Executor 后才开始执行。
            # 每次执行都重新比对当前可信 Policy 和参数合同，不能只依赖 Planner
            # 创建时的那一次校验。
            self.graph = self.validator.validate(self.plan)
            self.machine = TaskStateMachine(self.plan, self.graph)
            self._events = []
            self._state = (
                self.machine.initial_state()
                if initial_state is None
                else self.machine.state_from_dict(initial_state.to_dict())
            )
            approval_queries: set[str] = set()
            await self._recover_running_steps()
            while True:
                token.throw_if_cancelled()
                changed = await self._skip_blocked_steps()
                if self.fail_fast and any(
                    value.status == "failed" for value in self._state.steps.values()
                ):
                    changed = await self._skip_all_pending("Plan fail_fast") or changed

                if self.approval_barrier is not None:
                    for step_id in self.graph.topological_order:
                        if self._state.steps[step_id].status != "waiting_approval":
                            continue
                        if step_id in approval_queries:
                            continue
                        prepared_context = await self._prepare_context(
                            self.plan.step(step_id)
                        )
                        if prepared_context is None:
                            changed = True
                            continue
                        approval_queries.add(step_id)
                        changed = (
                            await self._resolve_approval(
                                self.plan.step(step_id), token, prepared_context
                            )
                            or changed
                        )

                ready = list(self.graph.ready_steps(self._state))
                for step_id in ready:
                    step = self.plan.step(step_id)
                    current = self._state.steps[step_id]
                    if (
                        not step.requires_approval
                        or current.approval_receipt is not None
                    ):
                        continue
                    prepared_context = await self._prepare_context(step)
                    if prepared_context is None:
                        changed = True
                        continue
                    if self.approval_barrier is None:
                        await self._transition(
                            "step_waiting_approval",
                            step_id,
                            {
                                "approvalId": _stable_approval_request_id(
                                    self.plan.plan_id,
                                    step,
                                    prepared_context.authorization_action_hash,
                                ),
                                "actionHash": prepared_context.authorization_action_hash,
                            },
                        )
                        changed = True
                        continue
                    if step_id in approval_queries:
                        continue
                    approval_queries.add(step_id)
                    try:
                        changed = await self._resolve_approval(
                            step, token, prepared_context
                        ) or changed
                    except PlanExecutionError:
                        # 后端损坏响应也必须留下不可执行的恢复锚点。该稳定 ID
                        # 只是请求标识；它绝不等价于授权 Receipt。
                        if self._state.steps[step_id].status == "pending":
                            await self._transition(
                                "step_waiting_approval",
                                step_id,
                                {
                                    "approvalId": _stable_approval_request_id(
                                        self.plan.plan_id,
                                        step,
                                        prepared_context.authorization_action_hash,
                                    ),
                                    "actionHash": prepared_context.authorization_action_hash,
                                },
                            )
                        raise

                executable = [
                    step_id
                    for step_id in self.graph.ready_steps(self._state)
                    if (
                        not self.plan.step(step_id).requires_approval
                        or self._state.steps[step_id].approval_receipt is not None
                    )
                ]
                if executable:
                    await self._execute_batch(executable[: self.max_parallel_steps], token)
                    continue
                if not changed:
                    break
                # 本轮只推进到 Waiting Approval、Failed 或 Skipped，重新计算一次；
                # 如果没有新的 Ready Step，下一轮会自然结束。
                changed = False
            synthesized = self.result_synthesizer.synthesize(
                self.plan, self._state, self.graph
            )
            return PlanExecutionResult(
                state=self._state,
                synthesized=synthesized,
                events=tuple(self._events),
            )

    async def _resolve_approval(
        self,
        step: PlanStep,
        cancellation: CancellationToken,
        context: PlanStepExecutionContext,
    ) -> bool:
        if self.approval_barrier is None:
            raise PlanExecutionError("Plan 缺少 Approval Barrier")
        action_hash = context.authorization_action_hash
        if action_hash is None:
            raise PlanExecutionError("Plan Approval 缺少最终解析 Action Hash")
        authorized_step = replace(step, _execution_action_hash=action_hash)
        decision = await self.approval_barrier.authorize(
            authorized_step, self._state, cancellation
        )
        current = self._state.steps[step.step_id]
        if decision.status == "pending":
            if current.status == "waiting_approval":
                return False
            await self._transition(
                "step_waiting_approval",
                step.step_id,
                {
                    "approvalId": decision.approval_id,
                    "actionHash": decision.action_hash,
                },
            )
            return True
        if decision.status == "approved":
            await self._transition(
                "step_approval_granted",
                step.step_id,
                {
                    "approvalId": decision.approval_id,
                    "actionHash": decision.action_hash,
                    "receipt": (
                        decision.receipt.to_dict()
                        if decision.receipt is not None
                        else None
                    ),
                },
            )
            return True
        else:
            await self._transition(
                "step_approval_denied",
                step.step_id,
                {
                    "approvalId": decision.approval_id or current.approval_id,
                    "reason": decision.reason,
                    "actionHash": decision.action_hash,
                },
            )
            return True

    async def _recover_running_steps(self) -> None:
        for step_id in self.graph.topological_order:
            if self._state.steps[step_id].status != "running":
                continue
            step = self.plan.step(step_id)
            await self._transition(
                "step_recovered",
                step_id,
                {
                    "targetStatus": (
                        "pending"
                        if step.replay_policy == "safe"
                        else "manual_intervention"
                    )
                },
            )

    async def _skip_blocked_steps(self) -> bool:
        changed = False
        while True:
            blocked = self.graph.blocked_steps(self._state)
            if not blocked:
                return changed
            for step_id in blocked:
                failed_dependencies = [
                    dependency
                    for dependency in self.graph.dependencies[step_id]
                    if self._state.steps[dependency].status
                    in {
                        "failed",
                        "skipped",
                        "not_applicable",
                        "manual_intervention",
                    }
                ]
                await self._transition(
                    "step_skipped",
                    step_id,
                    {
                        "reason": (
                            "依赖未成功：" + ",".join(sorted(failed_dependencies))
                        )
                    },
                )
                changed = True

    async def _skip_all_pending(self, reason: str) -> bool:
        changed = False
        for step_id in self.graph.topological_order:
            if self._state.steps[step_id].status not in {
                "pending",
                "waiting_approval",
            }:
                continue
            await self._transition("step_skipped", step_id, {"reason": reason})
            changed = True
        return changed

    async def _execute_batch(
        self,
        step_ids: list[str],
        cancellation: CancellationToken,
    ) -> None:
        tasks = [
            asyncio.create_task(
                self._execute_one(self.plan.step(step_id), cancellation),
                name=f"plan-step:{self.plan.plan_id}:{step_id}",
            )
            for step_id in step_ids
        ]
        batch = asyncio.create_task(
            _wait_for_all(tasks),
            name=f"plan-batch:{self.plan.plan_id}",
        )
        cancellation_wait = asyncio.create_task(
            cancellation.wait(),
            name=f"plan-batch-cancellation:{self.plan.plan_id}",
        )
        try:
            await asyncio.wait(
                {batch, cancellation_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation.cancelled and not batch.done():
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                cancellation.throw_if_cancelled()
            await batch
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if not batch.done():
                batch.cancel()
            await asyncio.gather(batch, return_exceptions=True)
            raise
        finally:
            if not cancellation_wait.done():
                cancellation_wait.cancel()
            await asyncio.gather(cancellation_wait, return_exceptions=True)

    async def _execute_one(
        self,
        step: PlanStep,
        cancellation: CancellationToken,
    ) -> None:
        cancellation.throw_if_cancelled()
        # 在真正派发 Handler 前再做一次 fail-closed 校验，覆盖恢复路径，
        # 也防止调用方在计划创建和执行之间改动嵌套 arguments。
        self.validator.validate_step(step)
        context = await self._prepare_context(step)
        if context is None:
            return
        started_data: dict[str, Any] = {}
        if step.requires_approval:
            current = self._state.steps[step.step_id]
            receipt = current.approval_receipt
            if receipt is None:
                raise PlanExecutionError(
                    "写 Plan Step 缺少已消费的 PlanApprovalReceipt"
                )
            if receipt.action_hash != context.authorization_action_hash:
                raise PlanExecutionError(
                    "写 Plan Step 最终参数或业务写绑定已变化，"
                    "Approval Receipt 不再匹配"
                )
            started_data = {
                "approvalReceiptId": receipt.receipt_id,
                "startedAt": _clock_value(self.clock),
            }
        if self.resource_reserver is not None:
            resource_reserver = self.resource_reserver
            current = self._state.steps[step.step_id]
            step_attempt = current.attempts + 1
            counts_as_tool_call = bool(
                getattr(self.step_executor, "counts_as_tool_call", False)
            )
            if counts_as_tool_call and not bool(
                getattr(
                    self.step_executor,
                    "supports_tool_attempt_admission",
                    False,
                )
            ):
                raise PlanExecutionError(
                    "Tool-backed Plan Step Executor 必须在每次 Tool Retry "
                    "Attempt 前执行 durable admission"
                )
            reservation = resource_reserver(
                step,
                step_attempt,
                None,
            )
            if inspect.isawaitable(reservation):
                await cast(Awaitable[Any], reservation)
            if counts_as_tool_call:
                async def reserve_tool_attempt(tool_attempt: int) -> None:
                    value = resource_reserver(
                        step,
                        step_attempt,
                        tool_attempt,
                    )
                    if inspect.isawaitable(value):
                        await cast(Awaitable[Any], value)

                context = replace(
                    context,
                    tool_attempt_reserver=reserve_tool_attempt,
                )
        await self._transition("step_started", step.step_id, started_data)
        try:
            if _accepts_execution_context(self.step_executor):
                dispatch_options: dict[str, Any] = {"context": context}
                if _accepts_fencing_token(self.step_executor):
                    dispatch_options["fencing_token"] = self.fencing_token
                value = self.step_executor(
                    step,
                    cancellation,
                    **dispatch_options,
                )
            elif _accepts_fencing_token(self.step_executor):
                if step.argument_bindings or step.preconditions:
                    raise PlanExecutionError(
                        "包含数据流或条件的 Plan Step 必须使用 execution context 接口"
                    )
                value = self.step_executor(
                    step,
                    cancellation,
                    fencing_token=self.fencing_token,
                )
            else:
                if (
                    (step.write or step.replay_policy == "never")
                    and self.fencing_token is not None
                ):
                    raise PlanExecutionError(
                        "write/never Plan Step 必须使用 execution context 或 fencing 接口"
                    )
                # 旧签名只兼容 safe/read；它拿到的是替换完依赖参数的副本。
                resolved_step = replace(
                    step,
                    arguments=context.resolved_arguments,
                    argument_bindings=(),
                    preconditions=(),
                )
                value = self.step_executor(resolved_step, cancellation)
            result = await cast(Awaitable[Any], value) if inspect.isawaitable(value) else value
        except (asyncio.CancelledError, OperationCancelledError):
            # Started 已经是恢复锚点；安全重放或人工介入由下一次 execute 决定。
            raise
        except Exception as error:
            event_type = (
                "step_outcome_unknown"
                if (
                    not getattr(error, "durably_failed", False)
                    and not getattr(error, "definitely_not_committed", False)
                    and (
                        getattr(error, "outcome_unknown", False)
                        or step.replay_policy == "never"
                        or step.write
                    )
                )
                else "step_failed"
            )
            await self._transition(
                event_type,
                step.step_id,
                _exception_event_data(
                    error,
                    code=(
                        "step_outcome_unknown"
                        if event_type == "step_outcome_unknown"
                        else "step_execution_failed"
                    ),
                    fallback=(
                        "Plan step outcome is unknown"
                        if event_type == "step_outcome_unknown"
                        else "Plan step execution failed"
                    ),
                ),
            )
            return

        if step.result_contract is not None:
            try:
                validate_step_result(result, step.result_contract)
            except Exception as error:
                await self._transition(
                    "step_validation_failed",
                    step.step_id,
                    _exception_event_data(
                        error,
                        code="step_result_validation_failed",
                        fallback="Plan step result validation failed",
                    ),
                )
                return
            await self._transition(
                "step_validation_passed",
                step.step_id,
                {"resultDigest": result_digest(result)},
            )

        # Handler 已经返回以后，外部副作用与成功事实落盘属于两个不同的
        # 故障边界。成功事件持久化失败时绝不能落成 step_failed：恢复时，
        # safe Step 可以重新执行，never/write Step 会进入 manual_intervention。
        cancellation.throw_if_cancelled()
        await self._transition(
            "step_succeeded",
            step.step_id,
            {"result": result},
        )

    async def _prepare_context(
        self,
        step: PlanStep,
    ) -> PlanStepExecutionContext | None:
        current = self._state.steps[step.step_id]
        if current.status not in {"pending", "waiting_approval"}:
            return None
        try:
            results = dependency_results(step, self._state)
            resolved = resolve_step_arguments(step, results)
        except (PlanExecutionError, PlanValidationError) as error:
            await self._transition(
                "step_arguments_invalid",
                step.step_id,
                _exception_event_data(
                    error,
                    code="step_arguments_invalid",
                    fallback="Plan step arguments are invalid",
                ),
            )
            return None
        try:
            evaluations = evaluate_preconditions(step, results)
        except (PlanExecutionError, PlanValidationError) as error:
            await self._transition(
                "step_precondition_failed",
                step.step_id,
                _exception_event_data(
                    error,
                    code="step_precondition_evaluation_failed",
                    fallback="Plan step precondition evaluation failed",
                ),
            )
            return None
        failed = [item.description for item in evaluations if not item.passed]
        if failed:
            await self._transition(
                "step_not_applicable",
                step.step_id,
                {"reason": "可信前置条件不满足：" + ",".join(failed)},
            )
            return None
        context = PlanStepExecutionContext(
            plan_id=self.plan.plan_id,
            step=step,
            resolved_arguments=resolved,
            dependency_results=results,
            approval_receipt=current.approval_receipt,
            identity=self.identity,
            fencing_token=self.fencing_token,
            fencing_scope=self.fencing_scope,
            fenced_claim=self.fenced_claim,
            fenced_claim_lease_seconds=self.fenced_claim_lease_seconds,
        )
        if not (step.requires_approval or step.write):
            return context
        try:
            action_hash = await self._authorization_action_hash(step, context)
        except (asyncio.CancelledError, OperationCancelledError):
            raise
        except Exception as error:
            await self._transition(
                "step_arguments_invalid",
                step.step_id,
                _exception_event_data(
                    error,
                    code="step_authorization_action_invalid",
                    fallback="Plan step authorization action is invalid",
                ),
            )
            return None
        return replace(context, authorization_action_hash=action_hash)

    async def _authorization_action_hash(
        self,
        step: PlanStep,
        context: PlanStepExecutionContext,
    ) -> str:
        builder = getattr(self.step_executor, "build_authorization_action", None)
        if callable(builder):
            value = builder(step, context)
            payload = (
                await cast(Awaitable[Any], value)
                if inspect.isawaitable(value)
                else value
            )
        else:
            # Preserve existing hashes for a callback step whose executable
            # arguments are already fully literal. Dataflow-bound arguments use
            # the resolved canonical payload below and therefore cannot reuse a
            # receipt issued for the unresolved template.
            if (
                not step.argument_bindings
                and dict(context.resolved_arguments) == dict(step.arguments)
            ):
                return step.action_hash
            payload = {
                "planId": self.plan.plan_id,
                "stepId": step.step_id,
                "intent": step.intent,
                "arguments": copy.deepcopy(dict(context.resolved_arguments)),
                "write": step.write,
                "replayPolicy": step.replay_policy,
                "capabilities": list(step.capabilities),
            }
        if not isinstance(payload, dict):
            raise PlanExecutionError(
                "Plan authorization action builder 必须返回严格 JSON 对象"
            )
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise PlanExecutionError(
                "Plan authorization action 必须是严格 JSON"
            ) from error
        return hashlib.sha256(encoded).hexdigest()

    async def _transition(
        self,
        event_type: str,
        step_id: str,
        data: dict[str, Any],
    ) -> None:
        async with self._transition_lock:
            event = PlanEvent(
                sequence=self._state.version + 1,
                type=event_type,
                step_id=step_id,
                data=data,
            )
            next_state = self.machine.apply(self._state, event)
            if self.event_sink is not None:
                value = self.event_sink(event)
                if inspect.isawaitable(value):
                    await cast(Awaitable[Any], value)
            self._state = next_state
            self._events.append(event)


def _exception_event_data(
    error: BaseException,
    *,
    code: str,
    fallback: str,
) -> dict[str, Any]:
    """Create a persistable error payload without retaining exception text."""

    data = {
        "errorCode": code,
        "error": public_error_message(error, fallback=fallback),
        "errorType": type(error).__name__,
    }
    if error.__cause__ is not None:
        data["causeType"] = type(error.__cause__).__name__
    return data


def _accepts_fencing_token(callback: Callable[..., Any]) -> bool:
    """Return whether a callback opted into the additive fencing API."""

    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        return False
    explicit = parameters.get("fencing_token")
    if explicit is not None and explicit.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _accepts_execution_context(callback: Callable[..., Any]) -> bool:
    """Return whether a callback opted into resolved trusted Plan input."""

    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        return False
    explicit = parameters.get("context")
    if explicit is not None and explicit.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _stable_approval_request_id(
    plan_id: str,
    step: PlanStep,
    action_hash: str | None = None,
) -> str:
    """生成可恢复的审批请求 ID；该 ID 本身绝不代表授权。"""

    encoded = "\x1f".join(
        (plan_id, step.step_id, action_hash or step.action_hash)
    ).encode("utf-8")
    return "plan-approval-request-" + hashlib.sha256(encoded).hexdigest()


async def _wait_for_all(tasks: list[asyncio.Task[None]]) -> None:
    await asyncio.gather(*tasks)


def _clock_value(clock: Callable[[], float]) -> float:
    value = clock()
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise PlanExecutionError("PlanExecutor clock 必须返回有限时间戳")
    return float(value)
