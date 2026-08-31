"""审批最终边界、动作条件绑定和 Session 配置身份回归测试。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    Agent,
    AgentTool,
    AgentToolResult,
    ApprovalError,
    ApprovalResumeCoordinator,
    ApprovalService,
    CapabilityRegistry,
    DurableActionEnvelope,
    DurableAgentHost,
    DurableAgentWorkspace,
    IdentityClaim,
    InMemoryOperationEventStore,
    IntentPlanPolicy,
    Model,
    RequestDecision,
    RoutedAgent,
    ScriptedProvider,
    SessionConfigurationMismatchError,
    SimpleBusinessConfig,
    SimpleIntent,
    SimpleProduct,
    StaticIdentityVerifier,
    ToolChoicePolicy,
    WriteExecutionContext,
    WriteOperationService,
    agent_configuration_hash,
    assistant_message,
)
from pi_agent_loop.retry.errors import DefinitelyNotCommittedToolError  # noqa: E402


async def _execute(_call_id, _arguments, _cancellation, _update):
    return AgentToolResult(content=[{"type": "text", "text": "ok"}], details={})


async def _execute_with_context(
    _call_id,
    _arguments,
    _context,
    _cancellation,
    _update,
):
    return AgentToolResult(content=[{"type": "text", "text": "ok"}], details={})


def _tool(
    name: str = "change_resource",
    *,
    requires_approval: bool = False,
    implementation_version: str = "1",
    security_policy_version: str = "1",
    contextual: bool = False,
) -> AgentTool:
    return AgentTool(
        name=name,
        label=name,
        description="变更测试资源",
        parameters={"type": "object", "additionalProperties": False},
        execute=None if contextual else _execute,
        replay_policy="never",
        requires_approval=requires_approval,
        implementation_version=implementation_version,
        security_policy_version=security_policy_version,
        execute_with_context=_execute_with_context if contextual else None,
    )


class FixedRouter:
    def __init__(self, decision: RequestDecision) -> None:
        self.decision = decision

    def route(self, _text: str) -> RequestDecision:
        return self.decision


class ConfiguredRouter(FixedRouter):
    def __init__(self, decision: RequestDecision) -> None:
        super().__init__(decision)
        self.config = SimpleBusinessConfig(
            product=SimpleProduct("资源助手", "测试 Intent 审批边界", False),
            intents=(
                SimpleIntent(
                    id="resource.change",
                    name="变更资源",
                    description="变更资源",
                    examples=("变更资源",),
                    required_fields=(),
                    capability="resources.change",
                    must_use_tool=True,
                    requires_approval=True,
                    ask_when_missing="无需参数",
                ),
            ),
        )


def _decision(*, requires_approval: bool = False) -> RequestDecision:
    return RequestDecision(
        status="in_scope_tool_ready",
        reason="自定义 Router 认为可以直接执行",
        message="ready",
        intent="resource.change",
        required_capabilities=("resources.change",),
        selected_tools=("change_resource",),
        requires_approval=requires_approval,
        tool_policy=ToolChoicePolicy("required"),
    )


class DelayedConsumeStore(InMemoryOperationEventStore):
    async def append_batch(
        self,
        session_id,
        operation_id,
        events,
        *,
        expected_last_sequence=None,
        deadline_ms=None,
    ):
        if any(event_type == "approval_consumed" for event_type, _ in events):
            await asyncio.sleep(0.25)
        return await super().append_batch(
            session_id,
            operation_id,
            events,
            expected_last_sequence=expected_last_sequence,
            deadline_ms=deadline_ms,
        )


class PolicyBoundaryRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="policy-model", provider="fake", api="fake")

    async def _identities(self):
        verifier = StaticIdentityVerifier(
            {
                "operator": ("operator-secret", {"operator"}),
                "manager": ("manager-secret", {"approver"}),
            }
        )
        return (
            await verifier.verify(IdentityClaim("operator", "operator-secret")),
            await verifier.verify(IdentityClaim("manager", "manager-secret")),
        )

    async def test_custom_router不能关闭tool自身审批且条件写入同一动作(self) -> None:
        tool = _tool(requires_approval=True)
        capabilities = CapabilityRegistry()
        capabilities.register(
            tool,
            capabilities={"resources.change"},
            domain="resources",
            operation="read",
            requires_approval=False,
        )
        provider = ScriptedProvider([])
        operator, _manager = await self._identities()

        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="tool-policy-boundary",
                state_dir=directory,
                model=self.model,
                stream_fn=provider.stream,
                system_prompt="测试",
                tools=[tool],
                router=FixedRouter(_decision()),
                capabilities=capabilities,
                auto_recover=False,
            )
            try:
                result = await host.prompt(
                    "变更资源",
                    requester=operator,
                    idempotency_key="resource-change-1",
                    entity_id="resource-1001",
                    expected_entity_version=7,
                    business_preconditions={"status": "active"},
                )
                events = await host.operation_store.load(
                    operation_id=result.operation_id
                )
            finally:
                await host.close()

        self.assertIsNotNone(result.approval_id)
        self.assertEqual(result.result.decision.status, "in_scope_approval_required")
        self.assertEqual(provider.call_count, 0)
        prepared = next(event for event in events if event.type == "write_prepared")
        self.assertEqual(prepared.data["entityId"], "resource-1001")
        self.assertEqual(prepared.data["expectedEntityVersion"], 7)
        self.assertEqual(
            prepared.data["businessPreconditions"],
            {"status": "active"},
        )
        registered = next(
            event for event in events if event.type == "approval_resume_registered"
        )
        envelope = DurableActionEnvelope.from_dict(registered.data["action"])
        self.assertEqual(envelope.entity_id, "resource-1001")
        self.assertEqual(envelope.expected_entity_version, 7)
        self.assertEqual(envelope.business_preconditions, {"status": "active"})
        self.assertEqual(prepared.data["actionHash"], envelope.action_hash)

    async def test_single_approval_resume支持context_handler(self) -> None:
        tool = _tool(requires_approval=True)
        capabilities = CapabilityRegistry()
        capabilities.register(
            tool,
            capabilities={"resources.change"},
            domain="resources",
            operation="write",
        )
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "资源已变更"}],
                )
            ]
        )
        operator, manager = await self._identities()
        seen: list[WriteExecutionContext] = []

        async def context_handler(context: WriteExecutionContext):
            seen.append(context)
            return {"status": "changed", "version": 12}

        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="single-context-handler",
                state_dir=directory,
                model=self.model,
                stream_fn=provider.stream,
                system_prompt="测试",
                tools=[tool],
                router=FixedRouter(_decision()),
                capabilities=capabilities,
                auto_recover=False,
            )
            try:
                pending = await host.prompt(
                    "变更资源",
                    requester=operator,
                    idempotency_key="single-context-handler-1",
                    entity_id="resource-12",
                    expected_entity_version=11,
                    business_preconditions={"status": "active"},
                )
                await host.approve_and_resume(
                    pending.approval_id,
                    approver=manager,
                    consumer=operator,
                    idempotency_key="single-context-handler-1",
                    context_handler=context_handler,
                )
            finally:
                await host.close()

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].entity_id, "resource-12")
        self.assertEqual(seen[0].expected_entity_version, 11)
        self.assertEqual(seen[0].business_preconditions, {"status": "active"})
        self.assertEqual(provider.call_count, 1)

    async def test_custom_router不能关闭capability或intent审批(self) -> None:
        for operation, intent_requires_approval in (
            ("write", False),
            ("read", True),
        ):
            with self.subTest(
                operation=operation,
                intent_requires_approval=intent_requires_approval,
            ):
                tool = _tool()
                capabilities = CapabilityRegistry()
                capabilities.register(
                    tool,
                    capabilities={"resources.change"},
                    domain="resources",
                    operation=operation,
                    requires_approval=False,
                )
                provider = ScriptedProvider([])
                routed = RoutedAgent(
                    Agent(model=self.model, stream_fn=provider.stream),
                    FixedRouter(
                        _decision(requires_approval=intent_requires_approval)
                    ),
                    capabilities,
                    suspend_on_approval=True,
                )

                result = await routed.prompt("变更资源")

                self.assertEqual(
                    result.decision.status,
                    "in_scope_approval_required",
                )
                self.assertTrue(result.decision.requires_approval)
                self.assertFalse(result.model_called)
                self.assertEqual(provider.call_count, 0)

    async def test_custom_router不能覆盖host持有的intent配置审批(self) -> None:
        tool = _tool()
        capabilities = CapabilityRegistry()
        capabilities.register(
            tool,
            capabilities={"resources.change"},
            domain="resources",
            operation="read",
        )
        provider = ScriptedProvider([])
        routed = RoutedAgent(
            Agent(model=self.model, stream_fn=provider.stream),
            ConfiguredRouter(_decision(requires_approval=False)),
            capabilities,
            suspend_on_approval=True,
        )

        result = await routed.prompt("变更资源")

        self.assertEqual(result.decision.status, "in_scope_approval_required")
        self.assertTrue(result.decision.requires_approval)
        self.assertEqual(provider.call_count, 0)

    async def test_no_router不能装配显式需要审批的tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "无 Router.*Approval"):
                await DurableAgentHost.create(
                    session_id="unsafe-no-router",
                    state_dir=directory,
                    model=self.model,
                    stream_fn=ScriptedProvider([]).stream,
                    system_prompt="测试",
                    tools=[_tool(requires_approval=True)],
                    auto_recover=False,
                )

        tool = _tool()
        capabilities = CapabilityRegistry()
        capabilities.register(
            tool,
            capabilities={"resources.change"},
            domain="resources",
            operation="write",
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "无 Router.*Approval"):
                await DurableAgentHost.create(
                    session_id="unsafe-capability-no-router",
                    state_dir=directory,
                    model=self.model,
                    stream_fn=ScriptedProvider([]).stream,
                    system_prompt="测试",
                    tools=[tool],
                    capabilities=capabilities,
                    auto_recover=False,
                )

        provider = ScriptedProvider([])
        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="mutated-no-router",
                state_dir=directory,
                model=self.model,
                stream_fn=provider.stream,
                system_prompt="测试",
                tools=[],
                auto_recover=False,
            )
            try:
                host.agent.state.tools = [_tool(requires_approval=True)]
                with self.assertRaisesRegex(PermissionError, "无 Router.*Approval"):
                    await host.prompt("变更资源")
            finally:
                await host.close()
        self.assertEqual(provider.call_count, 0)

    async def test_no_router配置失败不会污染受管session配置事件(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            workspace = DurableAgentWorkspace.open(root / "state")
            project = await workspace.create_project(project_path, title="Demo")
            session = await workspace.create_session(
                project.project_id,
                title="失败装配不落配置",
            )
            before = await workspace.catalog.journal.load_events(
                workspace.principal,
                journal_kind="audit",
            )

            with self.assertRaisesRegex(ValueError, "无 Router.*Approval"):
                await workspace.open_session(
                    session.session_id,
                    model=self.model,
                    stream_fn=ScriptedProvider([]).stream,
                    system_prompt="测试",
                    tools=[_tool(requires_approval=True)],
                    auto_recover=False,
                )

            metadata = await workspace.catalog.get_session(session.session_id)
            after = await workspace.catalog.journal.load_events(
                workspace.principal,
                journal_kind="audit",
            )

        self.assertIsNone(metadata.configuration_hash)
        self.assertEqual(after, before)
        self.assertFalse(
            any(
                event.event_type
                in {"session_configuration_bound", "session_configuration_migrated"}
                and event.payload.get("sessionId") == session.session_id
                for event in after
            )
        )

    async def test_approval批准后过期也不能被消费(self) -> None:
        store = DelayedConsumeStore()
        await store.append(
            "operation_started",
            "session-1",
            "operation-1",
            {"configuration": {}, "tools": []},
        )
        operator, manager = await self._identities()
        approvals = ApprovalService(store)
        envelope = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="call-1",
            tool_name="change_resource",
            arguments={},
            write_id="write-1",
        )
        approval = await approvals.request(
            session_id="session-1",
            operation_id="operation-1",
            requester=operator,
            action=envelope.to_dict(),
            action_summary="变更资源",
            required_role="approver",
            ttl_seconds=0.2,
        )
        await approvals.grant(approval.approval_id, manager)

        with self.assertRaises(ApprovalError) as raised:
            await approvals.consume(
                approval.approval_id,
                action=envelope.to_dict(),
                consumer=operator,
            )

        self.assertEqual(raised.exception.code, "approval_expired")
        events = await store.load(operation_id="operation-1")
        self.assertFalse(any(event.type == "approval_consumed" for event in events))

    async def test_approval写事务在消费瞬间再次检查ttl(self) -> None:
        store = DelayedConsumeStore()
        await store.append(
            "operation_started",
            "session-1",
            "operation-1",
            {"configuration": {}, "tools": []},
        )
        operator, manager = await self._identities()
        approvals = ApprovalService(store)
        coordinator = ApprovalResumeCoordinator(store, approvals)
        envelope = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="call-1",
            tool_name="change_resource",
            arguments={},
            write_id="write-1",
        )
        pending = await coordinator.request(
            session_id="session-1",
            operation_id="operation-1",
            requester=operator,
            action=envelope.to_dict(),
            action_summary="变更资源",
            required_role="approver",
            resume_payload={"envelope": envelope.to_dict()},
            idempotency_key="write-ttl-1",
            ttl_seconds=0.2,
        )
        await approvals.grant(pending.approval.approval_id, manager)
        handler_calls = 0

        async def handler(_arguments, _key, _actor):
            nonlocal handler_calls
            handler_calls += 1
            return {"status": "changed"}

        with self.assertRaises(ApprovalError) as raised:
            await WriteOperationService(store, approvals).execute(
                envelope.write_id,
                actor=operator,
                idempotency_key="write-ttl-1",
                handler=handler,
                approval_resume_id=pending.approval.approval_id,
            )

        self.assertEqual(raised.exception.code, "approval_expired")
        self.assertEqual(handler_calls, 0)
        events = await store.load(operation_id="operation-1")
        self.assertFalse(any(event.type == "write_submitting" for event in events))

    async def test_coordinator审批路径保留entity和业务前置条件(self) -> None:
        store = InMemoryOperationEventStore()
        await store.append(
            "operation_started",
            "session-1",
            "operation-1",
            {"configuration": {}, "tools": []},
        )
        operator, _manager = await self._identities()
        approvals = ApprovalService(store)
        coordinator = ApprovalResumeCoordinator(store, approvals)
        envelope = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="call-1",
            tool_name="change_resource",
            arguments={},
            write_id="write-1",
            entity_id="resource-1",
            expected_entity_version=9,
            business_preconditions={"status": "active"},
        )

        await coordinator.request(
            session_id="session-1",
            operation_id="operation-1",
            requester=operator,
            action=envelope.to_dict(),
            action_summary="变更资源",
            required_role="approver",
            resume_payload={"envelope": envelope.to_dict()},
            idempotency_key="coordinator-resource-1",
        )

        events = await store.load(operation_id="operation-1")
        prepared = next(event for event in events if event.type == "write_prepared")
        self.assertEqual(prepared.data["entityId"], "resource-1")
        self.assertEqual(prepared.data["expectedEntityVersion"], 9)
        self.assertEqual(
            prepared.data["businessPreconditions"],
            {"status": "active"},
        )

    async def test_write_context向cas_adapter提供持久条件且隔离可变输入(self) -> None:
        store = InMemoryOperationEventStore()
        operator, _manager = await self._identities()
        approvals = ApprovalService(store)
        service = WriteOperationService(store, approvals)
        await store.append(
            "operation_started",
            "session-1",
            "operation-success",
            {"configuration": {}, "tools": []},
        )
        arguments = {"quantity": 2, "nested": {"source": "caller"}}
        preconditions = {"status": "active", "region": {"id": "cn"}}
        write = await service.prepare(
            session_id="session-1",
            operation_id="operation-success",
            tool_name="change_resource",
            arguments=arguments,
            idempotency_key="cas-success",
            requester=operator,
            requires_approval=False,
            entity_id="resource-1",
            expected_entity_version=7,
            business_preconditions=preconditions,
        )
        arguments["nested"]["source"] = "tampered"
        preconditions["region"]["id"] = "tampered"
        database = {"version": 7, "status": "active", "quantity": 0}
        seen: list[WriteExecutionContext] = []

        async def compare_and_swap(context: WriteExecutionContext):
            seen.append(context)
            if database["version"] != context.expected_entity_version:
                raise DefinitelyNotCommittedToolError(
                    "实体版本冲突",
                    code="entity_version_conflict",
                    public_message="实体版本已经变化",
                )
            if database["status"] != context.business_preconditions["status"]:
                raise DefinitelyNotCommittedToolError(
                    "状态已变化",
                    code="business_precondition_failed",
                    public_message="业务前置条件不满足",
                )
            quantity = context.arguments["quantity"]
            # Adapter 误改自己的 Context，也不能改写已持久化动作。
            context.arguments["nested"]["source"] = "adapter-mutated"
            context.business_preconditions["region"]["id"] = "adapter-mutated"
            database.update(version=database["version"] + 1, quantity=quantity)
            return {"version": database["version"]}

        completed = await service.execute(
            write.write_id,
            actor=operator,
            idempotency_key="cas-success",
            context_handler=compare_and_swap,
        )
        persisted = await service.get(write.write_id)

        self.assertEqual(completed.state, "succeeded")
        self.assertEqual(database, {"version": 8, "status": "active", "quantity": 2})
        self.assertEqual(seen[0].entity_id, "resource-1")
        self.assertEqual(seen[0].expected_entity_version, 7)
        self.assertEqual(seen[0].arguments["nested"]["source"], "adapter-mutated")
        self.assertEqual(persisted.arguments["nested"]["source"], "caller")
        self.assertEqual(persisted.business_preconditions["region"]["id"], "cn")
        with self.assertRaises(AttributeError):
            seen[0].entity_id = "resource-2"

        await store.append(
            "operation_started",
            "session-1",
            "operation-stale",
            {"configuration": {}, "tools": []},
        )
        stale = await service.prepare(
            session_id="session-1",
            operation_id="operation-stale",
            tool_name="change_resource",
            arguments={"quantity": 5},
            idempotency_key="cas-stale",
            requester=operator,
            requires_approval=False,
            entity_id="resource-1",
            expected_entity_version=7,
            business_preconditions={"status": "active"},
        )
        with self.assertRaises(DefinitelyNotCommittedToolError) as raised:
            await service.execute(
                stale.write_id,
                actor=operator,
                idempotency_key="cas-stale",
                context_handler=compare_and_swap,
            )

        self.assertEqual(raised.exception.code, "entity_version_conflict")
        self.assertEqual(database, {"version": 8, "status": "active", "quantity": 2})
        self.assertEqual((await service.get(stale.write_id)).state, "failed")

    def test_action_hash绑定entity_version和业务前置条件(self) -> None:
        preconditions = {"status": "active", "owner": {"id": "u-1"}}
        envelope = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="call-1",
            tool_name="change_resource",
            arguments={},
            write_id="write-1",
            entity_id="resource-1",
            expected_entity_version=3,
            business_preconditions=preconditions,
        )
        preconditions["status"] = "tampered"
        restored = DurableActionEnvelope.from_dict(envelope.to_dict())
        changed = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="call-1",
            tool_name="change_resource",
            arguments={},
            write_id="write-1",
            entity_id="resource-1",
            expected_entity_version=4,
            business_preconditions={"status": "active", "owner": {"id": "u-1"}},
        )

        self.assertEqual(restored, envelope)
        self.assertEqual(envelope.business_preconditions["status"], "active")
        self.assertNotEqual(envelope.action_hash, changed.action_hash)

    def test_session配置哈希覆盖全部策略和版本边界(self) -> None:
        base_tool = _tool(implementation_version="impl-1")
        capabilities = CapabilityRegistry()
        capabilities.register(
            base_tool,
            capabilities={"resources.change"},
            domain="resources",
            operation="read",
        )
        plan = {
            "resource.read": IntentPlanPolicy(
                "resource.read",
                capabilities=("resources.read",),
            )
        }

        def digest(**overrides):
            values = {
                "model": self.model,
                "system_prompt": "测试",
                "tools": [base_tool],
                "router": FixedRouter(_decision()),
                "capabilities": capabilities,
                "plan_policies": plan,
            }
            values.update(overrides)
            return agent_configuration_hash(**values)

        baseline = digest()
        changed_capabilities = CapabilityRegistry()
        changed_capabilities.register(
            base_tool,
            capabilities={"resources.change"},
            domain="resources",
            operation="write",
        )
        side_effect_capabilities = CapabilityRegistry()
        side_effect_capabilities.register(
            base_tool,
            capabilities={"resources.change"},
            domain="resources",
            operation="read",
            side_effect=True,
        )
        changed_plan = {
            "resource.other": IntentPlanPolicy(
                "resource.other",
                capabilities=("resources.other",),
            )
        }
        versioned_tool = _tool(implementation_version="impl-2")
        secured_tool = _tool(security_policy_version="security-2")
        contextual_tool = _tool(contextual=True)

        variants = (
            digest(router_policy_version="router-2"),
            digest(capabilities=changed_capabilities),
            digest(capabilities=side_effect_capabilities),
            digest(approval_policy_version="approval-2"),
            digest(plan_policies=changed_plan),
            digest(plan_policy_version="plan-2"),
            digest(tools=[versioned_tool]),
            digest(tools=[secured_tool]),
            digest(tools=[contextual_tool]),
            digest(security_policy_version="host-security-2"),
        )
        self.assertTrue(all(value != baseline for value in variants))

    async def test_managed_session拒绝静默切换tool实现版本(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            workspace = DurableAgentWorkspace.open(root / "state")
            project = await workspace.create_project(project_path, title="Demo")
            session = await workspace.create_session(
                project.project_id,
                title="Tool 版本边界",
            )
            host = await workspace.open_session(
                session.session_id,
                model=self.model,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="测试",
                tools=[_tool(implementation_version="impl-1")],
            )
            await host.close()

            with self.assertRaises(SessionConfigurationMismatchError):
                await workspace.open_session(
                    session.session_id,
                    model=self.model,
                    stream_fn=ScriptedProvider([]).stream,
                    system_prompt="测试",
                    tools=[_tool(implementation_version="impl-2")],
                )


if __name__ == "__main__":
    unittest.main()
