from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    AgentTool,
    AgentToolResult,
    ApprovalError,
    CapabilityRegistry,
    DurableAgentHost,
    DurableHostClosedError,
    IdentityClaim,
    Model,
    RequestDecision,
    ScriptedProvider,
    SessionWriterLeaseLostError,
    StaticIdentityVerifier,
    replay_operation,
)
from pi_agent_loop.harness.approval import (  # noqa: E402
    DurableApprovalAction,
    DurableApprovalBatch,
    DurableApprovalBatchItem,
    DurableApprovalExecution,
    _approval_batch_digest,
)
from pi_agent_loop.durable_action import DurableActionEnvelope  # noqa: E402


class FixedRouter:
    def route(self, _text):
        return RequestDecision(
            status="in_scope_approval_required",
            reason="测试批量审批",
            message="等待审批",
            selected_tools=("write_a",),
            requires_approval=True,
        )


class AdapterOutcomeUnknown(RuntimeError):
    outcome_unknown = True
    code = "outcome_unknown"


def make_tool(name: str, *, reject: bool = False) -> AgentTool:
    def validate(arguments):
        if reject:
            raise ValueError(f"{name} 参数无效")
        if set(arguments) != {"value"} or not isinstance(arguments["value"], int):
            raise ValueError("value 必须是整数")
        return {"value": arguments["value"]}

    async def never_direct(_id, _args, _token, _update):
        return AgentToolResult(content=[])

    return AgentTool(
        name=name,
        label=name,
        description=name,
        parameters={"type": "object"},
        validate_args=validate,
        execute=never_direct,
        execution_mode="exclusive",
        replay_policy="never",
    )


class ApprovalBatchWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def identities(self):
        verifier = StaticIdentityVerifier(
            {
                "requester": ("requester-secret", {"operator"}),
                "approver": ("approver-secret", {"approver"}),
            }
        )
        return (
            await verifier.verify(IdentityClaim("requester", "requester-secret")),
            await verifier.verify(IdentityClaim("approver", "approver-secret")),
        )

    async def host(self, directory: str, tools: list[AgentTool]):
        capabilities = CapabilityRegistry()
        for tool in tools:
            capabilities.register(
                tool,
                capabilities={f"writes.{tool.name}"},
                domain="test",
                operation="write",
                requires_approval=True,
            )
        return await DurableAgentHost.create(
            session_id="batch-session",
            state_dir=directory,
            model=Model(id="batch-model", provider="fake", api="fake"),
            stream_fn=ScriptedProvider([]).stream,
            system_prompt="批量审批测试",
            tools=tools,
            router=FixedRouter(),
            capabilities=capabilities,
        )

    async def test_batch和single_helper拒绝duck身份(self) -> None:
        class DuckIdentity:
            principal_id = "forged-operator"
            roles = frozenset({"operator", "approver"})
            issuer = "forged"
            verification_id = "forged-verification"

        with tempfile.TemporaryDirectory() as directory:
            host = await self.host(directory, [make_tool("write_a")])
            operation_id = await host.operation_recorder.start_operation()
            before = await host.operation_store.load(operation_id=operation_id)
            action = DurableApprovalAction(
                "write_a",
                {"value": 1},
                "forged-batch-key",
                "伪造批量动作",
            )

            with self.assertRaises(ApprovalError) as batch_error:
                await host.request_approval_batch(
                    text="伪造批量审批",
                    actions=(action,),
                    requester=DuckIdentity(),  # type: ignore[arg-type]
                )
            self.assertEqual(
                batch_error.exception.code,
                "verified_identity_required",
            )
            after_batch = await host.operation_store.load(
                operation_id=operation_id
            )
            self.assertEqual(after_batch, before)

            routed = SimpleNamespace(
                decision=RequestDecision(
                    status="in_scope_approval_required",
                    reason="需要审批",
                    message="等待审批",
                    selected_tools=("write_a",),
                    extracted_fields={"value": 1},
                    requires_approval=True,
                )
            )
            with self.assertRaises(ApprovalError) as single_error:
                await host.approval_workflow.prepare(
                    text="伪造单项审批",
                    routed_result=routed,
                    requester=DuckIdentity(),  # type: ignore[arg-type]
                    approval_role="approver",
                    idempotency_key="forged-single-key",
                )
            self.assertEqual(
                single_error.exception.code,
                "verified_identity_required",
            )
            events = await host.operation_store.load(operation_id=operation_id)
            self.assertFalse(
                any(event.type == "approval_requested" for event in events)
            )
            await host.close()

    async def test_任一action预校验失败时不会留下半批事件(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = await self.host(
                directory,
                [make_tool("write_a"), make_tool("write_b", reject=True)],
            )
            requester, _approver = await self.identities()
            operation_id = await host.operation_recorder.start_operation()
            before = await host.operation_store.load(operation_id=operation_id)

            with self.assertRaisesRegex(ValueError, "write_b 参数无效"):
                await host.request_approval_batch(
                    text="执行两个写动作",
                    requester=requester,
                    actions=(
                        DurableApprovalAction(
                            "write_a", {"value": 1}, "key-a", "动作 A"
                        ),
                        DurableApprovalAction(
                            "write_b", {"value": 2}, "key-b", "动作 B"
                        ),
                    ),
                )

            after = await host.operation_store.load(operation_id=operation_id)
            self.assertEqual(after, before)
            await host.close()

    async def test_缺少任一确认时整批零副作用_全部确认后按顺序闭合(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = await self.host(
                directory,
                [make_tool("write_a"), make_tool("write_b")],
            )
            requester, approver = await self.identities()
            operation_id = await host.operation_recorder.start_operation()
            batch = await host.request_approval_batch(
                text="执行两个写动作",
                requester=requester,
                actions=(
                    DurableApprovalAction(
                        "write_a", {"value": 1}, "key-a", "动作 A"
                    ),
                    DurableApprovalAction(
                        "write_b", {"value": 2}, "key-b", "动作 B"
                    ),
                ),
            )
            effects: list[tuple[str, int]] = []

            async def handler_a(arguments, _key, _actor, *, fenced_claim):
                self.assertIsNotNone(fenced_claim)
                effects.append(("write_a", arguments["value"]))
                return {"tool": "write_a", "value": arguments["value"]}

            async def handler_b(arguments, _key, _actor, *, fenced_claim):
                self.assertIsNotNone(fenced_claim)
                effects.append(("write_b", arguments["value"]))
                return {"tool": "write_b", "value": arguments["value"]}

            partial = await host.approve_approval_batch(
                batch,
                (
                    DurableApprovalExecution(
                        batch.items[0].approval_id,
                        approver,
                        requester,
                        "key-a",
                        handler_a,
                    ),
                ),
            )
            self.assertEqual(effects, [])
            self.assertEqual(
                partial.pending_approval_ids,
                (batch.items[1].approval_id,),
            )

            result = await host.approve_approval_batch(
                batch,
                (
                    DurableApprovalExecution(
                        batch.items[0].approval_id,
                        approver,
                        requester,
                        "key-a",
                        handler_a,
                    ),
                    DurableApprovalExecution(
                        batch.items[1].approval_id,
                        approver,
                        requester,
                        "key-b",
                        handler_b,
                    ),
                ),
            )

            self.assertEqual(effects, [("write_a", 1), ("write_b", 2)])
            self.assertEqual(
                [item.status for item in result.items],
                ["succeeded", "succeeded"],
            )
            operation = replay_operation(
                await host.operation_store.load(operation_id=operation_id)
            )
            self.assertEqual(operation.phase, "completed")
            self.assertEqual(
                [message["toolCallId"] for message in operation.messages if message["role"] == "toolResult"],
                [item.envelope.tool_call_id for item in batch.items],
            )
            self.assertEqual(
                [write.action_hash for write in operation.writes.values()],
                [item.envelope.action_hash for item in batch.items],
            )
            await host.close()

    async def test_批量审批execution支持context_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = await self.host(directory, [make_tool("write_a")])
            requester, approver = await self.identities()
            await host.operation_recorder.start_operation()
            batch = await host.request_approval_batch(
                text="CAS 变更资源",
                requester=requester,
                actions=(
                    DurableApprovalAction(
                        "write_a",
                        {"value": 3},
                        "context-key-a",
                        "动作 A",
                        entity_id="resource-a",
                        expected_entity_version=4,
                        business_preconditions={"status": "active"},
                    ),
                ),
            )
            seen = []

            async def context_handler(context):
                seen.append(context)
                return {
                    "tool": context.tool_name,
                    "value": context.arguments["value"],
                    "version": context.expected_entity_version + 1,
                }

            result = await host.approve_approval_batch(
                batch,
                (
                    DurableApprovalExecution(
                        batch.items[0].approval_id,
                        approver,
                        requester,
                        "context-key-a",
                        context_handler=context_handler,
                    ),
                ),
            )

            self.assertEqual([item.status for item in result.items], ["succeeded"])
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0].entity_id, "resource-a")
            self.assertEqual(seen[0].expected_entity_version, 4)
            self.assertEqual(seen[0].business_preconditions, {"status": "active"})
            await host.close()

    async def test_传入批次必须与持久canonical批次精确一致(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = await self.host(
                directory,
                [make_tool("write_a"), make_tool("write_b")],
            )
            requester, approver = await self.identities()
            operation_id = await host.operation_recorder.start_operation()
            batch = await host.request_approval_batch(
                text="执行两个写动作",
                requester=requester,
                actions=(
                    DurableApprovalAction(
                        "write_a", {"value": 1}, "key-a", "动作 A"
                    ),
                    DurableApprovalAction(
                        "write_b", {"value": 2}, "key-b", "动作 B"
                    ),
                ),
            )
            self.assertIsNotNone(batch.batch_id)
            self.assertIsNotNone(batch.batch_hash)
            before = await host.operation_store.load(operation_id=operation_id)
            effects: list[int] = []

            async def handler(arguments, _key, _actor, *, fenced_claim):
                self.assertIsNotNone(fenced_claim)
                effects.append(arguments["value"])
                return {"value": arguments["value"]}

            first = batch.items[0]
            second = batch.items[1]
            tampered_envelope = DurableActionEnvelope(
                operation_id=operation_id,
                tool_call_id=first.envelope.tool_call_id,
                tool_name=first.envelope.tool_name,
                arguments={"value": 999},
                write_id=first.envelope.write_id,
            )
            added_envelope = DurableActionEnvelope(
                operation_id=operation_id,
                tool_call_id="unpersisted-tool-call",
                tool_name="write_a",
                arguments={"value": 3},
                write_id="unpersisted-write",
            )
            assert batch.batch_id is not None

            def forged(
                items: tuple[DurableApprovalBatchItem, ...],
            ) -> DurableApprovalBatch:
                # An attacker can recompute an unkeyed hash. Store-derived
                # canonical equality, not possession of the digest, is the
                # security boundary under test.
                return DurableApprovalBatch(
                    operation_id,
                    items,
                    batch_id=batch.batch_id,
                    batch_hash=_approval_batch_digest(
                        operation_id,
                        batch.batch_id,
                        items,
                    ),
                )

            forged_batches = (
                forged((first,)),
                forged((second, first)),
                forged(
                    (
                        DurableApprovalBatchItem(
                            first.approval_id,
                            tampered_envelope,
                        ),
                        second,
                    ),
                ),
                forged(
                    (
                        first,
                        second,
                        DurableApprovalBatchItem(
                            "unpersisted-approval",
                            added_envelope,
                        ),
                    ),
                ),
            )
            key_by_approval = {
                first.approval_id: "key-a",
                second.approval_id: "key-b",
                "unpersisted-approval": "key-c",
            }
            for forged in forged_batches:
                executions = tuple(
                    DurableApprovalExecution(
                        item.approval_id,
                        approver,
                        requester,
                        key_by_approval[item.approval_id],
                        handler,
                    )
                    for item in forged.items
                )
                with self.assertRaisesRegex(RuntimeError, "canonical batch"):
                    await host.approve_approval_batch(forged, executions)
                self.assertEqual(effects, [])
                self.assertEqual(
                    await host.operation_store.load(operation_id=operation_id),
                    before,
                )
            await host.close()

    async def test_host关闭后workflow兼容入口也不能绕过生命周期(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = await self.host(directory, [make_tool("write_a")])
            requester, approver = await self.identities()
            await host.operation_recorder.start_operation()
            batch = await host.request_approval_batch(
                text="执行写动作",
                requester=requester,
                actions=(
                    DurableApprovalAction(
                        "write_a", {"value": 1}, "key-a", "动作 A"
                    ),
                ),
            )
            effects: list[int] = []

            async def handler(arguments, _key, _actor, *, fenced_claim):
                self.assertIsNotNone(fenced_claim)
                effects.append(arguments["value"])
                return {"value": arguments["value"]}

            await host.close()
            with self.assertRaises(DurableHostClosedError):
                await host.approval_workflow.approve_many(
                    batch,
                    (
                        DurableApprovalExecution(
                            batch.items[0].approval_id,
                            approver,
                            requester,
                            "key-a",
                            handler,
                        ),
                    ),
                )
            self.assertEqual(effects, [])

    async def test_lease丢失后批量批准零副作用且零新增事件(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = await self.host(directory, [make_tool("write_a")])
            requester, approver = await self.identities()
            operation_id = await host.operation_recorder.start_operation()
            batch = await host.request_approval_batch(
                text="执行写动作",
                requester=requester,
                actions=(
                    DurableApprovalAction(
                        "write_a", {"value": 1}, "key-a", "动作 A"
                    ),
                ),
            )
            before = await host.operation_store.load(operation_id=operation_id)
            effects: list[int] = []

            async def handler(arguments, _key, _actor, *, fenced_claim):
                self.assertIsNotNone(fenced_claim)
                effects.append(arguments["value"])
                return {"value": arguments["value"]}

            lease = host.session_writer_lease
            assert lease is not None
            await host.operation_store.release_claim(
                lease.claim_type,
                lease.session_id,
                lease.owner_token,
            )
            other_owner = "batch-test-other-owner"
            self.assertTrue(
                await host.operation_store.try_acquire_claim(
                    lease.claim_type,
                    lease.session_id,
                    other_owner,
                    lease_seconds=30,
                )
            )
            try:
                with self.assertRaises(SessionWriterLeaseLostError):
                    await host.approve_approval_batch(
                        batch,
                        (
                            DurableApprovalExecution(
                                batch.items[0].approval_id,
                                approver,
                                requester,
                                "key-a",
                                handler,
                            ),
                        ),
                    )
                after = await host.operation_store.load(
                    operation_id=operation_id
                )
                self.assertEqual(effects, [])
                self.assertEqual(after, before)
            finally:
                await host.operation_store.release_claim(
                    lease.claim_type,
                    lease.session_id,
                    other_owner,
                )
                await host.close()

    async def test_批量批准在执行期间由host生命周期跟踪(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = await self.host(directory, [make_tool("write_a")])
            requester, approver = await self.identities()
            await host.operation_recorder.start_operation()
            batch = await host.request_approval_batch(
                text="执行写动作",
                requester=requester,
                actions=(
                    DurableApprovalAction(
                        "write_a", {"value": 1}, "key-a", "动作 A"
                    ),
                ),
            )
            entered = asyncio.Event()
            release = asyncio.Event()

            async def handler(arguments, _key, _actor, *, fenced_claim):
                self.assertIsNotNone(fenced_claim)
                entered.set()
                await release.wait()
                return {"value": arguments["value"]}

            task = asyncio.create_task(
                host.approve_approval_batch(
                    batch,
                    (
                        DurableApprovalExecution(
                            batch.items[0].approval_id,
                            approver,
                            requester,
                            "key-a",
                            handler,
                        ),
                    ),
                )
            )
            await entered.wait()
            self.assertEqual(host.lifecycle.active_operation_count, 1)
            release.set()
            result = await task
            self.assertEqual(result.items[0].status, "succeeded")
            self.assertEqual(host.lifecycle.active_operation_count, 0)
            await host.close()

    async def test_批量中一项结果不确定时operation保持可恢复(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host = await self.host(
                directory,
                [make_tool("write_a"), make_tool("write_b")],
            )
            requester, approver = await self.identities()
            operation_id = await host.operation_recorder.start_operation()
            batch = await host.request_approval_batch(
                text="执行两个写动作",
                requester=requester,
                actions=(
                    DurableApprovalAction(
                        "write_a", {"value": 1}, "key-a", "动作 A"
                    ),
                    DurableApprovalAction(
                        "write_b", {"value": 2}, "key-b", "动作 B"
                    ),
                ),
            )

            async def unknown(*_args, fenced_claim):
                self.assertIsNotNone(fenced_claim)
                raise AdapterOutcomeUnknown("外部结果待核对")

            async def succeeded(arguments, _key, _actor, *, fenced_claim):
                self.assertIsNotNone(fenced_claim)
                return {"value": arguments["value"]}

            result = await host.approve_approval_batch(
                batch,
                (
                    DurableApprovalExecution(
                        batch.items[0].approval_id,
                        approver,
                        requester,
                        "key-a",
                        unknown,
                    ),
                    DurableApprovalExecution(
                        batch.items[1].approval_id,
                        approver,
                        requester,
                        "key-b",
                        succeeded,
                    ),
                ),
            )

            self.assertEqual(
                [item.status for item in result.items],
                ["outcome_unknown", "succeeded"],
            )
            events = await host.operation_store.load(operation_id=operation_id)
            self.assertFalse(any(event.type == "operation_finished" for event in events))
            operation = replay_operation(events)
            self.assertEqual(
                operation.writes[batch.items[0].envelope.write_id].state,
                "outcome_unknown",
            )
            unknown_result = next(
                message
                for message in operation.messages
                if message.get("toolCallId")
                == batch.items[0].envelope.tool_call_id
            )
            self.assertTrue(unknown_result["details"]["outcomeUnknown"])
            await host.close()


if __name__ == "__main__":
    unittest.main()


