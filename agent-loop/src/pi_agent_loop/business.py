"""Explicit, TOML-first assembly of an external business package."""

from __future__ import annotations

import inspect
import json
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .planning import HybridRequestPlanner, IntentPlanPolicy, PlanParameterContract
from .routing.capabilities import CapabilityRegistry
from .routing.hybrid_router import HybridModelRouter
from .routing.simple_config import SimpleBusinessConfig, parse_simple_business_config
from .retry.model import settle_stream_producer
from .types import AgentTool, Model, StreamFn


@dataclass(frozen=True, slots=True)
class BusinessBundle:
    """A validated catalogue. Factories own clients and credentials, never TOML."""

    tools: tuple[AgentTool, ...]
    capabilities: CapabilityRegistry
    router_config: SimpleBusinessConfig
    plan_policies: Mapping[str, IntentPlanPolicy]
    plan_tool_bindings: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(
            self, "plan_policies", MappingProxyType(dict(self.plan_policies))
        )
        object.__setattr__(
            self, "plan_tool_bindings", MappingProxyType(dict(self.plan_tool_bindings))
        )
        self.validate()

    def validate(self) -> None:
        """Revalidate before Host startup, including caller-assembled bundles."""
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("duplicate tool names in BusinessBundle")
        registered = self.capabilities.all_tools()
        if {tool.name for tool in registered} != set(names) or any(
            tool is not next(item for item in self.tools if item.name == tool.name)
            for tool in registered
        ):
            raise ValueError("BusinessBundle tools and capability instances differ")
        intents = {intent.id: intent for intent in self.router_config.intents}
        if len(intents) != len(self.router_config.intents):
            raise ValueError("duplicate bundle intent")
        if set(self.plan_policies) != set(self.plan_tool_bindings):
            raise ValueError("plan policies and bindings must have the same intents")
        for intent in self.router_config.intents:
            if not self.capabilities.match(intent.required_capabilities).available:
                raise ValueError("bundle intent requires unavailable capabilities")
        for name, policy in self.plan_policies.items():
            if name not in intents or policy.intent != name:
                raise ValueError("unknown/mismatched plan intent")
            entry = self.capabilities.entries_by_names(
                (self.plan_tool_bindings[name],)
            )[0]
            if not set(policy.capabilities).issubset(entry.capabilities):
                raise ValueError("plan binding capability conflict")
            if (
                policy.replay_policy != entry.tool.replay_policy
                or policy.write != entry.has_side_effect
            ):
                raise ValueError("plan binding replay/side-effect contract conflict")
            if (
                entry.approval_required
                or intents[name].requires_approval
                or intents[name].risk in {"high", "critical"}
            ) and not policy.requires_approval:
                raise ValueError("plan binding cannot lower approval policy")
            if policy.parameter_contract != _contract_for(entry.tool):
                raise ValueError("plan binding parameter contract conflict")

    def create_router(self, *, model: Model, stream_fn: StreamFn) -> HybridModelRouter:
        return HybridModelRouter(
            self.router_config,
            self.capabilities,
            model=model,
            stream_fn=stream_fn,
            max_intents=32 if self.plan_policies else 1,
        )

    def create_planner(self, *, model: Model, stream_fn: StreamFn) -> BundlePlanner:
        return BundlePlanner(self.plan_policies, model=model, stream_fn=stream_fn)


class BundlePlanner(HybridRequestPlanner):
    """Small structured planner whose calls can be rebound to a Host Runtime."""

    def __init__(
        self,
        policies: Mapping[str, IntentPlanPolicy],
        *,
        model: Model,
        stream_fn: StreamFn,
    ) -> None:
        self.model = model
        self.stream_fn = stream_fn
        super().__init__(policies, self._generate)

    def bind_runtime(self, *, stream_fn: StreamFn) -> BundlePlanner:
        return BundlePlanner(self.policies, model=self.model, stream_fn=stream_fn)

    async def _generate(self, request: str, catalog: tuple[dict[str, Any], ...]) -> Any:
        context = {
            "systemPrompt": 'Return only JSON {"steps":[{"stepId":"s1","intent":"configured intent","arguments":{},"dependsOn":[]}]}. Use only the trusted catalogue. Never invent missing required arguments. Catalogue: '
            + json.dumps(catalog, ensure_ascii=False),
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": request}]}
            ],
            "tools": [],
        }
        value = self.stream_fn(self.model, context, {"model_request_source": "planner"})
        stream = await value if inspect.isawaitable(value) else value
        completed = False
        try:
            async for _ in stream:
                pass
            final = await stream.result()
            if final.get("stopReason") in {"error", "aborted"}:
                raise ValueError("Business planner model request failed")
            text = "".join(
                item["text"]
                for item in final.get("content", [])
                if item.get("type") == "text"
            )
            completed = True
            return json.loads(text)
        finally:
            await settle_stream_producer(stream, cancel=not completed)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


