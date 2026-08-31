"""P1 regressions for approval fail-closed and uncertain write outcomes."""

from __future__ import annotations

import unittest

from pi_agent_loop.approval.state_machine import ApprovalService
from pi_agent_loop.retry.errors import DefinitelyNotCommittedToolError
from pi_agent_loop.security import (
    IdentityClaim,
    StaticIdentityVerifier,
    VerifiedIdentity,
)
from pi_agent_loop.session.operation_store import InMemoryOperationEventStore
from pi_agent_loop.tool_runtime import ToolDispatchRuntime
from pi_agent_loop.types import AgentTool, AgentToolResult, ToolDispatchContext
from pi_agent_loop.writes.state_machine import (
    WriteOperationService,
    WriteOutcomeUnknownError,
)


def _call(call_id: str, name: str) -> dict:
    return {
        "type": "toolCall",
        "id": call_id,
        "name": name,
        "arguments": {},
    }


def _identity() -> VerifiedIdentity:
    return VerifiedIdentity(
        principal_id="operator-1",
        roles=frozenset({"operator"}),
        issuer="test",
        verification_id="verified-operator-1",
    )


async def _trusted_identity() -> VerifiedIdentity:
    verifier = StaticIdentityVerifier(
        {"operator-1": ("test-credential", {"operator"})}
    )
    return await verifier.verify(IdentityClaim("operator-1", "test-credential"))


class ApprovalAndOutcomeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_approval_without_trusted_authorizer_never_executes(
        self,
    ) -> None:
        calls = 0

        async def execute(_call_id, _args, _cancellation, _update):
            nonlocal calls
            calls += 1
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="approved_write",
            label="approved write",
            description="requires approval",
            execute=execute,
            requires_approval=True,
            replay_policy="never",
        )
        runtime = ToolDispatchRuntime([tool])

        missing = await runtime.dispatch(_call("missing", tool.name))
        forged = await runtime.dispatch(
            _call("forged", tool.name),
            dispatch_context=ToolDispatchContext(
                identity=_identity(),
                approval={"approved": True},
            ),
        )

        self.assertEqual(calls, 0)
        self.assertTrue(missing.is_error)
        self.assertTrue(forged.is_error)
        self.assertEqual(missing.result.details["code"], "permission_denied")
        self.assertEqual(forged.result.details["code"], "permission_denied")

    async def test_requires_approval_accepts_verified_authorization_path(self) -> None:
        calls = 0
        receipt = {"receiptId": "receipt-1", "actionHash": "trusted-hash"}

        async def execute(_call_id, _args, _cancellation, _update):
            nonlocal calls
            calls += 1
            return AgentToolResult(content=[])

        async def authorize(tool, arguments, context):
            return (
                tool.name == "approved_write"
                and arguments == {}
                and context.identity == _identity()
                and context.approval == receipt
            )

        tool = AgentTool(
            name="approved_write",
            label="approved write",
            description="requires approval",
            execute=execute,
            requires_approval=True,
            replay_policy="never",
        )
        runtime = ToolDispatchRuntime([tool], authorization=authorize)

        outcome = await runtime.dispatch(
            _call("authorized", tool.name),
            dispatch_context=ToolDispatchContext(
                identity=_identity(),
                approval=receipt,
            ),
        )

        self.assertFalse(outcome.is_error)
        self.assertEqual(calls, 1)

    async def test_never_tool_plain_handler_exception_is_outcome_unknown(self) -> None:
        async def execute(_call_id, _args, _cancellation, _update):
            raise RuntimeError("Authorization: Bearer internal-secret")

        tool = AgentTool(
            name="charge_once",
            label="charge",
            description="external charge",
            execute=execute,
            replay_policy="never",
        )
        outcome = await ToolDispatchRuntime([tool]).dispatch(
            _call("charge", tool.name)
        )

        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.result.details["code"], "outcome_unknown")
        self.assertEqual(
            outcome.result.details["reason"], "tool_exception_after_dispatch"
        )
        self.assertNotIn("internal-secret", repr(outcome.result))

    async def test_typed_not_committed_error_remains_definite_failure(self) -> None:
        async def execute(_call_id, _args, _cancellation, _update):
            raise DefinitelyNotCommittedToolError(
                "internal validation details",
                code="business_precondition_failed",
                public_message="业务前置条件不满足",
            )

        tool = AgentTool(
            name="validated_write",
            label="validated write",
            description="validated external write",
            execute=execute,
            replay_policy="never",
        )
        outcome = await ToolDispatchRuntime([tool]).dispatch(
            _call("validated", tool.name)
        )

        self.assertTrue(outcome.is_error)
        self.assertEqual(
            outcome.result.details["code"], "business_precondition_failed"
        )
        self.assertTrue(outcome.result.details["definitelyNotCommitted"])
        self.assertNotEqual(outcome.result.details["code"], "outcome_unknown")
        self.assertNotIn("internal validation details", repr(outcome.result))

    async def _prepared_write(self, operation_id: str):
        store = InMemoryOperationEventStore()
        await store.append(
            "operation_started",
            "session-1",
            operation_id,
            {"configuration": {}, "tools": []},
        )
        identity = await _trusted_identity()
        service = WriteOperationService(store, ApprovalService(store))
        write = await service.prepare(
            session_id="session-1",
            operation_id=operation_id,
            tool_name="external_write",
            arguments={"value": 1},
            idempotency_key=f"key-{operation_id}",
            requester=identity,
            requires_approval=False,
        )
        return store, service, write, identity

    async def test_write_plain_handler_exception_persists_outcome_unknown(self) -> None:
        store, service, write, identity = await self._prepared_write("ordinary-error")

        async def handler(*_args):
            raise RuntimeError("Authorization: Bearer internal-secret")

        with self.assertRaises(WriteOutcomeUnknownError) as raised:
            await service.execute(
                write.write_id,
                actor=identity,
                idempotency_key="key-ordinary-error",
                handler=handler,
            )

        self.assertEqual(raised.exception.code, "write_handler_outcome_unknown")
        self.assertEqual((await service.get(write.write_id)).state, "outcome_unknown")
        events = await store.load()
        self.assertTrue(any(event.type == "write_outcome_unknown" for event in events))
        self.assertFalse(any(event.type == "write_failed" for event in events))
        self.assertNotIn("internal-secret", repr(events))

    async def test_write_typed_not_committed_error_persists_failed(self) -> None:
        store, service, write, identity = await self._prepared_write("definite-error")

        async def handler(*_args):
            raise DefinitelyNotCommittedToolError(
                "internal precondition details",
                code="entity_version_conflict",
                public_message="实体版本已经变化",
            )

        with self.assertRaises(DefinitelyNotCommittedToolError):
            await service.execute(
                write.write_id,
                actor=identity,
                idempotency_key="key-definite-error",
                handler=handler,
            )

        self.assertEqual((await service.get(write.write_id)).state, "failed")
        events = await store.load()
        failed = next(event for event in events if event.type == "write_failed")
        self.assertEqual(failed.data["errorCode"], "entity_version_conflict")
        self.assertTrue(failed.data["definitelyNotCommitted"])
        self.assertFalse(any(event.type == "write_outcome_unknown" for event in events))
        self.assertNotIn("internal precondition details", repr(events))


if __name__ == "__main__":
    unittest.main()
