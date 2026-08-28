"""使用大模型理解自然语言、使用 Host 配置约束业务边界的混合 Router。"""

from __future__ import annotations

import copy
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from ..cancellation import CancellationToken
from ..types import Model, StreamFn
from .capabilities import CapabilityRegistry
from .simple_config import SimpleBusinessConfig, SimpleIntent
from .types import RequestDecision, ToolChoicePolicy

_ROUTE_TOOL_NAME = "select_business_intent"
_OUT_OF_SCOPE = "__out_of_scope__"
_GENERAL_QA = "__general_qa__"
_DENIED_PREFIX = "__denied__:"


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
        retry_event_sink: Any | None = None,
    ) -> None:
        if not 0 <= confidence_threshold <= 1:
            raise ValueError("confidence_threshold 必须在 0 到 1 之间")
        self.config = config
        self.capabilities = capabilities
        self.model = model
        self.stream_fn = stream_fn
        self.confidence_threshold = confidence_threshold
        self.retry_event_sink = retry_event_sink
        self._durable_metadata_provider: (
            Callable[[], Mapping[str, Any]] | None
        ) = None
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

    async def route(self, user_text: str) -> RequestDecision:
        # Rule-only routes and failed calls must not inherit metrics from the
        # preceding evaluation case.
        self._evaluation_metrics = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cost": 0.0,
        }
        text = user_text.strip()
        if not text:
            return RequestDecision(
                status="in_scope_need_clarification",
                reason="用户请求为空",
                message="请输入要处理的问题。",
                missing_fields=("user_request",),
                routing_source="rule",
            )

        # 明确写在 denied.examples 中的高风险表达先确定性阻止，不消耗模型。
        denied = self._match_denied_example(text)
        if denied is not None:
            return RequestDecision(
                status="prohibited",
                reason=denied.description,
                message=denied.message,
                routing_source="rule",
                confidence=1.0,
            )

        try:
            classification = await self._classify(text)
        except Exception:
            # 分类器失败必须 fail-closed，不能退回“让回答模型随便决定”。
            return RequestDecision(
                status="in_scope_need_clarification",
                reason="HybridModelRouter 分类失败",
                message="暂时无法可靠判断该请求所属业务，请换一种说法或稍后重试。",
                routing_source="model",
            )
        return self._decision_from_classification(classification)

    async def _classify(self, user_text: str) -> dict[str, Any]:
        tool = self._routing_tool()
        context = {
            "systemPrompt": self._system_prompt(),
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
            "cancellation_token": CancellationToken(),
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
            await cast(Awaitable[Any], value)
            if inspect.isawaitable(value)
            else value
        )
        if not hasattr(stream, "__aiter__") or not hasattr(stream, "result"):
            raise TypeError("HybridModelRouter 的 stream_fn 返回值不符合事件流契约")
        async for _event in stream:
            pass
        message = await stream.result()
        self._evaluation_metrics = _message_evaluation_metrics(message)
        if message.get("stopReason") in {"error", "aborted"}:
            raise RuntimeError("业务 Intent 分类模型请求失败")
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
            f"{_DENIED_PREFIX}{index}"
            for index, _ in enumerate(self.config.denied)
        ]
        decisions.extend(denied_values)
        decisions.append(_OUT_OF_SCOPE)
        if self.config.product.allow_general_questions:
            decisions.append(_GENERAL_QA)
        return {
            "name": _ROUTE_TOOL_NAME,
            "description": (
                "只用于把用户请求分类到已配置业务 Intent；不要回答用户问题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "decision": {
                        "type": "string",
                        "enum": decisions,
                        "description": "只能选择枚举中的一个业务决定",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "从用户原文提取的业务参数，值使用字符串",
                        "additionalProperties": {"type": "string"},
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
            },
        }

    def _system_prompt(self) -> str:
        catalog = {
            "product": {
                "name": self.config.product.name,
                "description": self.config.product.description,
                "allowGeneralQuestions": (
                    self.config.product.allow_general_questions
                ),
            },
            "intents": [
                {
                    "id": intent.id,
                    "name": intent.name,
                    "description": intent.description,
                    "examples": list(intent.examples),
                    "requiredFields": list(intent.required_fields),
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
        return (
            "你是严格的业务 Intent 分类器，不是问答助手。"
            "必须调用 select_business_intent，不能直接回答。"
            "只能选择目录中已有 decision；不要创造 Intent。"
            "arguments 只能来自用户原文，不能猜测。"
            "confidence 只表示 Intent 分类把握，不因缺少 required field 而降低；"
            "缺少参数时仍选择最匹配 Intent，并让 arguments 缺少该字段。"
            "确定超范围时选择 __out_of_scope__；无法判断时降低 confidence。\n"
            + json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
        )

    def _decision_from_classification(
        self,
        raw: dict[str, Any],
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
            )
        if decision_id == _OUT_OF_SCOPE:
            return RequestDecision(
                status="out_of_scope",
                reason="模型未匹配任何已配置业务 Intent",
                message=f"该请求超出 {self.config.product.name} 的业务范围。",
                routing_source="model",
                confidence=confidence_value,
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
            )

        intent = self.config.intent_by_id(decision_id)
        if intent is None:
            return self._invalid_classification("模型选择了不存在的 Intent")
        arguments = self._validated_arguments(intent, raw_arguments)
        missing = tuple(
            field for field in intent.required_fields if field not in arguments
        )
        if missing:
            return RequestDecision(
                status="in_scope_need_clarification",
                domain=_domain_from_intent(intent.id),
                intent=intent.id,
                reason=f"Intent {intent.id} 缺少必要参数",
                message=intent.ask_when_missing,
                extracted_fields=arguments,
                missing_fields=missing,
                routing_source="model",
                confidence=confidence_value,
            )
        if not intent.must_use_tool:
            return RequestDecision(
                status="in_scope_no_tool",
                domain=_domain_from_intent(intent.id),
                intent=intent.id,
                reason=f"Intent {intent.id} 不需要外部业务能力",
                message="交给回答模型处理。",
                extracted_fields=arguments,
                tool_policy=ToolChoicePolicy("none"),
                routing_source="model",
                confidence=confidence_value,
            )
        return self._tool_decision(intent, arguments, confidence_value)

    def _tool_decision(
        self,
        intent: SimpleIntent,
        arguments: dict[str, str],
        confidence: float,
    ) -> RequestDecision:
        capability = cast(str, intent.capability)
        match = self.capabilities.match((capability,))
        if not match.available:
            return RequestDecision(
                status="in_scope_capability_missing",
                domain=_domain_from_intent(intent.id),
                intent=intent.id,
                reason=f"Intent {intent.id} 所需业务能力当前不可用",
                message=f"请求属于产品范围，但当前缺少能力：{capability}",
                extracted_fields=arguments,
                required_capabilities=(capability,),
                missing_capabilities=match.missing_capabilities,
                requires_approval=intent.requires_approval,
                routing_source="model",
                confidence=confidence,
            )
        selected = tuple(tool.name for tool in match.tools)
        if intent.requires_approval:
            return RequestDecision(
                status="in_scope_approval_required",
                domain=_domain_from_intent(intent.id),
                intent=intent.id,
                reason=f"Intent {intent.id} 是需要审批的真实操作",
                message=f"操作“{intent.name}”需要用户确认或业务审批后才能执行。",
                extracted_fields=arguments,
                required_capabilities=(capability,),
                selected_tools=selected,
                requires_approval=True,
                routing_source="model",
                confidence=confidence,
                tool_policy=ToolChoicePolicy("required"),
            )
        return RequestDecision(
            status="in_scope_tool_ready",
            domain=_domain_from_intent(intent.id),
            intent=intent.id,
            reason=f"Intent {intent.id} 必须使用业务能力 {capability}",
            message="已找到完成请求所需的业务工具。",
            extracted_fields=arguments,
            required_capabilities=(capability,),
            selected_tools=selected,
            routing_source="model",
            confidence=confidence,
            tool_policy=ToolChoicePolicy("required"),
        )

    def _validated_arguments(
        self,
        intent: SimpleIntent,
        raw: dict[str, Any],
    ) -> dict[str, str]:
        values: dict[str, str] = {}
        for field in intent.required_fields:
            value = raw.get(field)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                text = str(value).strip()
                if text and len(text) <= 500:
                    values[field] = text
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
    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
        input_tokens = 0
    if isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or output_tokens < 0:
        output_tokens = 0
    if isinstance(cost_value, bool) or not isinstance(cost_value, (int, float)):
        cost_value = 0.0
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost": float(cost_value),
    }