def _texts(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    result = tuple(_text(item, label) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"duplicate {label}")
    return result


def _contract_for(tool: AgentTool) -> PlanParameterContract:
    schema = tool.parameters
    if schema.get("type") != "object" or not isinstance(
        schema.get("properties", {}), dict
    ):
        raise ValueError(f"Plan tool {tool.name} requires an object parameter schema")
    properties = schema.get("properties", {})
    required = set(_texts(schema.get("required", []), "schema.required"))
    if not required.issubset(properties):
        raise ValueError(f"invalid required fields for {tool.name}")
    fields: dict[str, Any] = {}
    for name, spec in properties.items():
        if not isinstance(spec, dict) or "type" not in spec:
            raise ValueError(
                f"Plan field {tool.name}.{name} needs an explicit JSON type"
            )
        fields[name] = spec["type"]
    return PlanParameterContract(
        required={key: value for key, value in fields.items() if key in required},
        optional={key: value for key, value in fields.items() if key not in required},
        allow_empty=not required,
    )


def load_business_bundle(
    config_path: str | Path, *, tool_factories: Mapping[str, Callable[[], AgentTool]]
) -> BusinessBundle:
    """Load tool registrations, existing routing schema and optional plan policies.

    ``[[tools]]`` uses CapabilityRegistry field names plus ``name``/``factory``.
    ``[[plans]]`` uses IntentPlanPolicy's public serialized fields plus ``tool``;
    omitted policy fields are derived conservatively from the bound tool/intent.
    """
    with Path(config_path).open("rb") as file:
        root = tomllib.load(file)
    if set(root) - {"product", "intents", "denied", "tools", "plans"}:
        raise ValueError("Business bundle contains unknown top-level fields")
    config = parse_simple_business_config(
        {
            key: value
            for key, value in root.items()
            if key in {"product", "intents", "denied"}
        }
    )
    raw_tools = root.get("tools", [])
    raw_plans = root.get("plans", [])
    if not isinstance(raw_tools, list) or not isinstance(raw_plans, list):
        raise ValueError("tools and plans must be arrays of tables")
    registry = CapabilityRegistry()
    for row in raw_tools:
        if not isinstance(row, dict) or set(row) - {
            "name",
            "factory",
            "capabilities",
            "domain",
            "operation",
            "risk",
            "requires_approval",
            "priority",
            "side_effect",
        }:
            raise ValueError("invalid tool registration fields")
        name = _text(row.get("name"), "tool.name")
        if name in {tool.name for tool in registry.all_tools()}:
            raise ValueError(f"duplicate tool {name}")
        factory_name = _text(row.get("factory", name), "tool.factory")
        factory = tool_factories.get(factory_name)
        if not callable(factory):
            raise ValueError(f"missing tool factory {factory_name}")
        tool = factory()
        if not isinstance(tool, AgentTool) or tool.name != name:
            raise ValueError(
                f"factory {factory_name} must return AgentTool named {name}"
            )
        for field in ("requires_approval", "side_effect"):
            if field in row and type(row[field]) is not bool:
                raise ValueError(f"{field} must be boolean")
        if type(row.get("priority", 0)) is not int:
            raise ValueError("priority must be an integer")
        registry.register(
            tool,
            capabilities=set(_texts(row.get("capabilities"), "capabilities")),
            domain=_text(row.get("domain"), "domain"),
            operation=_text(row.get("operation", "read"), "operation"),
            risk=row.get("risk", "low"),
            requires_approval=row.get("requires_approval", False),
            priority=row.get("priority", 0),
            side_effect=row.get("side_effect"),
        )
    intents = {intent.id: intent for intent in config.intents}
    for intent in config.intents:
        if not registry.match(intent.required_capabilities).available:
            raise ValueError(f"Intent {intent.id} references missing capabilities")
    policies: dict[str, IntentPlanPolicy] = {}
    bindings: dict[str, str] = {}
    for row in raw_plans:
        if not isinstance(row, dict):
            raise ValueError("plan must be a table")
        name = _text(row.get("intent"), "plan.intent")
        if name in policies or name not in intents:
            raise ValueError(f"duplicate or unknown plan intent {name}")
        tool_name = _text(row.get("tool"), "plan.tool")
        try:
            entry = registry.entries_by_names((tool_name,))[0]
        except KeyError:
            raise ValueError(f"missing plan tool {tool_name}") from None
        intent = intents[name]
        if not set(intent.required_capabilities).issubset(entry.capabilities):
            raise ValueError(f"plan capability mismatch for {name}")
        contract = _contract_for(entry.tool)
        if set(intent.required_fields) != set(dict(contract.required)) or not set(
            intent.optional_fields
        ).issubset(dict(contract.optional)):
            raise ValueError(f"intent/tool parameter contract conflict for {name}")
        write = entry.has_side_effect or intent.has_side_effect
        if write != entry.has_side_effect:
            raise ValueError(f"intent/tool side-effect conflict for {name}")
        defaults = IntentPlanPolicy(name).to_dict()
        defaults.update(
            {
                "requiresApproval": entry.approval_required
                or intent.requires_approval
                or intent.risk in {"high", "critical"}
                or write,
                "write": write,
                "replayPolicy": "never"
                if write or entry.tool.replay_policy == "never"
                else "safe",
                "capabilities": list(intent.required_capabilities),
                "parameterContract": contract.to_dict(),
            }
        )
        values = {
            **defaults,
            **{key: value for key, value in row.items() if key != "tool"},
        }
        for field in ("requiresApproval", "write"):
            if type(values[field]) is not bool:
                raise ValueError(f"{field} must be boolean")
            values[field] = values[field] or defaults[field]
        if defaults["replayPolicy"] == "never":
            if values["replayPolicy"] not in {"safe", "never"}:
                raise ValueError("invalid replayPolicy")
            values["replayPolicy"] = "never"
        policy = IntentPlanPolicy.from_dict(values)
        if (
            policy.parameter_contract != contract
            or not set(policy.capabilities).issubset(entry.capabilities)
            or not set(intent.required_capabilities).issubset(policy.capabilities)
        ):
            raise ValueError(
                f"plan/tool parameter or capability contract conflict for {name}"
            )
        if policy.write != entry.has_side_effect:
            raise ValueError(f"plan/tool side-effect conflict for {name}")
        policies[name], bindings[name] = policy, tool_name
    for policy in policies.values():
        if not set(policy.required_predecessor_intents).issubset(policies):
            raise ValueError(f"unknown predecessor for {policy.intent}")
        if any(
            binding.source.intent not in policies
            for binding in policy.argument_bindings
        ):
            raise ValueError(f"unknown argument binding source for {policy.intent}")
    dependencies = {}
    for name, policy in policies.items():
        fields = (
            set(dict(policy.parameter_contract.required))
            | set(dict(policy.parameter_contract.optional))
            if policy.parameter_contract is not None
            else set()
        )
        if any(binding.target not in fields for binding in policy.argument_bindings):
            raise ValueError(f"unknown argument binding target for {name}")
        dependencies[name] = set(policy.required_predecessor_intents) | {
            binding.source.intent for binding in policy.argument_bindings
        }
    visited: set[str] = set()
    active: set[str] = set()

    def visit(name: str) -> None:
        if name in active:
            raise ValueError("cyclic plan intent dependencies")
        if name not in visited:
            active.add(name)
            for predecessor in dependencies[name]:
                visit(predecessor)
            active.remove(name)
            visited.add(name)

    for name in dependencies:
        visit(name)
    return BusinessBundle(
        tuple(registry.all_tools()),
        registry,
        config,
        MappingProxyType(policies),
        MappingProxyType(bindings),
    )
