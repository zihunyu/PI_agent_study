"""Plan steps must cross the same trusted boundary as ordinary Tool calls."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from types import SimpleNamespace

from pi_agent_loop import (
    AgentTool,
    AgentToolResult,
    CancellationToken,
    DurableAgentHost,
    IntentPlanPolicy,
    Model,
    PlanParameterContract,
    PlanStep,
    PlanToolDispatchError,
    ScriptedProvider,
    ToolDispatchContext,
    ToolDispatchRuntime,
    ToolRuntimePlanStepExecutor,
    VerifiedIdentity,
)


MODEL = Model(id="plan-tool-test", provider="test", api="scripted")


def identity() -> VerifiedIdentity:
    return VerifiedIdentity(
        principal_id="worker-principal",
        roles=frozenset({"reader"}),
        issuer="test",
        verification_id="verified-1",
    )


class PlanToolRuntimeBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_step复用schema身份授权与fencing边界(self) -> None:
        observed: dict = {}

        def validate(arguments):
            if set(arguments) != {"record_id"}:
                raise ValueError("record_id required")
            return {"record_id": str(arguments["record_id"]).upper()}

        async def execute_with_context(_call_id, args, context, _token, _update):
            observed.update(
                args=args,
                identity=context.identity,
                fence=context.fencing_token,
                scope=context.fencing_scope,
            )
            return AgentToolResult(
                content=[{"type": "text", "text": "ok"}],
                details={"record": {"id": args["record_id"]}},
            )

        tool = AgentTool(
            name="read_record",
            label="read",
            description="read",
            execute=None,
            execute_with_context=execute_with_context,
            validate_args=validate,
            replay_policy="safe",
        )
        runtime = ToolDispatchRuntime(
            [tool],
            authorization=lambda _tool, _args, context: (
                context.identity is not None
                and "reader" in context.identity.roles
            ),
            require_identity=True,
        )
        adapter = ToolRuntimePlanStepExecutor(
            runtime_provider=lambda: runtime,
            tools=[tool],
            intent_tools={"record.read": "read_record"},
            model=MODEL,
            session_id="session-a",
            dispatch_context_provider=lambda: ToolDispatchContext(
                identity=identity(), tenant_id="tenant-a"
            ),
        )
        step = PlanStep(
            "read",
            "record.read",
            arguments={"record_id": "r-1"},
        )
        context = SimpleNamespace(
            resolved_arguments={"record_id": "r-1"},
            approval_receipt=None,
            plan_id="plan-a",
            fencing_token=7,
        )

        result = await adapter(
            step,
            CancellationToken(),
            fencing_token=7,
            context=context,
        )

        self.assertEqual(result["details"], {"record": {"id": "R-1"}})
        self.assertEqual(observed["args"], {"record_id": "R-1"})
        self.assertEqual(observed["identity"].principal_id, "worker-principal")
        self.assertEqual(observed["fence"], 7)
        self.assertEqual(
            observed["scope"],
            "plan_execution:session-a:plan:plan-a",
        )

    async def test_runtime授权拒绝时plan_handler零调用(self) -> None:
        calls = 0

        async def execute(_call_id, _args, _token, _update):
            nonlocal calls
            calls += 1
            return AgentToolResult(content=[])

        tool = AgentTool(
            "read", "read", "read", execute, replay_policy="safe"
        )
        runtime = ToolDispatchRuntime(
            [tool],
            authorization=lambda _tool, _args, _context: False,
            require_identity=True,
        )
        adapter = ToolRuntimePlanStepExecutor(
            runtime_provider=lambda: runtime,
            tools=[tool],
            intent_tools={"record.read": "read"},
            model=MODEL,
            session_id="session-a",
            dispatch_context_provider=lambda: ToolDispatchContext(
                identity=identity(), tenant_id="tenant-a"
            ),
        )

        with self.assertRaisesRegex(PlanToolDispatchError, "未通过执行授权"):
            await adapter(
                PlanStep("read", "record.read"),
                CancellationToken(),
                fencing_token=1,
            )
        self.assertEqual(calls, 0)

    async def test_host拒绝危险plan绕过tool_runtime(self) -> None:
        async def unsafe_callback(_step, _token):
            return "should not run"

        policy = IntentPlanPolicy(
            "orders.create",
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("approver",),
            parameter_contract=PlanParameterContract(allow_empty=True),
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "plan_tool_bindings"):
                await DurableAgentHost.create(
                    session_id="unsafe-plan-host",
                    state_dir=directory,
                    model=MODEL,
                    stream_fn=ScriptedProvider([]).stream,
                    system_prompt="test",
                    tools=[],
                    auto_recover=False,
                    plan_policies={policy.intent: policy},
                    plan_step_executor=unsafe_callback,
                )

    async def test_tool_timeout也会阻止plan声称成功(self) -> None:
        async def slow(_call_id, _args, _token, _update):
            await asyncio.sleep(10)
            return AgentToolResult(content=[])

        tool = AgentTool(
            "slow",
            "slow",
            "slow",
            slow,
            timeout_seconds=0.01,
            replay_policy="safe",
        )
        runtime = ToolDispatchRuntime([tool])
        adapter = ToolRuntimePlanStepExecutor(
            runtime_provider=lambda: runtime,
            tools=[tool],
            intent_tools={"slow.read": "slow"},
            model=MODEL,
            session_id="session-a",
            dispatch_context_provider=ToolDispatchContext,
        )
        with self.assertRaises(PlanToolDispatchError):
            await adapter(
                PlanStep("slow", "slow.read"),
                CancellationToken(),
                fencing_token=1,
            )


if __name__ == "__main__":
    unittest.main()
