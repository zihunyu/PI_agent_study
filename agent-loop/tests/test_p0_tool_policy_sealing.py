"""P0 regressions for non-downgradable Tool replay/security contracts."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

from pi_agent_loop import (
    AgentTool,
    AgentToolResult,
    CancellationToken,
    IntentPlanPolicy,
    Model,
    PlanStep,
    RecoveryAction,
    ToolDispatchContext,
    ToolRetryPolicy,
)
from pi_agent_loop.harness.plan_tool_runtime import (
    PlanToolDispatchError,
    ToolRuntimePlanStepExecutor,
)
from pi_agent_loop.harness.tool_runtime_adapter import RecoverableToolRuntime
from pi_agent_loop.messages import assistant_message
from pi_agent_loop.tool_contract import ToolSecurityContract
from pi_agent_loop.tool_runtime import (
    PreparedToolCall,
    ToolDispatchError,
    ToolDispatchRuntime,
)
from pi_agent_loop.types import AgentContext


MODEL = Model(id="sealed-tools", provider="test")


async def _discard(_event) -> None:
    return None


class ToolPolicySealingTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime拒绝同名safe实例覆盖never工具且零handler(self) -> None:
        calls: list[str] = []

        async def original(_id, _args, _token, _update):
            calls.append("never")
            return AgentToolResult(content=[])

        async def replacement(_id, _args, _token, _update):
            calls.append("safe")
            return AgentToolResult(content=[])

        never_tool = AgentTool(
            "same", "same", "same", original, replay_policy="never"
        )
        safe_tool = AgentTool(
            "same", "same", "same", replacement, replay_policy="safe"
        )
        runtime = ToolDispatchRuntime([never_tool])

        with self.assertRaisesRegex(ToolDispatchError, "同名不同实例"):
            runtime.register_tools([safe_tool])

        self.assertIs(runtime.tools["same"], never_tool)
        self.assertEqual(calls, [])

    async def test_forged_prepared_call不能绕过registry或执行safe_handler(self) -> None:
        calls = 0

        async def execute(_id, _args, _token, _update):
            nonlocal calls
            calls += 1
            return AgentToolResult(content=[])

        never_tool = AgentTool(
            "effect", "effect", "effect", execute, replay_policy="never"
        )
        safe_tool = AgentTool(
            "effect", "effect", "effect", execute, replay_policy="safe"
        )
        runtime = ToolDispatchRuntime([never_tool])
        call = {
            "type": "toolCall",
            "id": "forged",
            "name": "effect",
            "arguments": {},
        }

        with self.assertRaisesRegex(ToolDispatchError, "不属于当前 Runtime"):
            await runtime.dispatch_prepared(
                PreparedToolCall(call, safe_tool, {}),
                context=AgentContext("", [], [never_tool]),
                assistant_message=assistant_message(
                    model=MODEL,
                    content=[call],
                    stop_reason="toolUse",
                ),
                cancellation=CancellationToken(),
                emit=_discard,
            )

        self.assertEqual(calls, 0)

    async def test_plan绑定never但runtime为同名safe时fail_closed零handler(self) -> None:
        calls = 0

        async def bound(_id, _args, _context, _token, _update):
            raise AssertionError("绑定的 never Handler 也不应执行")

        async def replacement(_id, _args, _token, _update):
            nonlocal calls
            calls += 1
            return AgentToolResult(content=[])

        never_tool = AgentTool(
            "effect",
            "effect",
            "effect",
            None,
            replay_policy="never",
            execute_with_context=bound,
        )
        safe_tool = AgentTool(
            "effect", "effect", "effect", replacement, replay_policy="safe"
        )
        runtime = ToolDispatchRuntime([safe_tool])
        policy = IntentPlanPolicy("effect.run", replay_policy="never")
        adapter = ToolRuntimePlanStepExecutor(
            runtime_provider=lambda: runtime,
            tools=[never_tool],
            intent_tools={policy.intent: never_tool.name},
            model=MODEL,
            session_id="sealed-plan",
            dispatch_context_provider=ToolDispatchContext,
            policies={policy.intent: policy},
        )

        with self.assertRaisesRegex(PlanToolDispatchError, "受信注册不一致"):
            await adapter(
                PlanStep("step", policy.intent, replay_policy="never"),
                CancellationToken(),
                context=SimpleNamespace(resolved_arguments={}),
            )

        self.assertEqual(calls, 0)

    async def test_recovery_contract不匹配时不会safe_replay(self) -> None:
        calls = 0

        async def execute(_id, _args, _token, _update):
            nonlocal calls
            calls += 1
            return AgentToolResult(content=[])

        tool = AgentTool("read", "read", "read", execute, replay_policy="safe")
        recovery = RecoverableToolRuntime(model=MODEL, tools=[tool])

        with self.assertRaisesRegex(PermissionError, "Security Contract"):
            await recovery.execute(
                RecoveryAction(
                    kind="replay_safe_tool",
                    tool_call_id="read-1",
                    tool_name=tool.name,
                    arguments={},
                    expected_replay_policy="safe",
                    expected_tool_contract_digest="0" * 64,
                )
            )

        self.assertEqual(calls, 0)

    async def test注册后篡改retry合同也在handler前拒绝(self) -> None:
        calls = 0

        async def execute(_id, _args, _token, _update):
            nonlocal calls
            calls += 1
            return AgentToolResult(content=[])

        tool = AgentTool("read", "read", "read", execute, replay_policy="safe")
        runtime = ToolDispatchRuntime([tool])
        retry = ToolRetryPolicy(
            max_retries=1,
            retryable_codes=frozenset({"temporary"}),
            idempotent=True,
        )
        # frozen=True blocks ordinary mutation; object.__setattr__ simulates a
        # hostile same-process bypass and proves the Runtime seal still detects it.
        with self.assertRaises(FrozenInstanceError):
            tool.replay_policy = "never"  # type: ignore[misc]
        object.__setattr__(tool, "retry_policy", retry)
        call = {
            "type": "toolCall",
            "id": "mutated-retry",
            "name": tool.name,
            "arguments": {},
        }
        outcome = await runtime.dispatch(
            call,
            context=AgentContext("", [], [tool]),
            assistant_message=assistant_message(
                model=MODEL,
                content=[call],
                stop_reason="toolUse",
            ),
            cancellation=CancellationToken(),
            emit=_discard,
        )

        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.result.details["code"], "tool_policy_invalid")
        self.assertEqual(calls, 0)

    async def test注册后篡改replay_policy也在handler前拒绝(self) -> None:
        calls = 0

        async def execute(_id, _args, _token, _update):
            nonlocal calls
            calls += 1
            return AgentToolResult(content=[])

        tool = AgentTool("effect", "effect", "effect", execute)
        runtime = ToolDispatchRuntime([tool])
        object.__setattr__(tool, "replay_policy", "safe")
        call = {
            "type": "toolCall",
            "id": "mutated-replay",
            "name": tool.name,
            "arguments": {},
        }
        outcome = await runtime.dispatch(
            call,
            context=AgentContext("", [], [tool]),
            assistant_message=assistant_message(
                model=MODEL,
                content=[call],
                stop_reason="toolUse",
            ),
            cancellation=CancellationToken(),
            emit=_discard,
        )

        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.result.details["code"], "tool_policy_invalid")
        self.assertEqual(calls, 0)

    async def test注册后篡改handler也在调用前拒绝(self) -> None:
        calls: list[str] = []

        async def original(_id, _args, _token, _update):
            calls.append("original")
            return AgentToolResult(content=[])

        async def replacement(_id, _args, _token, _update):
            calls.append("replacement")
            return AgentToolResult(content=[])

        tool = AgentTool("read", "read", "read", original, replay_policy="safe")
        runtime = ToolDispatchRuntime([tool])
        object.__setattr__(tool, "execute", replacement)
        call = {
            "type": "toolCall",
            "id": "mutated-handler",
            "name": tool.name,
            "arguments": {},
        }
        outcome = await runtime.dispatch(
            call,
            context=AgentContext("", [], [tool]),
            assistant_message=assistant_message(
                model=MODEL,
                content=[call],
                stop_reason="toolUse",
            ),
            cancellation=CancellationToken(),
            emit=_discard,
        )

        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.result.details["code"], "tool_policy_invalid")
        self.assertEqual(calls, [])

    async def test旧safe_replay缺少digest时零handler(self) -> None:
        calls = 0

        async def execute(_id, _args, _token, _update):
            nonlocal calls
            calls += 1
            return AgentToolResult(content=[])

        tool = AgentTool("read", "read", "read", execute, replay_policy="safe")
        recovery = RecoverableToolRuntime(model=MODEL, tools=[tool])

        with self.assertRaisesRegex(PermissionError, "Security Contract"):
            await recovery.execute(
                RecoveryAction(
                    kind="replay_safe_tool",
                    tool_call_id="legacy-read",
                    tool_name=tool.name,
                    arguments={},
                    expected_replay_policy="safe",
                )
            )

        self.assertEqual(calls, 0)

    async def test_contract包含完整retry与timeout派发语义(self) -> None:
        async def execute(_id, _args, _token, _update):
            return AgentToolResult(content=[])

        retry = ToolRetryPolicy(
            max_retries=2,
            retryable_codes=frozenset({"a", "b"}),
            idempotent=True,
            initial_delay_seconds=0.1,
            max_delay_seconds=2,
            jitter_ratio=0.3,
            max_elapsed_seconds=9,
        )
        tool = AgentTool(
            "read",
            "read",
            "read",
            execute,
            replay_policy="safe",
            retry_policy=retry,
            timeout_seconds=4,
            lock_timeout_seconds=3,
            priority=7,
        )
        payload = ToolSecurityContract.capture(tool).to_dict()

        self.assertEqual(payload["timeoutSeconds"], 4)
        self.assertEqual(payload["lockTimeoutSeconds"], 3)
        self.assertEqual(payload["priority"], 7)
        self.assertEqual(payload["retryPolicy"]["maxRetries"], 2)
        self.assertEqual(payload["retryPolicy"]["retryableCodes"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
