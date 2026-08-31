"""Cross-cutting safety regressions for the reusable Agent scaffold."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pi_agent_loop import (
    DurableAgentHost,
    DurableAgentWorkspace,
    DurableHostResources,
    InMemoryRuntimeEventStore,
    Model,
    RuntimeEvent,
    RuntimeRecoveryClaimError,
    RuntimeRecoveryManager,
    RuntimeStoreConflictError,
    ScriptedProvider,
    SQLiteOperationEventStore,
    SQLiteRuntimeEventStore,
    WorkspaceCatalogError,
)
from pi_agent_loop.session.operation_events import OperationEvent
from pi_agent_loop.session.operation_store import (
    OperationStoreFencedClaimLostError,
    fenced_claim_resource_id,
)
from pi_agent_loop.session.operation_state import (
    OperationLogInvariantError,
    replay_operation,
)
from pi_agent_loop.session.store import RuntimeStoreFencedClaimLostError
from pi_agent_loop.runtime.tracker import RuntimeStateTracker
from pi_agent_loop.harness.session_runtime import (
    SessionWriterLease,
    SessionWriterLeaseLostError,
)


MODEL = Model(id="framework-safety", provider="scripted", api="scripted")


def _operation_events(
    specs: list[tuple[str, dict]],
) -> list[OperationEvent]:
    return [
        OperationEvent(
            type=event_type,
            session_id="session",
            operation_id="operation",
            sequence=index,
            data=data,
        )
        for index, (event_type, data) in enumerate(specs)
    ]


def _submitting_write_specs() -> list[tuple[str, dict]]:
    return [
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
    ]


class FrameworkSafetyRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_store_rejects_stale_cas(self) -> None:
        store = InMemoryRuntimeEventStore()
        first = RuntimeEvent("run_started", "run-1", 0, data={})
        await store.append_cas(first, expected_last_sequence=-1)
        stale = RuntimeEvent("run_interrupted", "run-1", 1, data={})
        with self.assertRaises(RuntimeStoreConflictError):
            await store.append_cas(stale, expected_last_sequence=-1)

    async def test_runtime_recovery_uses_cross_process_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            runtime = SQLiteRuntimeEventStore(path, session_id="session")
            await runtime.append_cas(
                RuntimeEvent("run_started", "run-1", 0, data={}),
                expected_last_sequence=-1,
            )
            claims = SQLiteOperationEventStore(path)
            self.assertTrue(
                await claims.try_acquire_claim(
                    "runtime_recovery",
                    "session",
                    "other-worker",
                    lease_seconds=30,
                )
            )
            manager = RuntimeRecoveryManager(
                runtime,
                claim_store=claims,
                claim_resource_id="session",
            )
            with self.assertRaises(RuntimeRecoveryClaimError):
                await manager.recover()
            await claims.release_claim(
                "runtime_recovery",
                "session",
                "other-worker",
            )
            recovered = await manager.recover()
            self.assertEqual(recovered.phase, "suspended")

    async def test_runtime_recovery_takeover_rejects_old_atomic_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            claims = SQLiteOperationEventStore(path)
            successor = None

            class TakeoverRuntimeStore(SQLiteRuntimeEventStore):
                async def append_cas_if_fenced_claim(
                    inner_self,
                    event,
                    lease,
                    *,
                    renew_lease_seconds,
                    expected_last_sequence,
                ):
                    nonlocal successor
                    self.assertTrue(
                        await claims.renew_fenced_claim(
                            lease,
                            lease_seconds=renew_lease_seconds,
                        )
                    )
                    await claims.release_fenced_claim(lease)
                    successor = await claims.acquire_fenced_claim(
                        lease.claim_type,
                        lease.resource_id,
                        "successor",
                        lease_seconds=30,
                    )
                    self.assertIsNotNone(successor)
                    return await super().append_cas_if_fenced_claim(
                        event,
                        lease,
                        renew_lease_seconds=renew_lease_seconds,
                        expected_last_sequence=expected_last_sequence,
                    )

            runtime = TakeoverRuntimeStore(path, session_id="session")
            await runtime.append_cas(
                RuntimeEvent("run_started", "run-1", 0, data={}),
                expected_last_sequence=-1,
            )
            manager = RuntimeRecoveryManager(
                runtime,
                claim_store=claims,
                claim_resource_id="session",
            )
            with self.assertRaises(RuntimeRecoveryClaimError):
                await manager.recover()
            self.assertEqual(
                [event.type for event in await runtime.load()],
                ["run_started"],
            )
            assert successor is not None
            self.assertTrue(await claims.verify_fenced_claim(successor))

    async def test_fenced_operation_append_binds_exact_entity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteOperationEventStore(
                Path(directory) / "state.sqlite3"
            )
            await store.append("operation_started", "session", "operation")
            scope = fenced_claim_resource_id(
                "approval_resume",
                session_id="session",
                operation_id="operation",
                entity_id="approval-a",
            )
            old = await store.acquire_fenced_claim(
                "approval_resume",
                scope,
                "old-worker",
                lease_seconds=30,
            )
            assert old is not None
            with self.assertRaises(OperationStoreFencedClaimLostError):
                await store.append_batch_if_fenced_claim(
                    "session",
                    "operation",
                    [("approval_resume_started", {"approvalId": "approval-b"})],
                    old,
                    renew_lease_seconds=30,
                    expected_claim_entity_id="approval-b",
                )
            self.assertEqual(len(await store.load()), 1)

            self.assertTrue(
                await store.renew_fenced_claim(old, lease_seconds=30)
            )
            await store.release_fenced_claim(old)
            current = await store.acquire_fenced_claim(
                "approval_resume",
                scope,
                "new-worker",
                lease_seconds=30,
            )
            assert current is not None
            with self.assertRaises(OperationStoreFencedClaimLostError):
                await store.append_batch_if_fenced_claim(
                    "session",
                    "operation",
                    [("approval_resume_started", {"approvalId": "approval-a"})],
                    old,
                    renew_lease_seconds=30,
                    expected_claim_entity_id="approval-a",
                )
            self.assertEqual(len(await store.load()), 1)

    async def test_runtime_tracker_old_session_writer_cannot_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            claims = SQLiteOperationEventStore(path)
            runtime = SQLiteRuntimeEventStore(path, session_id="session")
            old = await claims.acquire_fenced_claim(
                "conversation_session_writer",
                "session",
                "old-worker",
                lease_seconds=30,
            )
            assert old is not None
            tracker = await RuntimeStateTracker.create(
                runtime,
                fenced_claim=old,
                fenced_claim_lease_seconds=30,
            )
            await tracker.start_run()
            self.assertTrue(
                await claims.renew_fenced_claim(old, lease_seconds=30)
            )
            await claims.release_fenced_claim(old)
            successor = await claims.acquire_fenced_claim(
                "conversation_session_writer",
                "session",
                "new-worker",
                lease_seconds=30,
            )
            assert successor is not None
            with self.assertRaises(RuntimeStoreFencedClaimLostError):
                await tracker.record_external("turn_started", {"turn": 1})
            self.assertEqual(
                [event.type for event in await runtime.load()],
                ["run_started"],
            )

    async def test_catalog_activity_atomic_append_rejects_takeover_after_verify(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "project"
            project_path.mkdir()
            workspace = DurableAgentWorkspace.open(root / "state")
            project = await workspace.create_project(project_path, title="project")
            session = await workspace.create_session(
                project.project_id,
                title="session",
                session_id="session",
            )
            host = await workspace.open_session(
                session.session_id,
                model=MODEL,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="test",
                tools=[],
                auto_recover=False,
            )
            successor = None
            try:
                old = host.session_writer_lease.claim_lease
                await host.session_writer_lease.verify_owned()
                before = await workspace.catalog.journal.load_events(
                    workspace.principal,
                )
                await host.operation_store.release_fenced_claim(old)
                successor = await host.operation_store.acquire_fenced_claim(
                    "conversation_session_writer",
                    session.session_id,
                    "successor",
                    lease_seconds=30,
                )
                assert successor is not None

                with self.assertRaises(WorkspaceCatalogError):
                    await host._record_session_activity()

                after = await workspace.catalog.journal.load_events(
                    workspace.principal,
                )
                self.assertEqual(len(after), len(before))
            finally:
                if successor is not None:
                    await host.operation_store.release_fenced_claim(successor)
                await host.close()

    async def test_durable_host_cannot_disable_single_writer_protection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "单写者保护"):
                await DurableAgentHost.create(
                    session_id="unsafe",
                    state_dir=directory,
                    model=MODEL,
                    stream_fn=ScriptedProvider([]).stream,
                    system_prompt="test",
                    tools=[],
                    exclusive_session=False,
                )

    async def test_session_writer_lease_revalidates_against_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteOperationEventStore(Path(directory) / "state.sqlite3")
            lease = await SessionWriterLease.acquire(
                store,
                "session",
                lease_seconds=30,
            )
            lost = []
            lease.set_loss_callback(lambda: lost.append(True))
            await store.release_claim(
                lease.claim_type,
                lease.session_id,
                lease.owner_token,
            )
            self.assertTrue(
                await store.try_acquire_claim(
                    lease.claim_type,
                    lease.session_id,
                    "other-owner",
                    lease_seconds=30,
                )
            )
            try:
                with self.assertRaises(SessionWriterLeaseLostError):
                    await lease.verify_owned()
                self.assertEqual(lost, [True])
            finally:
                await lease.close()
                await store.release_claim(
                    lease.claim_type,
                    lease.session_id,
                    "other-owner",
                )

    async def test_custom_resource_factory_is_the_harness_extension_point(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            requests = []

            def resource_factory(request):
                requests.append(request)
                return DurableHostResources.create(
                    request.state_dir,
                    session_id=request.session_id,
                    tenant_id=request.tenant_id,
                )

            host = await DurableAgentHost.create(
                session_id="external-store-adapter",
                state_dir=directory,
                model=MODEL,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="test",
                tools=[],
                resource_factory=resource_factory,
            )
            try:
                self.assertEqual(requests[0].session_id, "external-store-adapter")
            finally:
                await host.close()

    def test_outcome_unknown_cannot_be_hidden_by_failed_operation(self) -> None:
        specs = [
            *_submitting_write_specs(),
            ("write_outcome_unknown", {"writeId": "write-1"}),
            (
                "tool_outcome_unknown",
                {
                    "toolCallId": "call-1",
                    "result": {"isError": True, "details": {"outcomeUnknown": True}},
                },
            ),
            ("operation_finished", {"outcome": "failed"}),
        ]
        with self.assertRaisesRegex(OperationLogInvariantError, "未决持久事实"):
            replay_operation(_operation_events(specs))

    def test_write_success_cannot_pair_with_error_tool_result(self) -> None:
        specs = [
            *_submitting_write_specs(),
            ("write_succeeded", {"writeId": "write-1", "result": {"ok": True}}),
            (
                "tool_completed",
                {
                    "toolCallId": "call-1",
                    "result": {"isError": True, "details": {"code": "failed"}},
                },
            ),
        ]
        with self.assertRaisesRegex(OperationLogInvariantError, "矛盾"):
            replay_operation(_operation_events(specs))

    def test_write_failure_cannot_pair_with_outcome_unknown_tool_result(self) -> None:
        specs = [
            *_submitting_write_specs(),
            (
                "write_failed",
                {
                    "writeId": "write-1",
                    "result": {"code": "business_rejected"},
                },
            ),
            (
                "tool_outcome_unknown",
                {
                    "toolCallId": "call-1",
                    "result": {
                        "isError": True,
                        "details": {"outcomeUnknown": True},
                    },
                },
            ),
        ]
        with self.assertRaisesRegex(OperationLogInvariantError, "成功/未知"):
            replay_operation(_operation_events(specs))

    def test_tool_outcome_unknown_event_requires_matching_result_marker(self) -> None:
        specs = [
            *_submitting_write_specs(),
            (
                "tool_outcome_unknown",
                {
                    "toolCallId": "call-1",
                    "result": {"isError": True, "details": {}},
                },
            ),
        ]
        with self.assertRaisesRegex(OperationLogInvariantError, "结果未知"):
            replay_operation(_operation_events(specs))

    def test_core_import_does_not_eagerly_load_optional_calculator_modules(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (
            "import sys;"
            f"sys.path.insert(0, {str(root / 'src')!r});"
            "sys.modules['pi_agent_loop.tools.add']=None;"
            "sys.modules['pi_agent_loop.tools.multiply']=None;"
            "sys.modules['pi_agent_loop.tools.divide']=None;"
            "import pi_agent_loop;"
            "assert pi_agent_loop.Agent.__name__ == 'Agent';"
            "assert 'create_add_tool' not in pi_agent_loop.__dict__;"
            "assert 'create_multiply_tool' not in pi_agent_loop.__dict__;"
            "assert 'create_divide_tool' not in pi_agent_loop.__dict__"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_public_tool_exports_match_lazy_loading_and_star_import(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (
            "import sys;"
            f"sys.path.insert(0, {str(root / 'src')!r});"
            "import pi_agent_loop;"
            "import pi_agent_loop.tools as tools;"
            "tool_create_names={name for name in tools._LAZY_EXPORTS "
            "if name.startswith('create_')};"
            "tool_create_names.update({'create_calculator_registry',"
            "'create_calculator_tools'});"
            "assert tool_create_names <= set(tools.__all__);"
            "root_create_names={name for name in "
            "pi_agent_loop._OPTIONAL_TOOL_EXPORTS if name.startswith('create_')};"
            "assert root_create_names <= set(pi_agent_loop.__all__);"
            "tool_namespace={};"
            "exec('from pi_agent_loop.tools import *', tool_namespace);"
            "assert not (set(tools.__all__) - set(tool_namespace));"
            "root_namespace={};"
            "exec('from pi_agent_loop import *', root_namespace);"
            "assert not (set(pi_agent_loop.__all__) - set(root_namespace));"
            "assert root_namespace['create_add_tool'] is "
            "tool_namespace['create_add_tool'];"
            "assert root_namespace['create_calculator_registry'] is "
            "tool_namespace['create_calculator_registry'];"
            "assert root_namespace['create_calculator_tools'] is "
            "tool_namespace['create_calculator_tools'];"
            "assert root_namespace['create_divide_tool'] is "
            "tool_namespace['create_divide_tool'];"
            "assert root_namespace['create_multiply_tool'] is "
            "tool_namespace['create_multiply_tool']"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
