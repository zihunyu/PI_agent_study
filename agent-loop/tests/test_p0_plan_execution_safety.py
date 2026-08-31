"""Regression tests for non-downgradable Plan execution policy boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from pi_agent_loop import (
    AgentTool,
    AgentToolResult,
    DurableAgentHost,
    DurablePlanWorkflow,
    HybridRequestPlanner,
    InMemoryOperationEventStore,
    IdentityClaim,
    JournalPrincipal,
    IntentPlanPolicy,
    Model,
    MultiIntentPlan,
    PlanApprovalReceipt,
    PlanStep,
    PlanToolDispatchError,
    PlanWriteMetadata,
    ScriptedProvider,
    SessionJournalOperationEventStore,
    SessionJournalPlanStore,
    SQLiteSessionEventJournal,
    StaticJournalKeyProvider,
    StaticIdentityVerifier,
    ApprovalService,
    ToolDispatchContext,
    ToolDispatchRuntime,
    ToolRuntimePlanStepExecutor,
    VerifiedIdentity,
    WriteOperationService,
    CancellationToken,
)


MODEL = Model(id="plan-p0", provider="test", api="scripted")


async def _execute(_call_id, _arguments, _token, _update):
    return AgentToolResult(content=[])


class PlanReplayPolicyRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_policy不能绑定never_replay_tool(self) -> None:
        tool = AgentTool("effect", "effect", "effect", _execute)
        policy = IntentPlanPolicy("effect.run", replay_policy="safe")

        with self.assertRaisesRegex(ValueError, "降低.*replay_policy"):
            ToolRuntimePlanStepExecutor(
                runtime_provider=lambda: ToolDispatchRuntime([tool]),
                tools=[tool],
                intent_tools={policy.intent: tool.name},
                model=MODEL,
                session_id="session-a",
                dispatch_context_provider=ToolDispatchContext,
                policies={policy.intent: policy},
            )

    async def test_dangerous_tool不能省略intent_policy(self) -> None:
        tool = AgentTool("effect", "effect", "effect", _execute)

        with self.assertRaisesRegex(ValueError, "危险 Plan Tool.*Intent Policy"):
            ToolRuntimePlanStepExecutor(
                runtime_provider=lambda: ToolDispatchRuntime([tool]),
                tools=[tool],
                intent_tools={"effect.run": tool.name},
                model=MODEL,
                session_id="session-a",
                dispatch_context_provider=ToolDispatchContext,
            )

    async def test_factory不能遗漏planner中的危险有效policy(self) -> None:
        policy = IntentPlanPolicy(
            "effect.run",
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("approver",),
        )
        planner = HybridRequestPlanner(
            {policy.intent: policy},
            lambda *_args: {"steps": []},
        )

        async def unsafe_step(_step, _cancellation):
            return None

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "plan_tool_bindings"):
                await DurableAgentHost.create(
                    session_id="effective-policy",
                    state_dir=directory,
                    model=MODEL,
                    stream_fn=ScriptedProvider([]).stream,
                    system_prompt="test",
                    tools=[],
                    auto_recover=False,
                    planner=planner,
                    plan_step_executor=unsafe_step,
                )


class PlanWriteBoundaryRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = InMemoryOperationEventStore()
        await self.store.append(
            "operation_started",
            "session-write",
            "operation-write",
            {"configuration": {}, "tools": []},
        )
        self.identity = await StaticIdentityVerifier(
            {"writer": ("writer-credential", {"operator"})}
        ).verify(
            IdentityClaim(
                principal_id="writer",
                credential="writer-credential",
            )
        )
        self.policy = IntentPlanPolicy(
            "entity.write",
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("approver",),
        )
        self.step = PlanStep(
            "write",
            self.policy.intent,
            arguments={"entity": "e-1", "value": 2},
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=self.policy.approval_roles,
        )
        approver = VerifiedIdentity(
            principal_id="manager",
            roles=frozenset({"approver"}),
            issuer="test",
            verification_id="approver-proof",
        )
        now = time.time()
        self.receipt = PlanApprovalReceipt.issue(
            approval_id="approval-1",
            receipt_id="receipt-1",
            plan_id="plan-write",
            step=self.step,
            state_version=1,
            approver=approver,
            verification_id="receipt-proof",
            issued_at=now - 1,
            expires_at=now + 60,
        ).consumed(now)

    def _context(
        self,
        *,
        receipt: PlanApprovalReceipt | None = None,
        authorization_action_hash: str | None = None,
    ):
        return SimpleNamespace(
            resolved_arguments=dict(self.step.arguments),
            approval_receipt=self.receipt if receipt is None else receipt,
            plan_id="plan-write",
            fencing_token=None,
            identity=self.identity,
            authorization_action_hash=(
                self.step.action_hash
                if authorization_action_hash is None
                else authorization_action_hash
            ),
        )

    async def _authorized_context(self, adapter):
        base = self._context()
        action = await adapter.build_authorization_action(self.step, base)
        encoded = json.dumps(
            action,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        action_hash = hashlib.sha256(encoded).hexdigest()
        approver = VerifiedIdentity(
            principal_id="manager",
            roles=frozenset({"approver"}),
            issuer="test",
            verification_id="approver-proof",
        )
        now = time.time()
        receipt = PlanApprovalReceipt.issue(
            approval_id="approval-exact",
            receipt_id="receipt-exact",
            plan_id="plan-write",
            step=replace(self.step, _execution_action_hash=action_hash),
            state_version=1,
            approver=approver,
            verification_id="receipt-exact-proof",
            issued_at=now - 1,
            expires_at=now + 60,
        ).consumed(now)
        return self._context(
            receipt=receipt,
            authorization_action_hash=action_hash,
        )

    async def test_write_step复用完整write_state_machine且幂等(self) -> None:
        calls = 0

        async def execute(_call_id, arguments, _context, _token, _update):
            nonlocal calls
            calls += 1
            return AgentToolResult(
                content=[{"type": "text", "text": "updated"}],
                details={"entity": arguments["entity"], "version": 3},
            )

        tool = AgentTool(
            "write_entity",
            "write",
            "write",
            None,
            execute_with_context=execute,
            replay_policy="never",
        )
        writes = WriteOperationService(self.store, ApprovalService(self.store))
        runtime = ToolDispatchRuntime([tool])
        adapter = ToolRuntimePlanStepExecutor(
            runtime_provider=lambda: runtime,
            tools=[tool],
            intent_tools={self.policy.intent: tool.name},
            model=MODEL,
            session_id="session-write",
            dispatch_context_provider=lambda: ToolDispatchContext(
                identity=self.identity, tenant_id="tenant-a"
            ),
            policies={self.policy.intent: self.policy},
            write_service_provider=lambda: writes,
            write_metadata_provider=lambda *_args: PlanWriteMetadata(
                operation_id="operation-write",
                idempotency_key="stable-business-key",
                entity_id="e-1",
                expected_entity_version=2,
                business_preconditions={"state": "active"},
            ),
        )

        context = await self._authorized_context(adapter)
        first = await adapter(
            self.step,
            CancellationToken(),
            context=context,
        )
        second = await adapter(
            self.step,
            CancellationToken(),
            context=context,
        )

        self.assertEqual(calls, 1)
        self.assertEqual(first, second)
        events = await self.store.load(
            session_id="session-write", operation_id="operation-write"
        )
        prepared = next(event for event in events if event.type == "write_prepared")
        self.assertEqual(prepared.data["expectedEntityVersion"], 2)
        self.assertEqual(
            prepared.data["trustedAuthorization"]["receiptId"], "receipt-exact"
        )
        self.assertEqual(
            [event.type for event in events].count("write_submitting"), 1
        )
        self.assertEqual(
            [event.type for event in events].count("write_succeeded"), 1
        )

    async def test_write_step缺metadata时tool零调用(self) -> None:
        calls = 0

        async def execute(_call_id, _arguments, _context, _token, _update):
            nonlocal calls
            calls += 1
            return AgentToolResult(content=[])

        tool = AgentTool(
            "write_entity",
            "write",
            "write",
            None,
            execute_with_context=execute,
            replay_policy="never",
        )
        adapter = ToolRuntimePlanStepExecutor(
            runtime_provider=lambda: ToolDispatchRuntime([tool]),
            tools=[tool],
            intent_tools={self.policy.intent: tool.name},
            model=MODEL,
            session_id="session-write",
            dispatch_context_provider=lambda: ToolDispatchContext(identity=self.identity),
            policies={self.policy.intent: self.policy},
        )
        with self.assertRaisesRegex(Exception, "WriteOperationService"):
            await adapter(
                self.step,
                CancellationToken(),
                fencing_token=3,
                context=self._context(),
            )
        self.assertEqual(calls, 0)

    async def test_write_metadata审批后变化时tool零调用(self) -> None:
        calls = 0
        metadata_calls = 0

        async def execute(_call_id, _arguments, _context, _token, _update):
            nonlocal calls
            calls += 1
            return AgentToolResult(content=[])

        def metadata_provider(*_args):
            nonlocal metadata_calls
            metadata_calls += 1
            if metadata_calls == 1:
                return PlanWriteMetadata(
                    operation_id="operation-write",
                    idempotency_key="approved-key",
                    entity_id="e-1",
                    expected_entity_version=2,
                )
            return PlanWriteMetadata(
                operation_id="operation-write",
                idempotency_key="changed-key",
                entity_id="e-2",
                expected_entity_version=3,
            )

        tool = AgentTool(
            "write_entity",
            "write",
            "write",
            None,
            execute_with_context=execute,
            replay_policy="never",
        )
        writes = WriteOperationService(self.store, ApprovalService(self.store))
        adapter = ToolRuntimePlanStepExecutor(
            runtime_provider=lambda: ToolDispatchRuntime([tool]),
            tools=[tool],
            intent_tools={self.policy.intent: tool.name},
            model=MODEL,
            session_id="session-write",
            dispatch_context_provider=lambda: ToolDispatchContext(
                identity=self.identity
            ),
            policies={self.policy.intent: self.policy},
            write_service_provider=lambda: writes,
            write_metadata_provider=metadata_provider,
        )
        context = await self._authorized_context(adapter)

        with self.assertRaisesRegex(Exception, "审批后发生变化"):
            await adapter(
                self.step,
                CancellationToken(),
                fencing_token=3,
                context=context,
            )
        self.assertEqual(calls, 0)
        events = await self.store.load(
            session_id="session-write", operation_id="operation-write"
        )
        self.assertNotIn("write_prepared", [event.type for event in events])

    async def test_factory拒绝两个不一致的policy_catalog(self) -> None:
        planner_policy = IntentPlanPolicy("records.read")
        configured_policy = IntentPlanPolicy(
            "records.read",
            replay_policy="never",
        )
        planner = HybridRequestPlanner(
            {planner_policy.intent: planner_policy},
            lambda *_args: {"steps": []},
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "planner.policies 不一致"):
                await DurableAgentHost.create(
                    session_id="conflicting-policy",
                    state_dir=directory,
                    model=MODEL,
                    stream_fn=ScriptedProvider([]).stream,
                    system_prompt="test",
                    tools=[],
                    auto_recover=False,
                    planner=planner,
                    plan_policies={configured_policy.intent: configured_policy},
                )


class PlanWriteFencingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _fixture(self, directory: str, *, service_type=WriteOperationService):
        journal = SQLiteSessionEventJournal(
            Path(directory) / "state.sqlite3",
            key_provider=StaticJournalKeyProvider(
                {"test": b"f" * 32}, active_key_id="test"
            ),
        )
        principal = JournalPrincipal.system("tenant")
        plan_store = SessionJournalPlanStore(
            journal, principal, session_id="fenced-session"
        )
        operation_store = SessionJournalOperationEventStore(journal, principal)
        await operation_store.append(
            "operation_started",
            "fenced-session",
            "write-operation",
            {"configuration": {}, "tools": []},
        )
        identity = await StaticIdentityVerifier(
            {"writer": ("writer-credential", {"operator"})}
        ).verify(
            IdentityClaim(
                principal_id="writer",
                credential="writer-credential",
            )
        )
        policy = IntentPlanPolicy(
            "entity.write",
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("approver",),
        )
        step = PlanStep(
            "write",
            policy.intent,
            arguments={"entity": "e-1"},
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=policy.approval_roles,
        )
        plan = MultiIntentPlan("write", (step,), plan_id="fenced-plan")
        await plan_store.initialize(plan)
        service = service_type(operation_store, ApprovalService(operation_store))
        return plan_store, operation_store, identity, policy, step, service

    async def test_dangerous_plan_rejects_direct_step_callback_before_side_effect(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan_store, _events, _identity, policy, _step, _service = (
                await self._fixture(directory)
            )
            calls = 0

            async def unsafe_callback(_step, _token, **_kwargs):
                nonlocal calls
                calls += 1
                return "must not execute"

            workflow = DurablePlanWorkflow(
                store=plan_store,
                planner=None,
                policies={policy.intent: policy},
                step_executor=unsafe_callback,
                approval_barrier=None,
            )

            with self.assertRaisesRegex(Exception, "ToolRuntimePlanStepExecutor"):
                await workflow.execute("fenced-plan")
            self.assertEqual(calls, 0)

    async def test_dangerous_plan_rejects_tool_adapter_without_write_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan_store, _events, identity, policy, _step, _service = (
                await self._fixture(directory)
            )
            calls = 0

            async def execute(_id, _args, _context, _token, _update):
                nonlocal calls
                calls += 1
                return AgentToolResult(content=[])

            tool = AgentTool(
                "write_entity",
                "write",
                "write",
                None,
                execute_with_context=execute,
                replay_policy="never",
            )
            adapter = ToolRuntimePlanStepExecutor(
                runtime_provider=lambda: ToolDispatchRuntime([tool]),
                tools=[tool],
                intent_tools={policy.intent: tool.name},
                model=MODEL,
                session_id="fenced-session",
                dispatch_context_provider=lambda: ToolDispatchContext(
                    identity=identity
                ),
                policies={policy.intent: policy},
            )
            workflow = DurablePlanWorkflow(
                store=plan_store,
                planner=None,
                policies={policy.intent: policy},
                step_executor=adapter,
                approval_barrier=None,
            )

            with self.assertRaisesRegex(Exception, "WriteOperationService"):
                await workflow.execute("fenced-plan")
            self.assertEqual(calls, 0)

    async def _context(self, adapter, step, identity, lease, lease_seconds):
        base = SimpleNamespace(
            resolved_arguments=dict(step.arguments),
            approval_receipt=None,
            plan_id="fenced-plan",
            fencing_token=lease.fencing_token,
            fencing_scope=lease.resource_id,
            fenced_claim=lease,
            fenced_claim_lease_seconds=lease_seconds,
            identity=identity,
        )
        action = await adapter.build_authorization_action(step, base)
        digest = hashlib.sha256(
            json.dumps(
                action,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        now = time.time()
        receipt = PlanApprovalReceipt.issue(
            approval_id="approval",
            receipt_id="receipt",
            plan_id="fenced-plan",
            step=replace(step, _execution_action_hash=digest),
            state_version=1,
            approver=VerifiedIdentity(
                principal_id="manager",
                roles=frozenset({"approver"}),
                issuer="test",
                verification_id="manager-proof",
            ),
            verification_id="approval-proof",
            issued_at=now - 1,
            expires_at=now + 60,
        ).consumed(now)
        base.approval_receipt = receipt
        base.authorization_action_hash = digest
        return base

    async def test_successful_plan_write_receives_canonical_claim_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (
                plan_store,
                _operation_store,
                identity,
                policy,
                step,
                service,
            ) = await self._fixture(directory)
            observed_scopes: list[str | None] = []

            async def execute(_id, _args, context, _token, _update):
                observed_scopes.append(context.fencing_scope)
                return AgentToolResult(content=[], details={"ok": True})

            tool = AgentTool(
                "write_entity",
                "write",
                "write",
                None,
                execute_with_context=execute,
                replay_policy="never",
            )
            adapter = ToolRuntimePlanStepExecutor(
                runtime_provider=lambda: ToolDispatchRuntime([tool]),
                tools=[tool],
                intent_tools={policy.intent: tool.name},
                model=MODEL,
                session_id="fenced-session",
                dispatch_context_provider=lambda: ToolDispatchContext(identity=identity),
                policies={policy.intent: policy},
                write_service_provider=lambda: service,
                write_metadata_provider=lambda *_args: PlanWriteMetadata(
                    operation_id="write-operation",
                    idempotency_key="write-key",
                    entity_id="e-1",
                    expected_entity_version=1,
                ),
            )
            lease = await plan_store.acquire_execution(
                "fenced-plan", "worker", lease_seconds=2
            )
            assert lease is not None
            context = await self._context(adapter, step, identity, lease, 2)

            await adapter(step, CancellationToken(), context=context)

            self.assertEqual(observed_scopes, [lease.resource_id])

    async def test_successor_takeover_before_prepare_blocks_all_write_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (
                plan_store,
                operation_store,
                identity,
                policy,
                step,
                service,
            ) = await self._fixture(directory)
            calls = 0

            async def execute(_id, _args, _context, _token, _update):
                nonlocal calls
                calls += 1
                return AgentToolResult(content=[])

            tool = AgentTool(
                "write_entity",
                "write",
                "write",
                None,
                execute_with_context=execute,
                replay_policy="never",
            )
            adapter = ToolRuntimePlanStepExecutor(
                runtime_provider=lambda: ToolDispatchRuntime([tool]),
                tools=[tool],
                intent_tools={policy.intent: tool.name},
                model=MODEL,
                session_id="fenced-session",
                dispatch_context_provider=lambda: ToolDispatchContext(identity=identity),
                policies={policy.intent: policy},
                write_service_provider=lambda: service,
                write_metadata_provider=lambda *_args: PlanWriteMetadata(
                    operation_id="write-operation",
                    idempotency_key="stale-prepare-key",
                    entity_id="e-1",
                    expected_entity_version=1,
                ),
            )
            stale = await plan_store.acquire_execution(
                "fenced-plan", "stale-worker", lease_seconds=0.03
            )
            assert stale is not None
            context = await self._context(adapter, step, identity, stale, 0.03)
            await asyncio.sleep(0.06)
            successor = await plan_store.acquire_execution(
                "fenced-plan", "successor", lease_seconds=1
            )
            assert successor is not None

            with self.assertRaisesRegex(
                PlanToolDispatchError,
                "Plan write preparation failed",
            ) as raised:
                await adapter(step, CancellationToken(), context=context)

            self.assertFalse(raised.exception.outcome_unknown)
            self.assertTrue(raised.exception.definitely_not_committed)
            self.assertEqual(calls, 0)
            events = await operation_store.load(
                session_id="fenced-session", operation_id="write-operation"
            )
            self.assertNotIn("write_prepared", [event.type for event in events])

    async def test_successor_takeover_before_submitting_blocks_tool_and_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            holder: dict[str, object] = {}

            class TakeoverAfterPrepareService(WriteOperationService):
                async def prepare(self, **kwargs):
                    prepared = await super().prepare(**kwargs)
                    # prepare must renew using the Plan's short policy (0.5s),
                    # not WriteService's unrelated 300s reconciliation default.
                    await asyncio.sleep(0.7)
                    successor = await holder["plan_store"].acquire_execution(  # type: ignore[union-attr]
                        "fenced-plan", "successor", lease_seconds=1
                    )
                    if successor is None:
                        raise AssertionError("successor should acquire expired short lease")
                    holder["successor"] = successor
                    return prepared

            (
                plan_store,
                operation_store,
                identity,
                policy,
                step,
                service,
            ) = await self._fixture(
                directory,
                service_type=TakeoverAfterPrepareService,
            )
            holder["plan_store"] = plan_store
            calls = 0

            async def execute(_id, _args, _context, _token, _update):
                nonlocal calls
                calls += 1
                return AgentToolResult(content=[])

            tool = AgentTool(
                "write_entity",
                "write",
                "write",
                None,
                execute_with_context=execute,
                replay_policy="never",
            )
            adapter = ToolRuntimePlanStepExecutor(
                runtime_provider=lambda: ToolDispatchRuntime([tool]),
                tools=[tool],
                intent_tools={policy.intent: tool.name},
                model=MODEL,
                session_id="fenced-session",
                dispatch_context_provider=lambda: ToolDispatchContext(identity=identity),
                policies={policy.intent: policy},
                write_service_provider=lambda: service,
                write_metadata_provider=lambda *_args: PlanWriteMetadata(
                    operation_id="write-operation",
                    idempotency_key="takeover-key",
                    entity_id="e-1",
                    expected_entity_version=1,
                ),
            )
            stale = await plan_store.acquire_execution(
                "fenced-plan", "stale-worker", lease_seconds=0.5
            )
            assert stale is not None
            context = await self._context(adapter, step, identity, stale, 0.5)

            with self.assertRaisesRegex(
                PlanToolDispatchError,
                "Plan write execution failed",
            ) as raised:
                await adapter(step, CancellationToken(), context=context)

            self.assertFalse(raised.exception.outcome_unknown)
            self.assertTrue(raised.exception.definitely_not_committed)
            self.assertEqual(calls, 0)
            events = await operation_store.load(
                session_id="fenced-session", operation_id="write-operation"
            )
            types = [event.type for event in events]
            self.assertIn("write_prepared", types)
            self.assertNotIn("write_submitting", types)
            self.assertNotIn("write_succeeded", types)
            self.assertNotIn("write_failed", types)
            self.assertIn("successor", holder)


if __name__ == "__main__":
    unittest.main()
