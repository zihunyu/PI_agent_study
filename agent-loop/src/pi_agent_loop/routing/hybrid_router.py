"""使用大模型理解自然语言、使用 Host 配置约束业务边界的混合 Router。"""

from __future__ import annotations

import copy
import inspect
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Any, cast

from ..cancellation import CancellationToken, OperationCancelledError
from ..types import Model, StreamFn
from .capabilities import CapabilityRegistry
from .clarification import ClarificationState, ClarificationStateStore
from .simple_config import SimpleBusinessConfig, SimpleIntent
from .types import (
    JsonValue,
    RequestDecision,
    RouteAuthorizationContext,
    RouteAuthorizationDecision,
    TaskDecision,
    TaskDependencyHint,
    ToolChoicePolicy,
    highest_risk_level,
)

_ROUTE_TOOL_NAME = "select_business_intent"
_OUT_OF_SCOPE = "__out_of_scope__"
_GENERAL_QA = "__general_qa__"
_DENIED_PREFIX = "__denied__:"

RouteAuthorizationPolicy = Callable[
    [RouteAuthorizationContext],
    RouteAuthorizationDecision | Awaitable[RouteAuthorizationDecision],
]


class HybridModelRouter:
    """模型负责语言映射，配置、Capability 和 Host 负责最终决定。"""

    def __init__(
        self,
        config: SimpleBusinessConfig,
        capabilities: CapabilityRegistry,
        *,
        model: Model,
        stream_fn: StreamFn,
        confidence_threshold: float = 0.65,
        max_intents: int = 1,
        retry_event_sink: Any | None = None,
        authorization_policy: RouteAuthorizationPolicy | None = None,
        clarification_store: ClarificationStateStore | None = None,
        clarification_ttl_seconds: float = 300.0,
        clarification_clock: Callable[[], float] = time.time,
    ) -> None:
        if not 0 <= confidence_threshold <= 1:
            raise ValueError("confidence_threshold 必须在 0 到 1 之间")
        if (
            isinstance(max_intents, bool)
            or not isinstance(max_intents, int)
            or not 1 <= max_intents <= 32
        ):
            raise ValueError("max_intents 必须是 1 到 32 之间的整数")
        if (
            isinstance(clarification_ttl_seconds, bool)
            or not isinstance(clarification_ttl_seconds, (int, float))
            or not math.isfinite(float(clarification_ttl_seconds))
            or clarification_ttl_seconds <= 0
        ):
            raise ValueError("clarification_ttl_seconds 必须是有限正数")
        if not callable(clarification_clock):
            raise TypeError("clarification_clock 必须可调用")
        if clarification_store is not None and not all(
            callable(getattr(clarification_store, name, None))
            for name in ("load", "save", "clear")
        ):
            raise TypeError("clarification_store 必须实现 load/save/clear")
        self.config = config
        self.capabilities = capabilities
        self.model = model
        self.stream_fn = stream_fn
        self.confidence_threshold = confidence_threshold
        self.max_intents = max_intents
        self.retry_event_sink = retry_event_sink
        self.authorization_policy = authorization_policy
        self.clarification_store = clarification_store
        self.clarification_ttl_seconds = float(clarification_ttl_seconds)
        self._clarification_clock = clarification_clock
        self._durable_metadata_provider: Callable[[], Mapping[str, Any]] | None = None
        self.call_count = 0
        self._evaluation_metrics: dict[str, int | float] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cost": 0.0,
        }

    def bind_runtime(
        self,
        *,
        stream_fn: StreamFn,
        retry_event_sink: Any | None,
        durable_metadata_provider: Callable[[], Mapping[str, Any]] | None = None,
    ) -> HybridModelRouter:
        """Create an independent per-Host router bound to one model runtime.

        A router passed to ``DurableAgentHost.create`` is caller-owned and may
        be reused as a configuration template.  Mutating that object would let
        a later Host replace the stream and journal callbacks used by an
        earlier Host.  The shallow copy intentionally shares the immutable
        business configuration and capability registry, while all known
        request-scoped state is reset on the bound instance.

        Subclasses with additional mutable request state should override this
        method and clone that state as well.
        """

        bound = copy.copy(self)
        bound.stream_fn = stream_fn
        bound.retry_event_sink = retry_event_sink
        bound._durable_metadata_provider = durable_metadata_provider
        bound.call_count = 0
        bound._evaluation_metrics = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cost": 0.0,
        }
        return bound

    async def route(
        self,
        user_text: str,
        *,
        cancellation: CancellationToken | None = None,
        session_id: str | None = None,
        tenant_id: str | None = None,
    ) -> RequestDecision:
        token = cancellation or CancellationToken()
        token.throw_if_cancelled()
        # Rule-only routes and failed calls must not inherit metrics from the
        # preceding evaluation case.
        self._evaluation_metrics = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cost": 0.0,
        }
        try:
            scope = self._clarification_scope(
                session_id=session_id,
                tenant_id=tenant_id,
            )
        except Exception:
            return self._clarification_state_unavailable("无法读取可信澄清作用域")
        if self.clarification_store is not None and scope is None:
            return self._clarification_state_unavailable(
                "已启用澄清状态，但缺少可信 tenant_id/session_id"
            )
        pending: ClarificationState | None = None
        if self.clarification_store is not None and scope is not None:
            try:
                pending = await self.clarification_store.load(*scope)
            except Exception:
                return self._clarification_state_unavailable("澄清状态存储暂时不可用")
        text = user_text.strip()
        if not text:
            decision = RequestDecision(
                status="in_scope_need_clarification",
                reason="用户请求为空",
                message=(
                    self._pending_clarification_message(pending)
                    if pending is not None
                    else "请输入要处理的问题。"
                ),
                missing_fields=(
                    pending.missing_fields if pending is not None else ("user_request",)
                ),
                routing_source="rule",
            )
            return await self._sync_clarification_state(decision, scope, pending)

        # 明确写在 denied.examples 中的高风险表达先确定性阻止，不消耗模型。
        denied = self._match_denied_example(text)
        if denied is not None:
            decision = RequestDecision(
                status="prohibited",
                reason=denied.description,
                message=denied.message,
                routing_source="rule",
                confidence=1.0,
            )
            return await self._sync_clarification_state(decision, scope, pending)

        try:
            classification = await self._classify(
                text,
                cancellation=token,
                pending_state=pending,
            )
        except OperationCancelledError:
            raise
        except Exception:
            # Provider 可能把协作式取消规范成 aborted 响应；取消不能被下面的
            # fail-closed clarification 吞掉。
            token.throw_if_cancelled()
            # 分类器失败必须 fail-closed，不能退回“让回答模型随便决定”。
            return RequestDecision(
                status="in_scope_need_clarification",
                reason="HybridModelRouter 分类失败",
                message="暂时无法可靠判断该请求所属业务，请换一种说法或稍后重试。",
                routing_source="model",
            )
        classification = self._merge_pending_classification(
            classification,
            pending,
        )
        decision = self._decision_from_classification(classification)
        decision = await self._apply_authorization(decision)
        return await self._sync_clarification_state(decision, scope, pending)

    async def _apply_authorization(
        self,
        decision: RequestDecision,
    ) -> RequestDecision:
        """Apply trusted application authorization after Intent resolution.

        The classifier must first identify an Intent before an identity-aware
        policy can decide access.  A denied component makes the complete
        multi-Intent request non-executable; allowed siblings are never sent to
        the Planner or answering model as a partial request.
        """

        authorization_policy = self.authorization_policy
        if authorization_policy is None:
            return decision
        if decision.component_decisions:
            authorized = [
                await self._authorize_component(component, authorization_policy)
                for component in decision.component_decisions
            ]
            return self._combine_decisions(authorized)
        return await self._authorize_component(decision, authorization_policy)

    async def _authorize_component(
        self,
        decision: RequestDecision,
        authorization_policy: RouteAuthorizationPolicy,
    ) -> RequestDecision:
        if decision.intent is None or decision.status in {
            "permission_denied",
            "out_of_scope",
            "prohibited",
        }:
            return decision
        context = RouteAuthorizationContext(
            domain=decision.domain,
            intent=decision.intent,
            arguments=copy.deepcopy(decision.extracted_fields),
            required_capabilities=tuple(decision.required_capabilities),
            selected_tools=tuple(decision.selected_tools),
            requires_approval=decision.requires_approval,
            side_effect=decision.side_effect,
            risk=decision.risk,
            task_id=decision.task_id,
        )
        try:
            value = authorization_policy(context)
            authorization = (
                value if isinstance(value, RouteAuthorizationDecision) else await value
            )
            if not isinstance(authorization, RouteAuthorizationDecision):
                raise TypeError(
                    "authorization_policy 必须返回 RouteAuthorizationDecision"
                )
        except Exception:
            authorization = RouteAuthorizationDecision.deny(
                "authorization_policy_unavailable",
                "暂时无法可靠校验当前身份权限，已阻止该请求。",
            )
        if authorization.allowed:
            return decision
        return replace(
            decision,
            status="permission_denied",
            reason=authorization.reason,
            message=(authorization.message or "当前身份没有执行该请求的权限。"),
            selected_tools=(),
            tool_policy=ToolChoicePolicy("none"),
        )

    async def _classify(
        self,
        user_text: str,
        *,
        cancellation: CancellationToken,
        pending_state: ClarificationState | None = None,
    ) -> dict[str, Any]:
        cancellation.throw_if_cancelled()
        tool = self._routing_tool()
        context = {
            "systemPrompt": self._system_prompt(pending_state),
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": user_text}],
                }
            ],
            "tools": [tool],
        }
        options = {
            "tool_choice": {
                "type": "function",
                "function": {"name": _ROUTE_TOOL_NAME},
            },
            "cancellation_token": cancellation,
            "retry_event_sink": self.retry_event_sink,
            "model_request_source": "router",
        }
        if self._durable_metadata_provider is not None:
            metadata = self._durable_metadata_provider()
            if not isinstance(metadata, Mapping):
                raise TypeError("Router durable metadata provider 必须返回 Mapping")
            options["durable_metadata"] = {
                str(key): copy.deepcopy(value)
                for key, value in metadata.items()
                if isinstance(value, str) and value
            }
        self.call_count += 1
        value = self.stream_fn(self.model, context, options)
        stream = (
            await cast(Awaitable[Any], value) if inspect.isawaitable(value) else value
        )
        if not hasattr(stream, "__aiter__") or not hasattr(stream, "result"):
            raise TypeError("HybridModelRouter 的 stream_fn 返回值不符合事件流契约")
        async for _event in stream:
            pass
        message = await stream.result()
        cancellation.throw_if_cancelled()
        self._evaluation_metrics = _message_evaluation_metrics(message)
        if message.get("stopReason") in {"error", "aborted"}:
            raise RuntimeError("业务 Intent 分类模型请求失败")
        if message.get("stopReason") == "length":
            raise RuntimeError("业务 Intent 分类响应达到长度上限，禁止采用截断路由")
        calls = [
            block
            for block in message.get("content", [])
            if isinstance(block, dict)
            and block.get("type") == "toolCall"
            and block.get("name") == _ROUTE_TOOL_NAME
        ]
        if len(calls) != 1 or not isinstance(calls[0].get("arguments"), dict):
            raise ValueError("分类模型没有返回唯一的结构化 Intent Tool Call")
        return calls[0]["arguments"]

    def evaluation_metrics(self) -> dict[str, int | float]:
        """Return measured usage for the immediately preceding route call."""

        return dict(self._evaluation_metrics)

    def _routing_tool(self) -> dict[str, Any]:
        decisions = [intent.id for intent in self.config.intents]
        denied_values = [
            f"{_DENIED_PREFIX}{index}" for index, _ in enumerate(self.config.denied)
        ]
        decisions.extend(denied_values)
        decisions.append(_OUT_OF_SCOPE)
        if self.config.product.allow_general_questions:
            decisions.append(_GENERAL_QA)
        decision_values: list[JsonValue] = list(decisions)
        classification_schema: dict[str, JsonValue] = {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": decision_values,
                    "description": "只能选择枚举中的一个业务决定",
                },
                "arguments": {
                    "type": "object",
                    "description": (
                        "从用户原文提取的业务参数；保留字符串、数字、布尔值、"
                        "null、数组和对象的 JSON 类型，不能猜测"
                    ),
                    "additionalProperties": _json_value_schema(),
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "reason": {"type": "string"},
            },
            "required": ["decision", "arguments", "confidence", "reason"],
            "additionalProperties": False,
        }
        parameters: dict[str, JsonValue] = classification_schema
        description = "只用于把用户请求分类到已配置业务 Intent；不要回答用户问题。"
        if self.max_intents > 1:
            multi_item_schema = copy.deepcopy(classification_schema)
            multi_item_properties = multi_item_schema["properties"]
            if not isinstance(multi_item_properties, dict):
                raise RuntimeError("内部 Router schema properties 必须是对象")
            multi_item_properties.update(
                {
                    "taskId": {
                        "type": "string",
                        "description": (
                            "本次复合请求内唯一且稳定的步骤标识，例如 task_1"
                        ),
                    },
                    "dependsOn": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": self.max_intents - 1,
                        "description": (
                            "仅列出当前步骤确实依赖的 taskId；无依赖时为空数组"
                        ),
                    },
                }
            )
            required_fields = classification_schema["required"]
            if not isinstance(required_fields, list):
                raise RuntimeError("内部 Router schema required 必须是数组")
            multi_item_schema["required"] = [
                *required_fields,
                "taskId",
                "dependsOn",
            ]
            parameters = {
                "type": "object",
                "properties": {
                    "decisions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": self.max_intents,
                        "items": multi_item_schema,
                    }
                },
                "required": ["decisions"],
                "additionalProperties": False,
            }
            description = (
                "把用户请求完整拆成一个或多个已配置业务 Intent；不要漏掉子目标，"
                "不要回答用户问题"
            )
        return {
            "name": _ROUTE_TOOL_NAME,
            "description": description,
            "parameters": parameters,
        }

    def _system_prompt(
        self,
        pending_state: ClarificationState | None = None,
    ) -> str:
        catalog = {
            "product": {
                "name": self.config.product.name,
                "description": self.config.product.description,
                "allowGeneralQuestions": (self.config.product.allow_general_questions),
            },
            "intents": [
                {
                    "id": intent.id,
                    "name": intent.name,
                    "description": intent.description,
                    "examples": list(intent.examples),
                    "requiredFields": list(intent.required_fields),
                    "optionalFields": list(intent.optional_fields),
                    "requiredCapabilities": list(intent.required_capabilities),
                    "sideEffect": intent.has_side_effect,
                    "risk": intent.risk,
                }
                for intent in self.config.intents
            ],
            "denied": [
                {
                    "decision": f"{_DENIED_PREFIX}{index}",
                    "name": rule.name,
                    "description": rule.description,
                    "examples": list(rule.examples),
                }
                for index, rule in enumerate(self.config.denied)
            ],
        }
        if pending_state is not None:
            catalog["pendingClarification"] = {
                "intent": pending_state.pending_intent,
                "knownArguments": copy.deepcopy(dict(pending_state.extracted_fields)),
                "missingFields": list(pending_state.missing_fields),
            }
        multi_instruction = (
            "一个请求包含多个目标时，必须在 decisions 中完整列出且不重复。"
            "每个目标提供唯一 taskId；dependsOn 只描述用户语义中明确的先后或"
            "数据依赖，不能自行创造依赖。"
            if self.max_intents > 1
            else "每次只能选择一个 decision。"
        )
        return (
            "你是严格的业务 Intent 分类器，不是问答助手。"
            "必须调用 select_business_intent，不能直接回答。"
            "只能选择目录中已有 decision；不要创造 Intent。"
            + multi_instruction
            + (
                "存在 pendingClarification 时，先判断当前消息是否在回答其追问；"
                "若是，保持该 Intent，并把当前消息中明确给出的缺失参数与"
                "knownArguments 合并。knownArguments 是可信历史槽位，不能猜测或"
                "无依据覆盖。若用户明确切换目标，再按新请求分类。"
                if pending_state is not None
                else "arguments 只能来自用户原文，不能猜测。"
            )
            + "confidence 只表示 Intent 分类把握，不因缺少 required field 而降低；"
            "缺少参数时仍选择最匹配 Intent，并让 arguments 缺少该字段。"
            "确定超范围时选择 __out_of_scope__；无法判断时降低 confidence。\n"
            + json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
        )

    def _clarification_scope(
        self,
        *,
        session_id: str | None,
        tenant_id: str | None,
    ) -> tuple[str, str] | None:
        if self.clarification_store is None:
            return None
        resolved_session = session_id
        resolved_tenant = tenant_id
        if (
            (not isinstance(resolved_session, str) or not resolved_session.strip())
            or (not isinstance(resolved_tenant, str) or not resolved_tenant.strip())
        ) and self._durable_metadata_provider is not None:
            metadata = self._durable_metadata_provider()
            if isinstance(metadata, Mapping):
                if (
                    not isinstance(resolved_session, str)
                    or not resolved_session.strip()
                ):
                    candidate = metadata.get("sessionId")
                    resolved_session = candidate if isinstance(candidate, str) else None
                if not isinstance(resolved_tenant, str) or not resolved_tenant.strip():
                    candidate = metadata.get("tenantId")
                    resolved_tenant = candidate if isinstance(candidate, str) else None
        if (
            not isinstance(resolved_session, str)
            or not resolved_session.strip()
            or len(resolved_session.strip()) > 256
            or not isinstance(resolved_tenant, str)
            or not resolved_tenant.strip()
            or len(resolved_tenant.strip()) > 256
        ):
            return None
        return resolved_tenant.strip(), resolved_session.strip()

    def _merge_pending_classification(
        self,
        raw: dict[str, Any],
        pending: ClarificationState | None,
    ) -> dict[str, Any]:
        if pending is None or raw.get("decision") != pending.pending_intent:
            return raw
        arguments = raw.get("arguments")
        if not isinstance(arguments, dict):
            return raw
        merged = copy.deepcopy(dict(pending.extracted_fields))
        merged.update(copy.deepcopy(arguments))
        output = copy.deepcopy(raw)
        output["arguments"] = merged
        return output

    async def _sync_clarification_state(
        self,
        decision: RequestDecision,
        scope: tuple[str, str] | None,
        previous: ClarificationState | None,
    ) -> RequestDecision:
        if self.clarification_store is None or scope is None:
            return decision
        try:
            if (
                decision.status == "in_scope_need_clarification"
                and decision.intent is not None
                and decision.missing_fields
            ):
                now = float(self._clarification_clock())
                created_at = (
                    previous.created_at
                    if previous is not None
                    and previous.pending_intent == decision.intent
                    else now
                )
                await self.clarification_store.save(
                    ClarificationState(
                        tenant_id=scope[0],
                        session_id=scope[1],
                        pending_intent=decision.intent,
                        extracted_fields=decision.extracted_fields,
                        missing_fields=tuple(decision.missing_fields),
                        created_at=created_at,
                        expires_at=now + self.clarification_ttl_seconds,
                    )
                )
            elif decision.status != "in_scope_need_clarification":
                await self.clarification_store.clear(*scope)
        except Exception:
            if decision.status in {"prohibited", "permission_denied", "out_of_scope"}:
                return decision
            return self._clarification_state_unavailable(
                "无法可靠提交澄清状态，已阻止继续执行"
            )
        return decision

    def _pending_clarification_message(
        self,
        state: ClarificationState | None,
    ) -> str:
        if state is None:
            return "请输入要处理的问题。"
        intent = self.config.intent_by_id(state.pending_intent)
        return intent.ask_when_missing if intent is not None else "请补充缺失信息。"

    def _clarification_state_unavailable(self, reason: str) -> RequestDecision:
        return RequestDecision(
            status="in_scope_need_clarification",
            reason=reason,
            message="暂时无法可靠恢复本轮澄清上下文，请稍后重试。",
            routing_source="rule",
        )

    def _decision_from_classification(
        self,
        raw: dict[str, Any],
    ) -> RequestDecision:
        raw_decisions = raw.get("decisions")
        if raw_decisions is not None:
            if self.max_intents == 1:
                return self._invalid_classification("当前 Router 未开启多 Intent")
            if (
                not isinstance(raw_decisions, list)
                or not 1 <= len(raw_decisions) <= self.max_intents
                or any(not isinstance(item, dict) for item in raw_decisions)
            ):
                return self._invalid_classification("模型没有返回合法 decisions 数组")
            metadata = self._validated_task_metadata(raw_decisions)
            if isinstance(metadata, str):
                return self._invalid_classification(metadata)
            decisions: list[RequestDecision] = []
            for item, (task_id, depends_on) in zip(
                raw_decisions,
                metadata,
                strict=True,
            ):
                decision = self._decision_from_single_classification(
                    item,
                    task_id=task_id,
                    depends_on=depends_on,
                )
                if decision.task_id is None:
                    decision = replace(
                        decision,
                        task_id=task_id,
                        depends_on=depends_on,
                    )
                decisions.append(decision)
            if len(decisions) == 1:
                return decisions[0]
            return self._combine_decisions(decisions)
        return self._decision_from_single_classification(raw)

    def _validated_task_metadata(
        self,
        raw_decisions: list[dict[str, Any]],
    ) -> list[tuple[str, tuple[str, ...]]] | str:
        """Validate untrusted task IDs/dependency hints before exposing them."""

        task_ids: list[str] = []
        dependencies: list[tuple[str, ...]] = []
        for index, item in enumerate(raw_decisions, start=1):
            raw_task_id = item.get("taskId")
            if raw_task_id is None:
                task_id = f"task_{index}"
            elif not isinstance(raw_task_id, str) or not raw_task_id.strip():
                return "模型返回了无效 taskId"
            else:
                task_id = raw_task_id.strip()
            if len(task_id) > 128:
                return "模型返回的 taskId 过长"
            raw_dependencies = item.get("dependsOn", [])
            if not isinstance(raw_dependencies, list) or any(
                not isinstance(value, str) or not value.strip()
                for value in raw_dependencies
            ):
                return f"任务 {task_id} 的 dependsOn 无效"
            depends_on = tuple(value.strip() for value in raw_dependencies)
            if len(depends_on) != len(set(depends_on)):
                return f"任务 {task_id} 的 dependsOn 不能重复"
            task_ids.append(task_id)
            dependencies.append(depends_on)
        if len(task_ids) != len(set(task_ids)):
            return "复合请求中的 taskId 不能重复"
        known = set(task_ids)
        graph = dict(zip(task_ids, dependencies, strict=True))
        for task_id, depends_on in graph.items():
            if task_id in depends_on:
                return f"任务 {task_id} 不能依赖自身"
            unknown = tuple(value for value in depends_on if value not in known)
            if unknown:
                return f"任务 {task_id} 依赖了未知 taskId：" + "、".join(unknown)
        if _has_dependency_cycle(graph):
            return "复合请求的依赖提示形成循环"
        return list(zip(task_ids, dependencies, strict=True))

    def _decision_from_single_classification(
        self,
        raw: dict[str, Any],
        *,
        task_id: str | None = None,
        depends_on: tuple[str, ...] = (),
    ) -> RequestDecision:
        decision_id = raw.get("decision")
        raw_arguments = raw.get("arguments")
        confidence = raw.get("confidence")
        if not isinstance(decision_id, str):
            return self._invalid_classification("模型没有返回合法 decision")
        if not isinstance(raw_arguments, dict):
            return self._invalid_classification("模型没有返回合法 arguments")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= float(confidence) <= 1
        ):
            return self._invalid_classification("模型没有返回合法 confidence")
        confidence_value = float(confidence)
        if confidence_value < self.confidence_threshold:
            return RequestDecision(
                status="in_scope_need_clarification",
                reason=f"Intent 分类置信度过低：{confidence_value:.2f}",
                message="我还不能确定你要办理哪项业务，请补充更具体的信息。",
                routing_source="model",
                confidence=confidence_value,
                task_id=task_id,
                depends_on=depends_on,
            )

        if decision_id.startswith(_DENIED_PREFIX):
            try:
                denied_index = int(decision_id.removeprefix(_DENIED_PREFIX))
                rule = self.config.denied[denied_index]
            except (ValueError, IndexError):
                return self._invalid_classification("模型选择了不存在的禁止规则")
            return RequestDecision(
                status="prohibited",
                reason=rule.description,
                message=rule.message,
                routing_source="model",
                confidence=confidence_value,
                task_id=task_id,
                depends_on=depends_on,
            )
        if decision_id == _OUT_OF_SCOPE:
            return RequestDecision(
                status="out_of_scope",
                reason="模型未匹配任何已配置业务 Intent",
                message=f"该请求超出 {self.config.product.name} 的业务范围。",
                routing_source="model",
                confidence=confidence_value,
                task_id=task_id,
                depends_on=depends_on,
            )
        if decision_id == _GENERAL_QA:
            if not self.config.product.allow_general_questions:
                return self._invalid_classification("当前产品不允许普通问答")
            return RequestDecision(
                status="in_scope_no_tool",
                domain="general",
                intent="general.qa",
                reason="模型识别为产品允许的普通问答",
                message="交给回答模型处理。",
                tool_policy=ToolChoicePolicy("none"),
                routing_source="model",
                confidence=confidence_value,
                task_id=task_id,
                depends_on=depends_on,
            )

        intent = self.config.intent_by_id(decision_id)
        if intent is None:
            return self._invalid_classification("模型选择了不存在的 Intent")
        arguments = self._validated_arguments(intent, raw_arguments)
        missing = tuple(
            field for field in intent.required_fields if field not in arguments
        )
        if missing:
            if intent.must_use_tool:
                planned = self._tool_decision(
                    intent,
                    arguments,
                    confidence_value,
                    task_id=task_id,
                    depends_on=depends_on,
                )
                return replace(
                    planned,
                    status="in_scope_need_clarification",
                    reason=f"Intent {intent.id} 缺少必要参数",
                    message=intent.ask_when_missing,
                    missing_fields=missing,
                    tool_policy=ToolChoicePolicy("none"),
                )
            return RequestDecision(
                status="in_scope_need_clarification",
                domain=_domain_from_intent(intent.id),
                intent=intent.id,
                reason=f"Intent {intent.id} 缺少必要参数",
                message=intent.ask_when_missing,
                extracted_fields=arguments,
                missing_fields=missing,
                side_effect=intent.has_side_effect,
                risk=intent.risk,
                routing_source="model",
                confidence=confidence_value,
                task_id=task_id,
                depends_on=depends_on,
            )
        if not intent.must_use_tool:
            return RequestDecision(
                status="in_scope_no_tool",
                domain=_domain_from_intent(intent.id),
                intent=intent.id,
                reason=f"Intent {intent.id} 不需要外部业务能力",
                message="交给回答模型处理。",
                extracted_fields=arguments,
                side_effect=intent.has_side_effect,
                risk=intent.risk,
                tool_policy=ToolChoicePolicy("none"),
                routing_source="model",
                confidence=confidence_value,
                task_id=task_id,
                depends_on=depends_on,
            )
        return self._tool_decision(
            intent,
            arguments,
            confidence_value,
            task_id=task_id,
            depends_on=depends_on,
        )

    def _combine_decisions(
        self,
        decisions: list[RequestDecision],
    ) -> RequestDecision:
        """Produce an executable read decision or a structured Planner hand-off."""

        components = tuple(decisions)
        task = self._build_task_decision(components)
        for status in ("prohibited", "permission_denied", "out_of_scope"):
            blocked = next((item for item in decisions if item.status == status), None)
            if blocked is not None:
                return replace(
                    blocked,
                    component_decisions=components,
                    task_decision=task,
                )
        uncertain = next(
            (
                item
                for item in decisions
                if item.status == "in_scope_need_clarification" and item.intent is None
            ),
            None,
        )
        if uncertain is not None:
            return replace(
                uncertain,
                component_decisions=components,
                task_decision=task,
            )
        unavailable = next(
            (
                item
                for item in decisions
                if item.status == "in_scope_capability_missing"
            ),
            None,
        )
        if unavailable is not None:
            return replace(
                unavailable,
                component_decisions=components,
                task_decision=task,
            )
        confidences = [
            item.confidence for item in decisions if item.confidence is not None
        ]
        confidence = min(confidences) if confidences else None
        actionable = [
            item
            for item in decisions
            if item.status
            in {
                "in_scope_tool_ready",
                "in_scope_approval_required",
                "in_scope_need_clarification",
            }
        ]
        capabilities = tuple(
            dict.fromkeys(
                capability
                for item in actionable
                for capability in item.required_capabilities
            )
        )
        tools = tuple(
            dict.fromkeys(
                tool_name for item in actionable for tool_name in item.selected_tools
            )
        )
        tool_decisions = [item for item in actionable if item.selected_tools]
        if not tool_decisions:
            if all(item.status == "in_scope_no_tool" for item in decisions):
                return RequestDecision(
                    status="in_scope_no_tool",
                    domain="multi",
                    intent="+".join(
                        item.intent for item in decisions if item.intent is not None
                    ),
                    reason="复合请求中的各 Intent 都不需要外部工具",
                    message="交给回答模型统一处理。",
                    routing_source="model",
                    confidence=confidence,
                    component_decisions=components,
                    task_decision=task,
                    risk=task.highest_risk,
                    tool_policy=ToolChoicePolicy("none"),
                )
            if any(
                item.status == "in_scope_need_clarification" and item.intent is not None
                for item in decisions
            ):
                return RequestDecision(
                    status="in_scope_plan_required",
                    domain="multi",
                    intent="multi_intent",
                    reason="复合任务已识别，但 Planner 需要先补齐组件参数",
                    message="已识别复合任务；请先补充各任务缺少的参数。",
                    requires_approval=bool(task.approval_required_tasks),
                    side_effect=bool(task.side_effect_tasks),
                    risk=task.highest_risk,
                    routing_source="model",
                    confidence=confidence,
                    component_decisions=components,
                    task_decision=task,
                    tool_policy=ToolChoicePolicy("none"),
                )
            return self._invalid_classification("复合请求包含不能安全合并的 Intent")
        requires_planner = any(
            item.requires_approval
            or item.status
            in {"in_scope_approval_required", "in_scope_need_clarification"}
            or item.extracted_fields
            or item.side_effect
            or item.depends_on
            for item in decisions
        )
        if requires_planner:
            return RequestDecision(
                status="in_scope_plan_required",
                domain="multi",
                intent="multi_intent",
                reason=(
                    "复合请求已拆成结构化任务，需由 Planner 校验参数、依赖、"
                    "权限和逐项审批"
                ),
                message=("已识别复合任务；请交给 Planner 生成并校验执行计划。"),
                required_capabilities=capabilities,
                selected_tools=tools,
                requires_approval=bool(task.approval_required_tasks),
                side_effect=bool(task.side_effect_tasks),
                risk=task.highest_risk,
                routing_source="model",
                confidence=confidence,
                component_decisions=components,
                task_decision=task,
                tool_policy=ToolChoicePolicy("none"),
            )
        return RequestDecision(
            status="in_scope_tool_ready",
            domain="multi",
            intent="+".join(
                item.intent for item in decisions if item.intent is not None
            ),
            reason="复合只读请求需要全部实时能力：" + "、".join(capabilities),
            message="已为复合请求找到全部实时查询工具。",
            required_capabilities=capabilities,
            selected_tools=tools,
            risk=task.highest_risk,
            routing_source="model",
            confidence=confidence,
            component_decisions=components,
            task_decision=task,
            tool_policy=ToolChoicePolicy("required"),
        )

    def _build_task_decision(
        self,
        decisions: tuple[RequestDecision, ...],
    ) -> TaskDecision:
        return TaskDecision(
            components=decisions,
            dependencies=tuple(
                TaskDependencyHint(
                    task_id=cast(str, item.task_id),
                    depends_on=item.depends_on,
                )
                for item in decisions
                if item.depends_on
            ),
            approval_required_tasks=tuple(
                cast(str, item.task_id)
                for item in decisions
                if item.requires_approval or item.status == "in_scope_approval_required"
            ),
            side_effect_tasks=tuple(
                cast(str, item.task_id) for item in decisions if item.side_effect
            ),
            clarification_tasks=tuple(
                cast(str, item.task_id)
                for item in decisions
                if item.status == "in_scope_need_clarification"
            ),
            missing_capability_tasks=tuple(
                cast(str, item.task_id)
                for item in decisions
                if item.status == "in_scope_capability_missing"
            ),
            permission_denied_tasks=tuple(
                cast(str, item.task_id)
                for item in decisions
                if item.status == "permission_denied"
            ),
            highest_risk=highest_risk_level([item.risk for item in decisions]),
        )

    def _tool_decision(
        self,
        intent: SimpleIntent,
        arguments: dict[str, JsonValue],
        confidence: float,
        *,
        task_id: str | None = None,
        depends_on: tuple[str, ...] = (),
    ) -> RequestDecision:
        required_capabilities = intent.required_capabilities
        match = self.capabilities.match(required_capabilities)
        side_effect = intent.has_side_effect or match.has_side_effect
        risk = highest_risk_level([intent.risk, match.risk])
        requires_approval = (
            intent.requires_approval
            or match.requires_approval
            or side_effect
            or risk in {"high", "critical"}
        )
        if not match.available:
            return RequestDecision(
                status="in_scope_capability_missing",
                domain=_domain_from_intent(intent.id),
                intent=intent.id,
                reason=f"Intent {intent.id} 所需业务能力当前不可用",
                message=(
                    "请求属于产品范围，但当前缺少能力："
                    + "、".join(match.missing_capabilities)
                ),
                extracted_fields=arguments,
                required_capabilities=required_capabilities,
                missing_capabilities=match.missing_capabilities,
                requires_approval=requires_approval,
                side_effect=side_effect,
                risk=risk,
                routing_source="model",
                confidence=confidence,
                task_id=task_id,
                depends_on=depends_on,
            )
        selected = tuple(tool.name for tool in match.tools)
        if requires_approval:
            return RequestDecision(
                status="in_scope_approval_required",
                domain=_domain_from_intent(intent.id),
                intent=intent.id,
                reason=f"Intent {intent.id} 是需要审批的真实操作",
                message=f"操作“{intent.name}”需要用户确认或业务审批后才能执行。",
                extracted_fields=arguments,
                required_capabilities=required_capabilities,
                selected_tools=selected,
                requires_approval=True,
                side_effect=side_effect,
                risk=risk,
                routing_source="model",
                confidence=confidence,
                task_id=task_id,
                depends_on=depends_on,
                tool_policy=ToolChoicePolicy("required"),
            )
        return RequestDecision(
            status="in_scope_tool_ready",
            domain=_domain_from_intent(intent.id),
            intent=intent.id,
            reason=(
                f"Intent {intent.id} 必须使用业务能力 "
                + "、".join(required_capabilities)
            ),
            message="已找到完成请求所需的业务工具。",
            extracted_fields=arguments,
            required_capabilities=required_capabilities,
            selected_tools=selected,
            side_effect=side_effect,
            risk=risk,
            routing_source="model",
            confidence=confidence,
            task_id=task_id,
            depends_on=depends_on,
            tool_policy=ToolChoicePolicy("required"),
        )

    def _validated_arguments(
        self,
        intent: SimpleIntent,
        raw: dict[str, Any],
    ) -> dict[str, JsonValue]:
        values: dict[str, JsonValue] = {}
        fields = tuple(
            dict.fromkeys((*intent.required_fields, *intent.optional_fields))
        )
        for field in fields:
            if field not in raw:
                continue
            try:
                values[field] = _validated_json_argument(raw[field])
            except (TypeError, ValueError):
                # An invalid value is equivalent to a missing required field;
                # Router must ask instead of coercing or inventing data.
                continue
        return values

    def _invalid_classification(self, reason: str) -> RequestDecision:
        return RequestDecision(
            status="in_scope_need_clarification",
            reason=reason,
            message="无法可靠识别该业务请求，请补充更具体的信息。",
            routing_source="model",
        )

    def _match_denied_example(self, user_text: str):
        normalized = _normalize(user_text)
        for rule in self.config.denied:
            if any(_normalize(example) in normalized for example in rule.examples):
                return rule
        return None


