"""Startup recovery fail-closed and monotonic claim fencing regressions."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (
    ApprovalResumeCoordinator,
    ApprovalService,
    DurableActionEnvelope,
    DurableAgentHost,
    DurableHostResources,
    DurableSessionRecovery,
    IdentityClaim,
    InMemoryOperationEventStore,
    JsonlOperationEventStore,
    Model,
    ModelRequestPolicy,
    RecoveryCallbacks,
    ScriptedProvider,
    SQLiteOperationEventStore,
    StaticIdentityVerifier,
    StartupRecoveryBlockedError,
    WriteOperationService,
    assistant_message,
    replay_operation,
)
from pi_agent_loop.session.operation_store import fenced_claim_resource_id


MODEL = Model(id="recovery-safety", provider="scripted", api="scripted")


async def _unexpected(*_args, **_kwargs):
    raise AssertionError("unexpected callback")


async def _approved_resume_fixture(directory: str, session_id: str):
    resources = DurableHostResources.create(directory, session_id=session_id)
    store = resources.operation_store
    verifier = StaticIdentityVerifier(
        {
            "operator": ("operator-secret", {"operator"}),
            "approver": ("approver-secret", {"approver"}),
        }
    )
    operator = await verifier.verify(
        IdentityClaim("operator", "operator-secret")
    )
    approver = await verifier.verify(
        IdentityClaim("approver", "approver-secret")
    )
    operation_id = "approval-operation"
    await store.append_batch(
        session_id,
        operation_id,
        [
            ("operation_started", {"configuration": {}, "tools": []}),
            (
                "message_appended",
                {
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "write"}],
                    }
                },
            ),
            (
                "model_policy_selected",
                {"policy": ModelRequestPolicy.no_tools().to_dict()},
            ),
        ],
    )
    approvals = ApprovalService(store, session_id=session_id)
    coordinator = ApprovalResumeCoordinator(
        store,
        approvals,
        session_id=session_id,
    )
    envelope = DurableActionEnvelope(
        operation_id=operation_id,
        tool_call_id="write-call",
        tool_name="write_tool",
        arguments={"value": 1},
        write_id="write-id",
    )
    pending = await coordinator.request(
        session_id=session_id,
        operation_id=operation_id,
        requester=operator,
        action=envelope.to_dict(),
        action_summary="write one",
        required_role="approver",
        resume_payload={"envelope": envelope.to_dict()},
        idempotency_key="write-key",
    )
    await approvals.grant(pending.approval.approval_id, approver)
    return resources, approvals, operator, pending


class RecoveryFencingAndGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_generation_in_different_operations_has_distinct_scope(
        self,
    ) -> None:
        store = InMemoryOperationEventStore()
        first_scope = fenced_claim_resource_id(
            "operation_recovery",
            session_id="session",
            operation_id="operation-a",
        )
        second_scope = fenced_claim_resource_id(
            "operation_recovery",
            session_id="session",
            operation_id="operation-b",
        )
        first = await store.acquire_fenced_claim(
            "operation_recovery",
            first_scope,
            "worker-a",
        )
        second = await store.acquire_fenced_claim(
            "operation_recovery",
            second_scope,
            "worker-b",
        )
        assert first is not None and second is not None
        self.assertEqual(first.fencing_token, second.fencing_token)
        self.assertNotEqual(first.resource_id, second.resource_id)

    async def _assert_expired_lease_cannot_be_revived(self, store) -> None:
        natural = await store.acquire_fenced_claim(
            "matrix",
            "natural-expiry",
            "worker-a",
            lease_seconds=0.01,
        )
        self.assertIsNotNone(natural)
        assert natural is not None
        await asyncio.sleep(0.03)
        self.assertFalse(await store.verify_fenced_claim(natural))
        self.assertFalse(
            await store.renew_fenced_claim(natural, lease_seconds=1),
            "已经过期的 Worker 必须永久失去本代租约，不能原地复活",
        )
        successor_after_expiry = await store.acquire_fenced_claim(
            "matrix",
            "natural-expiry",
            "worker-a",
            lease_seconds=1,
        )
        self.assertIsNotNone(successor_after_expiry)
        assert successor_after_expiry is not None
        self.assertGreater(successor_after_expiry.generation, natural.generation)
        self.assertTrue(await store.verify_fenced_claim(successor_after_expiry))

        # 旧代 release 也不能释放刚刚重新取得的新代租约。
        await store.release_fenced_claim(natural)
        self.assertTrue(await store.verify_fenced_claim(successor_after_expiry))
        await store.release_fenced_claim(successor_after_expiry)
        self.assertFalse(
            await store.renew_fenced_claim(natural, lease_seconds=1)
        )

        legacy = await store.acquire_fenced_claim(
            "matrix",
            "legacy-release",
            "worker-a",
            lease_seconds=1,
        )
        self.assertIsNotNone(legacy)
        assert legacy is not None
        await store.release_claim(
            legacy.claim_type,
            legacy.resource_id,
            legacy.owner_token,
        )
        self.assertFalse(
            await store.renew_fenced_claim(legacy, lease_seconds=1)
        )

        stale = await store.acquire_fenced_claim(
            "matrix",
            "takeover",
            "worker-a",
            lease_seconds=0.01,
        )
        self.assertIsNotNone(stale)
        assert stale is not None
        await asyncio.sleep(0.03)
        successor = await store.acquire_fenced_claim(
            "matrix",
            "takeover",
            "worker-b",
            lease_seconds=1,
        )
        self.assertIsNotNone(successor)
        assert successor is not None
        self.assertGreater(successor.generation, stale.generation)
        self.assertFalse(
            await store.renew_fenced_claim(stale, lease_seconds=1)
        )
        await store.release_fenced_claim(successor)

    async def test_all_claim_stores_reject_late_renew_without_aba(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stores = [
                InMemoryOperationEventStore(),
                JsonlOperationEventStore(Path(directory) / "events.jsonl"),
                SQLiteOperationEventStore(Path(directory) / "claims.db"),
                DurableHostResources.create(
                    Path(directory) / "journal",
                    session_id="journal-session",
                ).operation_store,
            ]
            for store in stores:
                with self.subTest(store=type(store).__name__):
                    await self._assert_expired_lease_cannot_be_revived(store)

    async def test_sqlite_claim_generation_prevents_same_owner_aba(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteOperationEventStore(Path(directory) / "claims.db")
            old = await store.acquire_fenced_claim(
                "session_writer",
                "session",
                "same-owner-token",
                lease_seconds=30,
            )
            self.assertIsNotNone(old)
            assert old is not None
            await store.release_fenced_claim(old)

            current = await store.acquire_fenced_claim(
                "session_writer",
                "session",
                "same-owner-token",
                lease_seconds=30,
            )
            self.assertIsNotNone(current)
            assert current is not None
            self.assertGreater(current.generation, old.generation)

            # A delayed cleanup from the old worker must neither renew nor
            # release the new ownership epoch, even when owner_token is reused.
            self.assertFalse(
                await store.renew_fenced_claim(old, lease_seconds=30)
            )
            await store.release_fenced_claim(old)
            self.assertTrue(await store.verify_fenced_claim(current))

    async def test_journal_session_writer_generation_increases_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = await DurableAgentHost.create(
                session_id="same-session",
                state_dir=directory,
                model=MODEL,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="test",
                tools=[],
                auto_recover=False,
            )
            first_token = first.session_writer_lease.fencing_token
            await first.close()

            second = await DurableAgentHost.create(
                session_id="same-session",
                state_dir=directory,
                model=MODEL,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="test",
                tools=[],
                auto_recover=False,
            )
            try:
                self.assertGreater(
                    second.session_writer_lease.fencing_token,
                    first_token,
                )
            finally:
                await second.close()

    async def test_recovery_callbacks_receive_fencing_generation(self) -> None:
        store = InMemoryOperationEventStore()
        policy = ModelRequestPolicy(
            visible_tool_names=("read_value",),
            tool_choice="required",
            allowed_tool_names=("read_value",),
            expected_tool_arguments={"key": "one"},
            continuation_policy=ModelRequestPolicy.no_tools(),
        )
        await store.append("operation_started", "session", "operation")
        await store.append(
            "model_policy_selected",
            "session",
            "operation",
            {"policy": policy.to_dict()},
        )
        await store.append(
            "model_request_started",
            "session",
            "operation",
            {"requestId": "request-1", "requestPolicy": policy.to_dict()},
        )
        await store.append(
            "model_request_completed",
            "session",
            "operation",
            {
                "requestId": "request-1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "read_value",
                            "arguments": {"key": "one"},
                        }
                    ],
                    "stopReason": "toolUse",
                },
            },
        )
        await store.append(
            "tool_intent_recorded",
            "session",
            "operation",
            {
                "toolCallId": "call-1",
                "toolName": "read_value",
                "arguments": {"key": "one"},
                "replayPolicy": "safe",
                "securityContractDigest": "c" * 64,
            },
        )
        await store.append(
            "tool_dispatch_started",
            "session",
            "operation",
            {"toolCallId": "call-1"},
        )
        tool_fences: list[int | None] = []
        tool_scopes: list[str | None] = []
        model_fences: list[str] = []
        model_scopes: list[str] = []

        async def execute(action):
            tool_fences.append(action.fencing_token)
            tool_scopes.append(action.fencing_scope)
            return {
                "role": "toolResult",
                "toolCallId": action.tool_call_id,
                "toolName": action.tool_name,
                "content": [{"type": "text", "text": "value"}],
                "details": {},
                "isError": False,
            }

        async def request_with_context(_messages, recovered_policy, identity):
            self.assertEqual(recovered_policy, ModelRequestPolicy.no_tools())
            model_fences.append(identity["fencingToken"])
            model_scopes.append(identity["fencingScope"])
            return assistant_message(
                model=MODEL,
                content=[{"type": "text", "text": "done"}],
            )

        result = await DurableSessionRecovery(store).resume(
            session_id="session",
            operation_id="operation",
            callbacks=RecoveryCallbacks(
                request_model=_unexpected,
                execute_tool=execute,
                reconcile_tool=_unexpected,
                request_model_with_context=request_with_context,
            ),
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(tool_fences), 1)
        self.assertIsInstance(tool_fences[0], int)
        self.assertGreater(tool_fences[0] or 0, 0)
        self.assertEqual(model_fences, [str(tool_fences[0])])
        self.assertEqual(model_scopes, tool_scopes)
        self.assertIn('"operationId":"operation"', tool_scopes[0] or "")
        fenced_starts = [
            event
            for event in await store.load()
            if event.type == "model_request_started"
            and event.data.get("recovery") is True
        ]
        self.assertEqual(
            fenced_starts[-1].data["fencingToken"],
            tool_fences[0],
        )

    async def test_outcome_unknown_blocks_ordinary_host_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider(
                [
                    assistant_message(
                        model=MODEL,
                        content=[{"type": "text", "text": "must not run"}],
                    )
                ]
            )
            host = await DurableAgentHost.create(
                session_id="blocked-session",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="test",
                tools=[],
                auto_recover=False,
            )
            try:
                specs = [
                    ("operation_started", {"configuration": {}, "tools": []}),
                    (
                        "tool_intent_recorded",
                        {
                            "toolCallId": "call-1",
                            "toolName": "write_tool",
                            "arguments": {"value": 1},
                            "replayPolicy": "never",
                        },
                    ),
                    ("tool_dispatch_started", {"toolCallId": "call-1"}),
                    (
                        "write_prepared",
                        {
                            "writeId": "write-1",
                            "toolCallId": "call-1",
                            "toolName": "write_tool",
                            "arguments": {"value": 1},
                            "actionHash": "a" * 64,
                            "idempotencyKeyHash": "b" * 64,
                        },
                    ),
                    ("write_approved", {"writeId": "write-1"}),
                    ("write_submitting", {"writeId": "write-1"}),
                    ("write_outcome_unknown", {"writeId": "write-1"}),
                ]
                await host.operation_store.append_batch(
                    host.session_id,
                    "uncertain-operation",
                    specs,
                )

                report = await host.recover_on_startup()
                self.assertTrue(report.blocked)
                self.assertEqual(
                    report.manual_intervention,
                    ("uncertain-operation",),
                )
                with self.assertRaises(StartupRecoveryBlockedError):
                    await host.prompt("start another write")
                self.assertEqual(provider.call_count, 0)
            finally:
                await host.close()

    async def test_factory_scans_later_blocker_before_any_recovery_callback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resources = DurableHostResources.create(
                directory,
                session_id="preflight-session",
            )
            policy = ModelRequestPolicy.no_tools()
            await resources.operation_store.append_batch(
                "preflight-session",
                "earlier-model-operation",
                [
                    ("operation_started", {"configuration": {}, "tools": []}),
                    (
                        "message_appended",
                        {
                            "message": {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "recover earlier"}
                                ],
                            }
                        },
                    ),
                    ("model_policy_selected", {"policy": policy.to_dict()}),
                ],
            )
            await resources.operation_store.append_batch(
                "preflight-session",
                "later-uncertain-operation",
                [
                    ("operation_started", {"configuration": {}, "tools": []}),
                    (
                        "tool_intent_recorded",
                        {
                            "toolCallId": "call-later",
                            "toolName": "write_tool",
                            "arguments": {"value": 1},
                            "replayPolicy": "never",
                        },
                    ),
                    ("tool_dispatch_started", {"toolCallId": "call-later"}),
                    (
                        "write_prepared",
                        {
                            "writeId": "write-later",
                            "toolCallId": "call-later",
                            "toolName": "write_tool",
                            "arguments": {"value": 1},
                            "actionHash": "c" * 64,
                            "idempotencyKeyHash": "d" * 64,
                        },
                    ),
                    ("write_approved", {"writeId": "write-later"}),
                    ("write_submitting", {"writeId": "write-later"}),
                    (
                        "write_outcome_unknown",
                        {"writeId": "write-later"},
                    ),
                ],
            )
            provider = ScriptedProvider(
                [
                    assistant_message(
                        model=MODEL,
                        content=[{"type": "text", "text": "must not run"}],
                    )
                ]
            )

            host = await DurableAgentHost.create(
                session_id="preflight-session",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="test",
                tools=[],
            )
            try:
                self.assertEqual(provider.call_count, 0)
                self.assertEqual(
                    host.startup_recovery_report.manual_intervention,
                    ("later-uncertain-operation",),
                )
                self.assertEqual(
                    host.startup_recovery_report.auto_recoverable,
                    ("earlier-model-operation",),
                )
            finally:
                await host.close()

    async def test_approved_pending_resume_runs_when_no_hard_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session_id = "approved-resume-session"
            resources, approvals, operator, pending = (
                await _approved_resume_fixture(directory, session_id)
            )
            store = resources.operation_store
            writes = WriteOperationService(store, approvals)
            handler_calls = 0
            resume_fences: list[int] = []
            write_context_fences: list[int | None] = []

            async def resume(payload, *, fencing_token, fenced_claim):
                nonlocal handler_calls
                handler_calls += 1
                resume_fences.append(fencing_token)
                envelope = DurableActionEnvelope.from_dict(payload["envelope"])
                write = await writes.execute(
                    envelope.write_id,
                    actor=operator,
                    idempotency_key="write-key",
                    context_handler=_business_success,
                    approval_resume_id=pending.approval.approval_id,
                    fencing_token=fencing_token,
                    fenced_claim=fenced_claim,
                )
                state = replay_operation(
                    await store.load(
                        session_id=session_id,
                        operation_id=envelope.operation_id,
                    )
                )
                specs = []
                if state.tools[envelope.tool_call_id].phase != "completed":
                    specs.append(
                        (
                            "tool_completed",
                            {
                                "toolCallId": envelope.tool_call_id,
                                "result": {
                                    "content": [],
                                    "details": write.result or {},
                                    "isError": False,
                                },
                            },
                        )
                    )
                specs.append(
                    (
                        "message_appended",
                        {
                            "message": {
                                "role": "toolResult",
                                "toolCallId": envelope.tool_call_id,
                                "toolName": envelope.tool_name,
                                "content": [],
                                "details": write.result or {},
                                "isError": False,
                            }
                        },
                    )
                )
                before_materialize = await store.load(
                    session_id=session_id,
                    operation_id=envelope.operation_id,
                )
                await store.append_batch_if_fenced_claim(
                    session_id,
                    envelope.operation_id,
                    specs,
                    fenced_claim,
                    renew_lease_seconds=300,
                    expected_last_sequence=before_materialize[-1].sequence,
                    expected_claim_entity_id=pending.approval.approval_id,
                )
                return {"status": "resumed"}

            async def _business_success(context):
                write_context_fences.append(context.fencing_token)
                return {"status": "succeeded"}

            provider = ScriptedProvider(
                [
                    assistant_message(
                        model=MODEL,
                        content=[{"type": "text", "text": "finished"}],
                    )
                ]
            )
            host = await DurableAgentHost.create(
                session_id=session_id,
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="test",
                tools=[],
                approval_resume_handler=resume,
                approval_consumer_resolver=lambda _pending: operator,
            )
            try:
                self.assertEqual(handler_calls, 1)
                self.assertEqual(len(resume_fences), 1)
                self.assertGreater(resume_fences[0], 0)
                self.assertEqual(write_context_fences, resume_fences)
                self.assertEqual(
                    host.recovered_approval_resumes,
                    (pending.approval.approval_id,),
                )
                self.assertFalse(host.startup_recovery_report.blocked)
                self.assertEqual(provider.call_count, 1)
            finally:
                await host.close()

    async def test_later_unknown_write_prevents_approved_resume_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session_id = "blocked-approved-resume"
            resources, _approvals, operator, _pending = (
                await _approved_resume_fixture(directory, session_id)
            )
            await resources.operation_store.append_batch(
                session_id,
                "later-unknown",
                [
                    ("operation_started", {"configuration": {}, "tools": []}),
                    (
                        "tool_intent_recorded",
                        {
                            "toolCallId": "unknown-call",
                            "toolName": "write_tool",
                            "arguments": {},
                            "replayPolicy": "never",
                        },
                    ),
                    ("tool_dispatch_started", {"toolCallId": "unknown-call"}),
                    (
                        "write_prepared",
                        {
                            "writeId": "unknown-write",
                            "toolCallId": "unknown-call",
                            "toolName": "write_tool",
                            "arguments": {},
                            "actionHash": "e" * 64,
                            "idempotencyKeyHash": "f" * 64,
                        },
                    ),
                    ("write_approved", {"writeId": "unknown-write"}),
                    ("write_submitting", {"writeId": "unknown-write"}),
                    (
                        "write_outcome_unknown",
                        {"writeId": "unknown-write"},
                    ),
                ],
            )
            handler_calls = 0

            async def forbidden_resume(_payload):
                nonlocal handler_calls
                handler_calls += 1

            host = await DurableAgentHost.create(
                session_id=session_id,
                state_dir=directory,
                model=MODEL,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="test",
                tools=[],
                approval_resume_handler=forbidden_resume,
                approval_consumer_resolver=lambda _pending: operator,
            )
            try:
                self.assertEqual(handler_calls, 0)
                self.assertTrue(host.startup_recovery_report.hard_blocked)
                self.assertEqual(
                    host.startup_recovery_report.manual_intervention,
                    ("later-unknown",),
                )
            finally:
                await host.close()

    async def test_host_unlock_requires_durable_recovery_fact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider(
                [
                    assistant_message(
                        model=MODEL,
                        content=[{"type": "text", "text": "recovered"}],
                    ),
                    assistant_message(
                        model=MODEL,
                        content=[{"type": "text", "text": "new answer"}],
                    ),
                ]
            )
            host = await DurableAgentHost.create(
                session_id="repair-session",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="test",
                tools=[],
                auto_recover=False,
            )
            try:
                await host.operation_store.append_batch(
                    host.session_id,
                    "repair-operation",
                    [
                        ("operation_started", {"configuration": {}, "tools": []}),
                        (
                            "message_appended",
                            {
                                "message": {
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": "recover me"}
                                    ],
                                }
                            },
                        ),
                    ],
                )
                report = await host.recover_on_startup()
                self.assertTrue(report.blocked)

                # There is intentionally no "acknowledge and continue" switch.
                with self.assertRaises(StartupRecoveryBlockedError):
                    await host.resolve_startup_recovery(lambda _report: None)
                self.assertEqual(provider.call_count, 0)

                async def persist_missing_policy(_report):
                    await host.operation_store.append(
                        "model_policy_selected",
                        host.session_id,
                        "repair-operation",
                        {"policy": ModelRequestPolicy.no_tools().to_dict()},
                    )

                resolved = await host.resolve_startup_recovery(
                    persist_missing_policy
                )
                self.assertFalse(resolved.blocked)
                self.assertEqual(resolved.completed, ("repair-operation",))

                await host.prompt("a safe new prompt")
                self.assertEqual(provider.call_count, 2)
            finally:
                await host.close()


if __name__ == "__main__":
    unittest.main()
