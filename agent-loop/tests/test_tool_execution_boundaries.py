"""Tool 执行边界的副作用、参数快照、批次 ID 与身份授权回归测试。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest

from pi_agent_loop.cancellation import CancellationToken
from pi_agent_loop.agent import Agent
from pi_agent_loop.messages import assistant_message
from pi_agent_loop.harness import DurableAgentHost
from pi_agent_loop.security import VerifiedIdentity
from pi_agent_loop.testing import ScriptedProvider
from pi_agent_loop.tool_runtime import ToolDispatchRuntime
from pi_agent_loop.types import (
    AgentContext,
    AgentTool,
    AgentToolResult,
    AfterToolCallResult,
    Model,
    ToolDispatchContext,
)


def call(call_id: str, name: str, arguments: dict | None = None) -> dict:
    return {
        "type": "toolCall",
        "id": call_id,
        "name": name,
        "arguments": arguments or {},
    }


def identity(*roles: str) -> VerifiedIdentity:
    return VerifiedIdentity(
        principal_id="principal-1",
        roles=frozenset(roles),
        issuer="test",
        verification_id="verification-1",
    )


class ToolExecutionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_Tool必须至少提供一种execute_handler(self) -> None:
        with self.assertRaisesRegex(ValueError, "execute 或 execute_with_context"):
            AgentTool(
                name="missing_handler",
                label="missing",
                description="missing",
                execute=None,
            )

    async def test_never工具进入execute后超时必须是outcome_unknown(self) -> None:
        effects: list[str] = []

        async def execute(_id, _args, _token, _update):
            effects.append("external-effect-may-have-committed")
            await asyncio.sleep(10)
            return AgentToolResult(content=[])

        runtime = ToolDispatchRuntime(
            [
                AgentTool(
                    name="charge_once",
                    label="charge",
                    description="charge",
                    execute=execute,
                    timeout_seconds=0.02,
                    replay_policy="never",
                )
            ]
        )

        outcome = await runtime.dispatch(call("timeout", "charge_once"))

        self.assertEqual(effects, ["external-effect-may-have-committed"])
        self.assertEqual(outcome.result.details["code"], "outcome_unknown")
        self.assertTrue(outcome.result.details["outcomeUnknown"])
        self.assertEqual(
            outcome.result.details["reason"],
            "tool_timeout_after_dispatch",
        )
        self.assertFalse(outcome.result.details["retryable"])

    async def test_never工具进入execute后取消必须是outcome_unknown(self) -> None:
        started = asyncio.Event()
        token = CancellationToken()

        async def execute(_id, _args, _token, _update):
            started.set()
            await asyncio.sleep(10)
            return AgentToolResult(content=[])

        runtime = ToolDispatchRuntime(
            [
                AgentTool(
                    name="refund_once",
                    label="refund",
                    description="refund",
                    execute=execute,
                    replay_policy="never",
                )
            ]
        )
        task = asyncio.create_task(
            runtime.dispatch(
                call("cancel", "refund_once"),
                cancellation=token,
            )
        )
        await asyncio.wait_for(started.wait(), 1)
        token.cancel("user cancelled")

        outcome = await asyncio.wait_for(task, 1)

        self.assertEqual(outcome.result.details["code"], "outcome_unknown")
        self.assertTrue(outcome.result.details["outcomeUnknown"])
        self.assertEqual(
            outcome.result.details["reason"],
            "tool_cancelled_after_dispatch",
        )

    async def test_业务异常显式标记outcome_unknown必须被Runtime识别(self) -> None:
        class BusinessOutcomeUnknownError(RuntimeError):
            outcome_unknown = True

        async def execute(_id, _args, _token, _update):
            raise BusinessOutcomeUnknownError("upstream disconnected after submit")

        runtime = ToolDispatchRuntime(
            [
                AgentTool(
                    name="submit",
                    label="submit",
                    description="submit",
                    execute=execute,
                    replay_policy="never",
                )
            ]
        )

        outcome = await runtime.dispatch(call("unknown", "submit"))

        self.assertEqual(outcome.result.details["code"], "outcome_unknown")
        self.assertTrue(outcome.result.details["outcomeUnknown"])
        self.assertEqual(outcome.result.details["reason"], "business_exception")

    async def test_after_hook不能把outcome_unknown降级成成功(self) -> None:
        class BusinessOutcomeUnknownError(RuntimeError):
            outcome_unknown = True

        async def execute(_id, _args, _token, _update):
            raise BusinessOutcomeUnknownError("unknown")

        async def unsafe_after(_context, _token):
            return AfterToolCallResult(
                details={"code": "success"},
                is_error=False,
            )

        tool = AgentTool(
            name="submit",
            label="submit",
            description="submit",
            execute=execute,
            replay_policy="never",
        )
        runtime = ToolDispatchRuntime([tool], after_tool_call=unsafe_after)

        outcome = await runtime.dispatch(call("unknown-hook", "submit"))

        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.result.details["code"], "outcome_unknown")
        self.assertTrue(outcome.result.details["outcomeUnknown"])

    async def test_after_hook异常不能把已提交副作用改记为失败(self) -> None:
        effects: list[str] = []

        async def execute(_id, _args, _token, _update):
            effects.append("committed")
            return AgentToolResult(
                content=[{"type": "text", "text": "RAW_OUTPUT"}],
                usage={"writes": 1},
            )

        async def broken_after(_context, _token):
            raise RuntimeError("post-processing failed")

        tool = AgentTool(
            name="commit_once",
            label="commit",
            description="side effect",
            execute=execute,
            replay_policy="never",
        )
        runtime = ToolDispatchRuntime([tool], after_tool_call=broken_after)

        outcome = await runtime.dispatch(call("commit-1", "commit_once", {}))

        self.assertEqual(effects, ["committed"])
        self.assertFalse(outcome.is_error)
        self.assertEqual(
            outcome.result.details["code"],
            "tool_output_unavailable_after_commit",
        )
        self.assertTrue(outcome.result.details["effectCommitted"])
        self.assertTrue(outcome.result.terminate)
        self.assertNotIn("RAW_OUTPUT", repr(outcome.result))

    async def test_dispatch_many重复ID整批拒绝且零副作用(self) -> None:
        effects: list[str] = []

        async def execute(call_id, _args, _token, _update):
            effects.append(call_id)
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="write",
            label="write",
            description="write",
            execute=execute,
        )
        runtime = ToolDispatchRuntime([tool])
        calls = [call("duplicate", "write"), call("duplicate", "write")]
        assistant = {"role": "assistant", "content": calls}
        context = AgentContext(system_prompt="", messages=[], tools=[tool])

        batch = await runtime.dispatch_many(
            calls,
            context=context,
            assistant_message=assistant,
            cancellation=CancellationToken(),
            emit=lambda _event: None,
        )

        self.assertEqual(effects, [])
        self.assertEqual(len(batch.outcomes), 2)
        self.assertTrue(
            all(
                item.result.details["code"] == "tool_call_batch_rejected"
                for item in batch.outcomes
            )
        )
        self.assertEqual(
            len({message["toolCallId"] for message in batch.messages}),
            2,
        )

    async def test_dispatch_many历史ID整批拒绝且零副作用(self) -> None:
        effects: list[str] = []

        async def execute(call_id, _args, _token, _update):
            effects.append(call_id)
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="read",
            label="read",
            description="read",
            execute=execute,
            replay_policy="safe",
        )
        old = {"role": "assistant", "content": [call("already-used", "read")]}
        current_calls = [call("already-used", "read"), call("new", "read")]
        current = {"role": "assistant", "content": current_calls}
        context = AgentContext(system_prompt="", messages=[old], tools=[tool])
        runtime = ToolDispatchRuntime([tool])

        batch = await runtime.dispatch_many(
            current_calls,
            context=context,
            assistant_message=current,
            cancellation=CancellationToken(),
            emit=lambda _event: None,
        )

        self.assertEqual(effects, [])
        self.assertEqual(
            [item.result.details["code"] for item in batch.outcomes],
            ["tool_call_batch_rejected", "tool_call_batch_rejected"],
        )

    async def test_Runtime已注册但当前轮未暴露的工具整批零执行(self) -> None:
        effects: list[str] = []
        authorization_calls = 0
        before_calls = 0

        async def execute(_id, _args, _token, _update):
            effects.append("write")
            return AgentToolResult(content=[])

        async def authorize(_tool, _args, _context):
            nonlocal authorization_calls
            authorization_calls += 1
            return True

        async def before(_context, _token):
            nonlocal before_calls
            before_calls += 1

        write = AgentTool(
            name="hidden_write",
            label="write",
            description="write",
            execute=execute,
        )
        runtime = ToolDispatchRuntime(
            [write],
            authorization=authorize,
            before_tool_call=before,
        )
        calls = [call("hidden", "hidden_write")]

        batch = await runtime.dispatch_many(
            calls,
            context=AgentContext(system_prompt="", messages=[], tools=[]),
            assistant_message={"role": "assistant", "content": calls},
            cancellation=CancellationToken(),
            emit=lambda _event: None,
            dispatch_context=ToolDispatchContext(identity=identity("buyer")),
        )

        self.assertEqual(effects, [])
        self.assertEqual(authorization_calls, 0)
        self.assertEqual(before_calls, 0)
        self.assertEqual(
            batch.outcomes[0].result.details["code"],
            "tool_call_batch_rejected",
        )

    async def test_后一个参数无效时前一个也不会进入授权或before_hook(self) -> None:
        prepare_calls = 0
        validate_calls = 0
        authorization_calls = 0
        before_calls = 0
        effects: list[str] = []

        def prepare(arguments):
            nonlocal prepare_calls
            prepare_calls += 1
            return arguments

        def validate(arguments):
            nonlocal validate_calls
            validate_calls += 1
            if not isinstance(arguments.get("quantity"), int):
                raise ValueError("quantity must be int")
            return arguments

        async def authorize(_tool, _args, _context):
            nonlocal authorization_calls
            authorization_calls += 1
            return True

        async def before(_context, _token):
            nonlocal before_calls
            before_calls += 1

        async def execute(call_id, _args, _token, _update):
            effects.append(call_id)
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="write",
            label="write",
            description="write",
            execute=execute,
            prepare_arguments=prepare,
            validate_args=validate,
        )
        runtime = ToolDispatchRuntime(
            [tool],
            authorization=authorize,
            before_tool_call=before,
        )
        calls = [
            call("valid", "write", {"quantity": 1}),
            call("invalid", "write", {"quantity": "bad"}),
        ]
        context = AgentContext(system_prompt="", messages=[], tools=[tool])

        batch = await runtime.dispatch_many(
            calls,
            context=context,
            assistant_message={"role": "assistant", "content": calls},
            cancellation=CancellationToken(),
            emit=lambda _event: None,
            dispatch_context=ToolDispatchContext(identity=identity("buyer")),
        )

        self.assertEqual(prepare_calls, 2)
        self.assertEqual(validate_calls, 2)
        self.assertEqual(authorization_calls, 0)
        self.assertEqual(before_calls, 0)
        self.assertEqual(effects, [])
        self.assertTrue(
            all(
                outcome.result.details["code"] == "tool_call_batch_rejected"
                for outcome in batch.outcomes
            )
        )

    async def test_canonical参数不受授权和Hook篡改(self) -> None:
        authorization_seen: list[dict] = []
        hook_seen: list[dict] = []
        executed: list[dict] = []
        events: list[dict] = []

        def prepare(arguments):
            arguments["prepared"] = True
            return arguments

        def validate(arguments):
            arguments["quantity"] = int(arguments["quantity"])
            return arguments

        async def authorize(_tool, arguments, dispatch_context):
            authorization_seen.append(arguments.copy())
            self.assertEqual(dispatch_context.identity, identity("buyer"))
            arguments["quantity"] = 999
            return True

        async def before(context, _token):
            hook_seen.append(context.args.copy())
            context.args["quantity"] = 888
            context.tool_call["arguments"]["quantity"] = 777

        async def after(context, _token):
            context.args["quantity"] = 666

        async def execute(_id, arguments, _token, _update):
            executed.append(arguments.copy())
            arguments["quantity"] = 555
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="create_order",
            label="create",
            description="create",
            execute=execute,
            prepare_arguments=prepare,
            validate_args=validate,
        )
        runtime = ToolDispatchRuntime(
            [tool],
            authorization=authorize,
            before_tool_call=before,
            after_tool_call=after,
        )
        original = call("canonical", "create_order", {"quantity": "2"})
        trusted = ToolDispatchContext(identity=identity("buyer"), tenant_id="acme")

        outcome = await runtime.dispatch(
            original,
            dispatch_context=trusted,
            emit=events.append,
        )

        canonical = {"quantity": 2, "prepared": True}
        self.assertEqual(original["arguments"], {"quantity": "2"})
        self.assertEqual(authorization_seen, [canonical])
        self.assertEqual(hook_seen, [canonical])
        self.assertEqual(executed, [canonical])
        self.assertEqual(outcome.args, canonical)
        start = next(event for event in events if event["type"] == "tool_execution_start")
        self.assertEqual(start["args"], canonical)

    async def test_普通工具配置授权后缺身份必须fail_closed(self) -> None:
        effects: list[str] = []
        authorization_calls: list[str] = []

        async def execute(_id, _args, _token, _update):
            effects.append("executed")
            return AgentToolResult(content=[])

        async def authorize(_tool, _args, context):
            authorization_calls.append(context.identity.principal_id)
            return "buyer" in context.identity.roles

        tool = AgentTool(
            name="ordinary_read",
            label="read",
            description="read",
            execute=execute,
            replay_policy="safe",
        )
        runtime = ToolDispatchRuntime([tool], authorization=authorize)

        missing = await runtime.dispatch(call("missing", "ordinary_read"))
        denied = await runtime.dispatch(
            call("denied", "ordinary_read"),
            dispatch_context=ToolDispatchContext(identity=identity("guest")),
        )
        allowed = await runtime.dispatch(
            call("allowed", "ordinary_read"),
            dispatch_context=ToolDispatchContext(identity=identity("buyer")),
        )

        self.assertEqual(missing.result.details["code"], "permission_denied")
        self.assertEqual(denied.result.details["code"], "permission_denied")
        self.assertFalse(allowed.is_error)
        self.assertEqual(authorization_calls, ["principal-1", "principal-1"])
        self.assertEqual(effects, ["executed"])

    async def test_require_identity无需授权回调也会fail_closed(self) -> None:
        effects: list[str] = []

        async def execute(_id, _args, _token, _update):
            effects.append("executed")
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="protected",
            label="protected",
            description="protected",
            execute=execute,
            replay_policy="safe",
        )
        runtime = ToolDispatchRuntime([tool], require_identity=True)

        missing = await runtime.dispatch(call("missing", "protected"))
        allowed = await runtime.dispatch(
            call("allowed", "protected"),
            dispatch_context=ToolDispatchContext(identity=identity("member")),
        )

        self.assertEqual(missing.result.details["code"], "permission_denied")
        self.assertFalse(allowed.is_error)
        self.assertEqual(effects, ["executed"])

    async def test_contextual_Tool直接取得可信身份且模型参数不能伪造(self) -> None:
        received: list[tuple[str, str | None, str, dict]] = []
        approval = {"approval_id": "trusted-approval"}

        async def execute_with_context(
            _id,
            arguments,
            dispatch_context,
            _token,
            _update,
        ):
            received.append(
                (
                    dispatch_context.identity.principal_id,
                    dispatch_context.tenant_id,
                    arguments["principal_id"],
                    dict(dispatch_context.approval),
                )
            )
            # Handler 只能修改自己的受信上下文副本。
            dispatch_context.approval["approval_id"] = "mutated-locally"
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="contextual_write",
            label="write",
            description="write",
            execute=None,
            execute_with_context=execute_with_context,
        )
        runtime = ToolDispatchRuntime([tool], require_identity=True)
        forged_arguments = {
            "principal_id": "attacker",
            "tenant_id": "evil-tenant",
            "approval": {"approval_id": "forged"},
        }

        outcome = await runtime.dispatch(
            call("contextual", "contextual_write", forged_arguments),
            dispatch_context=ToolDispatchContext(
                identity=identity("buyer"),
                tenant_id="trusted-tenant",
                approval=approval,
            ),
        )

        self.assertFalse(outcome.is_error)
        self.assertEqual(
            received,
            [
                (
                    "principal-1",
                    "trusted-tenant",
                    "attacker",
                    {"approval_id": "trusted-approval"},
                )
            ],
        )
        self.assertEqual(approval, {"approval_id": "trusted-approval"})

    async def test_Agent把可信身份正式传到普通Tool授权边界(self) -> None:
        effects: list[str] = []
        seen_principals: list[str] = []
        model = Model(id="identity", provider="test", api="test")

        async def execute(_id, _args, _token, _update):
            effects.append("executed")
            return AgentToolResult(content=[])

        async def authorize(_tool, _args, context):
            seen_principals.append(context.identity.principal_id)
            return True

        tool = AgentTool(
            name="read_profile",
            label="profile",
            description="profile",
            execute=execute,
            replay_policy="safe",
        )
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=model,
                    stop_reason="toolUse",
                    content=[call("agent-call", "read_profile")],
                ),
                assistant_message(model=model),
            ]
        )
        agent = Agent(
            model=model,
            stream_fn=provider.stream,
            tools=[tool],
            tool_identity=identity("buyer"),
            tool_authorization=authorize,
        )

        await agent.prompt("read")

        self.assertEqual(effects, ["executed"])
        self.assertEqual(seen_principals, ["principal-1"])

    async def test_Agent成功批次prepare和validate各只执行一次(self) -> None:
        prepare_calls = 0
        validate_calls = 0
        model = Model(id="canonical-once", provider="test", api="test")

        def prepare(arguments):
            nonlocal prepare_calls
            prepare_calls += 1
            arguments["prepared"] = True
            return arguments

        def validate(arguments):
            nonlocal validate_calls
            validate_calls += 1
            return arguments

        async def execute(_id, arguments, _token, _update):
            self.assertEqual(arguments, {"prepared": True})
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="canonical_once",
            label="once",
            description="once",
            execute=execute,
            prepare_arguments=prepare,
            validate_args=validate,
            replay_policy="safe",
        )
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=model,
                    stop_reason="toolUse",
                    content=[call("once", "canonical_once")],
                ),
                assistant_message(model=model),
            ]
        )
        agent = Agent(model=model, stream_fn=provider.stream, tools=[tool])

        await agent.prompt("once")

        self.assertEqual(prepare_calls, 1)
        self.assertEqual(validate_calls, 1)

    async def test_DurableHost使用prompt_requester作为可信Tool身份(self) -> None:
        effects: list[str] = []
        principals: list[str] = []
        model = Model(id="host-identity", provider="test", api="test")

        async def execute(_id, _args, _token, _update):
            effects.append("executed")
            return AgentToolResult(content=[])

        async def authorize(_tool, _args, context):
            principals.append(context.identity.principal_id)
            return True

        tool = AgentTool(
            name="host_read",
            label="read",
            description="read",
            execute=execute,
            replay_policy="safe",
        )
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=model,
                    stop_reason="toolUse",
                    content=[call("host-call", "host_read")],
                ),
                assistant_message(model=model),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="tool-identity",
                state_dir=directory,
                model=model,
                stream_fn=provider.stream,
                system_prompt="identity",
                tools=[tool],
                tool_authorization=authorize,
                auto_recover=False,
            )
            try:
                await host.prompt("read", requester=identity("buyer"))
            finally:
                await host.close()

        self.assertEqual(effects, ["executed"])
        self.assertEqual(principals, ["principal-1"])

    async def test_DurableHost把Session代际传入普通Tool执行上下文(self) -> None:
        observed: list[tuple[int | None, str | None]] = []
        model = Model(id="host-fencing", provider="test", api="test")

        async def execute_with_context(
            _call_id,
            _args,
            context,
            _token,
            _update,
        ):
            observed.append((context.fencing_token, context.fencing_scope))
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="fenced_read",
            label="fenced read",
            description="fenced read",
            execute=None,
            execute_with_context=execute_with_context,
            replay_policy="safe",
        )
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=model,
                    stop_reason="toolUse",
                    content=[call("fenced-call", "fenced_read")],
                ),
                assistant_message(model=model),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="tool-fencing",
                state_dir=directory,
                model=model,
                stream_fn=provider.stream,
                system_prompt="fencing",
                tools=[tool],
                auto_recover=False,
            )
            try:
                expected = host.session_writer_lease.fencing_token
                await host.prompt("read")
            finally:
                await host.close()

        self.assertEqual(
            observed,
            [(expected, "conversation_session_writer:tool-fencing")],
        )


if __name__ == "__main__":
    unittest.main()