def _normalize(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _has_dependency_cycle(graph: dict[str, tuple[str, ...]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> bool:
        if task_id in visiting:
            return True
        if task_id in visited:
            return False
        visiting.add(task_id)
        if any(visit(dependency) for dependency in graph[task_id]):
            return True
        visiting.remove(task_id)
        visited.add(task_id)
        return False

    return any(visit(task_id) for task_id in graph)


def _json_value_schema() -> dict[str, JsonValue]:
    """Provider-friendly schema for arbitrary JSON business arguments."""

    return {
        "anyOf": [
            {"type": "string"},
            {"type": "number"},
            {"type": "boolean"},
            {"type": "null"},
            {"type": "array", "items": {}},
            {"type": "object", "additionalProperties": {}},
        ]
    }


def _validated_json_argument(value: Any, *, depth: int = 0) -> JsonValue:
    """Copy one strict, bounded JSON value without string-only coercion."""

    if depth > 8:
        raise ValueError("业务参数嵌套过深")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or len(stripped) > 2_000:
            raise ValueError("业务字符串参数为空或过长")
        return stripped
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("业务数字参数必须有限")
        return value
    if isinstance(value, list):
        if len(value) > 100:
            raise ValueError("业务数组参数过长")
        return [_validated_json_argument(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 100 or any(
            not isinstance(key, str) or not key or len(key) > 200 for key in value
        ):
            raise ValueError("业务对象参数字段无效")
        return {
            key: _validated_json_argument(item, depth=depth + 1)
            for key, item in value.items()
        }
    raise TypeError("业务参数必须是严格 JSON 值")


def _domain_from_intent(intent_id: str) -> str:
    return intent_id.split(".", 1)[0] if "." in intent_id else "business"


def _message_evaluation_metrics(message: Any) -> dict[str, int | float]:
    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
        return {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}
    usage = message["usage"]
    input_tokens = usage.get("input", usage.get("inputTokens", 0))
    output_tokens = usage.get("output", usage.get("outputTokens", 0))
    cost_value = usage.get("cost", 0.0)
    if isinstance(cost_value, dict):
        cost_value = cost_value.get("total", 0.0)
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 0
    ):
        input_tokens = 0
    if (
        isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or output_tokens < 0
    ):
        output_tokens = 0
    if isinstance(cost_value, bool) or not isinstance(cost_value, (int, float)):
        cost_value = 0.0
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost": float(cost_value),
    }
