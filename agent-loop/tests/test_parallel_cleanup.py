"""并行 Listener/调度异常下的嵌套 Task 和 CancellationToken 清理测试。"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    Agent,
    AgentTool,
    AgentToolResult,
    Model,
    ScriptedProvider,
    assistant_message,
)


class ParallelCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="cleanup", provider="fake", api="fake")

    def provider(self, *names: str) -> ScriptedProvider:
        return ScriptedProvider([
            assistant_message(
                model=self.model,
                stop_reason="toolUse",
                content=[
                    {
                        "type": "toolCall",
                        "id": f"{name}-id",
                        "name": name,
                        "arguments": {},
                    }
                    for name in names
                ],
            ),
            assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "不应到达"}],
            ),
        ])

    @staticmethod
    def tool(name: str, execute) -> AgentTool:
        return AgentTool(
            name=name,
            label=name,
            description=name,
            execute=execute,
            execution_mode="parallel",
        )

    async def test_tool_end_listener_异常不会取消已提交的兄弟工具(self) -> None:
        slow_started = asyncio.Event()
        slow_completed = asyncio.Event()

        async def fast(_id, _args, _token, _update):
            await slow_started.wait()
            return AgentToolResult(content=[], details={})

        async def slow(_id, _args, _token, _update):
            slow_started.set()
            await asyncio.sleep(0.01)
            slow_completed.set()
            return AgentToolResult(content=[], details={})

        agent = Agent(
            model=self.model,
            stream_fn=self.provider("fast", "slow").stream,
            tools=[self.tool("fast", fast), self.tool("slow", slow)],
        )
        raised = False

        def listener(event, _token):
            nonlocal raised
            if (
                event["type"] == "tool_execution_end"
                and event["toolName"] == "fast"
                and not raised
            ):
                raised = True
                raise RuntimeError("primary-listener-error")

        agent.subscribe(listener)
        await agent.prompt("触发 Commit Boundary")
        await asyncio.sleep(0)

        self.assertTrue(slow_completed.is_set())
        self.assertTrue(agent.listener_errors)
        self.assertIn("[RuntimeError]", agent.listener_errors[0])
        self.assertNotIn("primary-listener-error", agent.listener_errors[0])
        self.assertEqual(_live_tool_tasks(), [])

    async def test_update_listener_异常后仍然_detach_子令牌(self) -> None:
        captured_token = None

        async def execute(_id, _args, _token, update):
            update(
                AgentToolResult(
                    content=[{"type": "text", "text": "触发更新"}],
                    details={},
                )
            )
            await asyncio.sleep(0)
            return AgentToolResult(content=[], details={})

        agent = Agent(
            model=self.model,
            stream_fn=self.provider("update-tool").stream,
            tools=[self.tool("update-tool", execute)],
        )
        raised = False

        def listener(event, token):
            nonlocal captured_token, raised
            captured_token = token
            if event["type"] == "tool_execution_update" and not raised:
                raised = True
                raise RuntimeError("update-listener-error")

        agent.subscribe(listener)
        await agent.prompt("触发 Update Listener 异常")
        await asyncio.sleep(0)

        self.assertIsNotNone(captured_token)
        self.assertEqual(captured_token.child_count, 0)
        self.assertEqual(agent.state.error_message, "Agent 运行失败")
        self.assertEqual(_live_tool_tasks(), [])

    async def test_tool_end_listener异常不会触发虚假的兄弟取消清理(self) -> None:
        slow_started = asyncio.Event()
        cleanup_reached = asyncio.Event()

        async def fast(_id, _args, _token, _update):
            await slow_started.wait()
            return AgentToolResult(content=[], details={})

        async def sibling(_id, _args, _token, _update):
            slow_started.set()
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                cleanup_reached.set()
                raise
            return AgentToolResult(content=[], details={})

        agent = Agent(
            model=self.model,
            stream_fn=self.provider("fast", "sibling").stream,
            tools=[
                self.tool("fast", fast),
                self.tool("sibling", sibling),
            ],
        )
        raised = False

        def listener(event, _token):
            nonlocal raised
            if event["type"] == "tool_execution_end" and not raised:
                raised = True
                raise RuntimeError("primary-listener-error")

        agent.subscribe(listener)
        await agent.prompt("Commit 后 Observer 失败")

        self.assertFalse(cleanup_reached.is_set())
        self.assertTrue(agent.listener_errors)

    async def test_用户取消后没有_tool_timer_waiter_update_task_残留(self) -> None:
        started = asyncio.Event()

        async def execute(_id, _args, _token, update):
            started.set()
            update(AgentToolResult(content=[], details={}))
            await asyncio.sleep(10)
            return AgentToolResult(content=[], details={})

        agent = Agent(
            model=self.model,
            stream_fn=self.provider("cancel-tool").stream,
            tools=[self.tool("cancel-tool", execute)],
        )
        prompt_task = asyncio.create_task(agent.prompt("取消并清理"))
        await asyncio.wait_for(started.wait(), timeout=1)
        agent.abort("测试取消")
        await asyncio.wait_for(prompt_task, timeout=1)
        await asyncio.sleep(0)

        self.assertEqual(_live_tool_tasks(), [])


def _live_tool_tasks() -> list[str]:
    current = asyncio.current_task()
    return sorted(
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not current
        and not task.done()
        and task.get_name().startswith("pi-tool-")
    )


if __name__ == "__main__":
    unittest.main()
