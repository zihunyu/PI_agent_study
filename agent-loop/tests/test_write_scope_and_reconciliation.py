from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    ApprovalError,
    ApprovalService,
    DurableActionEnvelope,
    IdentityClaim,
    InMemoryOperationEventStore,
    OutcomeUnknownToolError,
    StaticIdentityVerifier,
    VerifiedIdentity,
    WriteOperationError,
    WriteOperationService,
)


class WriteScopeAndReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def identity(self, principal_id: str):
        secret = f"{principal_id}-secret"
        return await StaticIdentityVerifier(
            {principal_id: (secret, {"operator"})}
        ).verify(IdentityClaim(principal_id, secret))

    async def start(
        self,
        store: InMemoryOperationEventStore,
        session_id: str,
        operation_id: str,
    ) -> None:
        await store.append(
            "operation_started",
            session_id,
            operation_id,
            {"configuration": {}, "tools": []},
        )

    async def test_same_key_is_isolated_by_session_and_principal(self) -> None:
        store = InMemoryOperationEventStore()
        writes = WriteOperationService(store, ApprovalService(store))
        alice = await self.identity("alice")
        bob = await self.identity("bob")
        for session_id, operation_id in (
            ("session-a", "operation-a"),
            ("session-b", "operation-b"),
            ("session-c", "operation-c"),
        ):
            await self.start(store, session_id, operation_id)

        first = await writes.prepare(
            session_id="session-a",
            operation_id="operation-a",
            tool_name="charge",
            arguments={"amount": 10},
            idempotency_key="caller-key",
            requester=alice,
            requires_approval=False,
            tool_call_id="call-a",
        )
        other_session = await writes.prepare(
            session_id="session-b",
            operation_id="operation-b",
            tool_name="charge",
            arguments={"amount": 10},
            idempotency_key="caller-key",
            requester=alice,
            requires_approval=False,
            tool_call_id="call-b",
        )
        other_principal = await writes.prepare(
            session_id="session-c",
            operation_id="operation-c",
            tool_name="charge",
            arguments={"amount": 10},
            idempotency_key="caller-key",
            requester=bob,
            requires_approval=False,
        )

        self.assertEqual(len({first.write_id, other_session.write_id, other_principal.write_id}), 3)
        self.assertEqual(len({
            first.idempotency_key_hash,
            other_session.idempotency_key_hash,
            other_principal.idempotency_key_hash,
        }), 3)

    async def test_no_approval_write_still_rejects_forged_identity(self) -> None:
        store = InMemoryOperationEventStore()
        writes = WriteOperationService(store, ApprovalService(store))
        await self.start(store, "session-a", "operation-a")
        forged = VerifiedIdentity(
            principal_id="forged",
            roles=frozenset({"operator"}),
            issuer="caller",
            verification_id="self-asserted",
        )

        with self.assertRaises(ApprovalError) as prepare_error:
            await writes.prepare(
                session_id="session-a",
                operation_id="operation-a",
                tool_name="charge",
                arguments={"amount": 10},
                idempotency_key="forged-prepare",
                requester=forged,
                requires_approval=False,
            )
        self.assertEqual(
            prepare_error.exception.code,
            "identity_provenance_invalid",
        )

        operator = await self.identity("operator")
        prepared = await writes.prepare(
            session_id="session-a",
            operation_id="operation-a",
            tool_name="charge",
            arguments={"amount": 10},
            idempotency_key="valid-prepare",
            requester=operator,
            requires_approval=False,
        )
        effects: list[str] = []

        async def handler(_arguments, _key, _actor):
            effects.append("called")
            return {"ok": True}

        with self.assertRaises(ApprovalError) as execute_error:
            await writes.execute(
                prepared.write_id,
                actor=forged,
                idempotency_key="valid-prepare",
                handler=handler,
            )
        self.assertEqual(
            execute_error.exception.code,
            "identity_provenance_invalid",
        )
        self.assertEqual(effects, [])

    async def test_same_scope_same_action_reuses_write_but_changed_action_conflicts(self) -> None:
        store = InMemoryOperationEventStore()
        writes = WriteOperationService(store, ApprovalService(store))
        alice = await self.identity("alice")
        await self.start(store, "session-a", "operation-a")
        await self.start(store, "session-a", "operation-b")

        first = await writes.prepare(
            session_id="session-a",
            operation_id="operation-a",
            tool_name="charge",
            arguments={"amount": 10},
            idempotency_key="caller-key",
            requester=alice,
            requires_approval=False,
        )
        replay = await writes.prepare(
            session_id="session-a",
            operation_id="operation-b",
            tool_name="charge",
            arguments={"amount": 10},
            idempotency_key="caller-key",
            requester=alice,
            requires_approval=False,
        )
        self.assertEqual(replay.write_id, first.write_id)
        self.assertEqual(replay.action_hash, first.action_hash)

        with self.assertRaises(WriteOperationError) as caught:
            await writes.prepare(
                session_id="session-a",
                operation_id="operation-b",
                tool_name="charge",
                arguments={"amount": 20},
                idempotency_key="caller-key",
                requester=alice,
                requires_approval=False,
            )
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    async def test_action_hash隔离operation_tool_call和write_id(self) -> None:
        store = InMemoryOperationEventStore()
        writes = WriteOperationService(store, ApprovalService(store))
        alice = await self.identity("alice")
        await self.start(store, "session-a", "operation-a")
        await self.start(store, "session-a", "operation-b")

        first = await writes.prepare(
            session_id="session-a",
            operation_id="operation-a",
            tool_name="charge",
            arguments={"amount": 10},
            idempotency_key="key-a",
            requester=alice,
            requires_approval=True,
            tool_call_id="call-a",
        )
        second = await writes.prepare(
            session_id="session-a",
            operation_id="operation-b",
            tool_name="charge",
            arguments={"amount": 10},
            idempotency_key="key-b",
            requester=alice,
            requires_approval=True,
            tool_call_id="call-b",
        )

        expected_first = DurableActionEnvelope(
            operation_id="operation-a",
            tool_call_id="call-a",
            tool_name="charge",
            arguments={"amount": 10},
            write_id=first.write_id,
        )
        expected_second = DurableActionEnvelope(
            operation_id="operation-b",
            tool_call_id="call-b",
            tool_name="charge",
            arguments={"amount": 10},
            write_id=second.write_id,
        )
        self.assertEqual(first.action_hash, expected_first.action_hash)
        self.assertEqual(second.action_hash, expected_second.action_hash)
        self.assertNotEqual(first.action_hash, second.action_hash)

        events = await store.load(session_id="session-a")
        approval_hashes = {
            event.operation_id: event.data["actionHash"]
            for event in events
            if event.type == "approval_requested"
        }
        self.assertEqual(approval_hashes["operation-a"], first.action_hash)
        self.assertEqual(approval_hashes["operation-b"], second.action_hash)

    async def test_entity_version_and_preconditions_are_bound_to_write_action(self) -> None:
        store = InMemoryOperationEventStore()
        writes = WriteOperationService(store, ApprovalService(store))
        alice = await self.identity("alice")
        await self.start(store, "session-a", "operation-a")
        await self.start(store, "session-a", "operation-b")

        write = await writes.prepare(
            session_id="session-a",
            operation_id="operation-a",
            tool_name="refund",
            arguments={"orderId": "1001"},
            idempotency_key="refund-key",
            requester=alice,
            requires_approval=True,
            entity_id="order:1001",
            expected_entity_version=7,
            business_preconditions={"status": "paid"},
        )
        self.assertEqual(write.entity_id, "order:1001")
        self.assertEqual(write.expected_entity_version, 7)
        self.assertEqual(write.business_preconditions, {"status": "paid"})

        with self.assertRaises(WriteOperationError) as caught:
            await writes.prepare(
                session_id="session-a",
                operation_id="operation-b",
                tool_name="refund",
                arguments={"orderId": "1001"},
                idempotency_key="refund-key",
                requester=alice,
                requires_approval=True,
                entity_id="order:1001",
                expected_entity_version=8,
                business_preconditions={"status": "paid"},
            )
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    async def uncertain_write(self):
        store = InMemoryOperationEventStore()
        await self.start(store, "session-a", "operation-a")
        actor = await self.identity("alice")
        writes = WriteOperationService(store, ApprovalService(store))
        write = await writes.prepare(
            session_id="session-a",
            operation_id="operation-a",
            tool_name="charge",
            arguments={"amount": 10},
            idempotency_key="caller-key",
            requester=actor,
            requires_approval=False,
        )

        async def uncertain(*_args):
            raise OutcomeUnknownToolError(
                "unknown",
                operation_id="external-1",
                idempotency_key="caller-key",
                reconciliation_name="check_charge",
            )

        with self.assertRaises(OutcomeUnknownToolError):
            await writes.execute(
                write.write_id,
                actor=actor,
                idempotency_key="caller-key",
                handler=uncertain,
            )
        return store, writes, write

    async def test_nonterminal_reconciliation_statuses_remain_unknown(self) -> None:
        for status in ("pending", "unknown", "not_found", "vendor_waiting"):
            with self.subTest(status=status):
                store, writes, write = await self.uncertain_write()
                result = await writes.reconcile(
                    write.write_id,
                    lambda _record, value=status: _result({"status": value}),
                )
                self.assertEqual(result.state, "outcome_unknown")
                events = await store.load()
                self.assertTrue(
                    any(event.type == "write_reconcile_deferred" for event in events)
                )
                self.assertFalse(any(event.type == "write_failed" for event in events))

    async def test_only_explicit_failed_reconciliation_becomes_failed(self) -> None:
        _store, writes, write = await self.uncertain_write()
        result = await writes.reconcile(
            write.write_id,
            lambda _record: _result({"status": "failed", "reason": "declined"}),
        )
        self.assertEqual(result.state, "failed")

    async def test_reconciliation传递scope_token对且旧worker不能提交(self) -> None:
        store, writes, write = await self.uncertain_write()
        seen: list[tuple[str | None, int | None]] = []

        async def defer(record):
            seen.append((record.fencing_scope, record.fencing_token))
            return {"status": "unknown"}

        await writes.reconcile(write.write_id, defer)
        await writes.reconcile(write.write_id, defer)

        self.assertEqual(len(seen), 2)
        self.assertIsNotNone(seen[0][0])
        self.assertEqual(seen[0][0], seen[1][0])
        self.assertIsInstance(seen[0][1], int)
        self.assertGreater(seen[1][1], seen[0][1])  # type: ignore[operator]

        events = await store.load(operation_id="operation-a")
        fenced_events = [
            event
            for event in events
            if event.type
            in {
                "write_reconciling",
                "write_reconcile_reentered",
                "write_reconcile_deferred",
            }
        ]
        self.assertTrue(fenced_events)
        self.assertTrue(
            all(
                event.data.get("fencingScope") == seen[0][0]
                and isinstance(event.data.get("fencingToken"), int)
                for event in fenced_events
            )
        )

        current_scope, current_token = seen[1]
        assert current_scope is not None
        assert current_token is not None
        old_scope, old_token = seen[0]
        assert old_scope is not None
        assert old_token is not None
        await store.append(
            "write_reconciling",
            "session-a",
            "operation-a",
            {
                "writeId": write.write_id,
                "fencingScope": current_scope,
                "fencingToken": current_token + 1,
            },
        )
        await store.append(
            "write_succeeded",
            "session-a",
            "operation-a",
            {
                "writeId": write.write_id,
                "result": {"status": "succeeded"},
                "fencingScope": old_scope,
                "fencingToken": old_token,
            },
        )
        with self.assertRaises(WriteOperationError) as stale:
            await writes.get(write.write_id)
        self.assertEqual(stale.exception.code, "stale_write_fencing_pair")

    async def test_reconciliation多资源scope隔离即使token相同(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start(store, "session-a", "operation-a")
        actor = await self.identity("alice")
        writes = WriteOperationService(store, ApprovalService(store))
        uncertain_writes = []
        for index in (1, 2):
            write = await writes.prepare(
                session_id="session-a",
                operation_id="operation-a",
                tool_name="charge",
                arguments={"amount": index},
                idempotency_key=f"key-{index}",
                requester=actor,
                requires_approval=False,
            )

            async def uncertain(*_args, value=index):
                raise OutcomeUnknownToolError(
                    "unknown",
                    operation_id=f"external-{value}",
                    idempotency_key=f"key-{value}",
                    reconciliation_name="check_charge",
                )

            with self.assertRaises(OutcomeUnknownToolError):
                await writes.execute(
                    write.write_id,
                    actor=actor,
                    idempotency_key=f"key-{index}",
                    handler=uncertain,
                )
            uncertain_writes.append(write)

        pairs: list[tuple[str | None, int | None]] = []
        for write in uncertain_writes:
            await writes.reconcile(
                write.write_id,
                lambda record: _capture_unknown(record, pairs),
            )

        self.assertEqual(pairs[0][1], pairs[1][1])
        self.assertNotEqual(pairs[0][0], pairs[1][0])
        self.assertTrue(all(scope and token for scope, token in pairs))


async def _result(value):
    return value


async def _capture_unknown(record, pairs):
    pairs.append((record.fencing_scope, record.fencing_token))
    return {"status": "unknown"}


if __name__ == "__main__":
    unittest.main()
