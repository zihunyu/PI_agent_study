"""Parallel、Exclusive Barrier 和 Resource Lock 调度测试。"""

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
    RetryableToolError,
    ScriptedProvider,
    ToolRetryPolicy,
    assistant_message,
)


class ToolSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="scheduler", provider="fake", api="fake")

    def provider(self, calls: list[dict]) -> ScriptedProvider:
        return ScriptedProvider([
            assistant_message(
                model=self.model,
                stop_reason="toolUse",
                content=calls,
            ),
            assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "调度完成"}],
            ),
        ])

    @staticmethod
    def call(call_id: str, name: str, arguments=None) -> dict:
        return {
            "type": "toolCall",
            "id": call_id,
            "name": name,
            "arguments": arguments or {},
        }

    async def test_exclusive_在前后_parallel_pool_之间形成屏障(self) -> None:
        timeline: list[str] = []
        active = 0
        max_active = 0

        def make_tool(name: str, mode: str, delay: float) -> AgentTool:
            async def execute(_id, _args, _token, _update):
                nonlocal active, max_active
                timeline.append(f"start:{name}")
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(delay)
                active -= 1
                timeline.append(f"end:{name}")
                return AgentToolResult(
                    content=[{"type": "text", "text": name}],
                    details={},
                )

            return AgentTool(
                name=name,
                label=name,
                description=name,
                execute=execute,
                execution_mode=mode,  # type: ignore[arg-type]
            )

        tools = [
            make_tool("read_a", "parallel", 0.02),
            make_tool("read_b", "parallel", 0.02),
            make_tool("write_x", "exclusive", 0.005),
            make_tool("read_d", "parallel", 0.01),
            make_tool("read_e", "parallel", 0.01),
        ]
        provider = self.provider([
            self.call("a", "read_a"),
            self.call("b", "read_b"),
            self.call("x", "write_x"),
            self.call("d", "read_d"),
            self.call("e", "read_e"),
        ])
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=tools,
            max_parallel_tools=5,
        )

        await agent.prompt("测试 Exclusive Barrier")

        write_start = timeline.index("start:write_x")
        write_end = timeline.index("end:write_x")
        self.assertLess(timeline.index("end:read_a"), write_start)
        self.assertLess(timeline.index("end:read_b"), write_start)
        self.assertGreater(timeline.index("start:read_d"), write_end)
        self.assertGreater(timeline.index("start:read_e"), write_end)
        self.assertEqual(max_active, 2)

    async def test_resource_locked_同资源串行_不同资源并行(self) -> None:
        active_by_resource: dict[str, int] = {}
        max_by_resource: dict[str, int] = {}
        active_total = 0
        max_total = 0

        async def execute(_id, args, _token, _update):
            nonlocal active_total, max_total
            key = str(args["order_id"])
            active_by_resource[key] = active_by_resource.get(key, 0) + 1
            max_by_resource[key] = max(
                max_by_resource.get(key, 0),
                active_by_resource[key],
            )
            active_total += 1
            max_total = max(max_total, active_total)
            await asyncio.sleep(0.015)
            active_total -= 1
            active_by_resource[key] -= 1
            return AgentToolResult(
                content=[{"type": "text", "text": key}],
                details={},
            )

        tool = AgentTool(
            name="update_order",
            label="更新订单",
            description="按订单号获取资源锁",
            execute=execute,
            validate_args=lambda value: value,
            execution_mode="resource_locked",
            resolve_resource_keys=lambda args: f"order:{args['order_id']}",
            replay_policy="never",
        )
        agent = Agent(
            model=self.model,
            stream_fn=self.provider([
                self.call("a1", "update_order", {"order_id": "1001"}),
                self.call("a2", "update_order", {"order_id": "1001"}),
                self.call("b", "update_order", {"order_id": "2002"}),
            ]).stream,
            tools=[tool],
            max_parallel_tools=3,
        )

        await agent.prompt("测试 Resource Lock")

        self.assertEqual(max_by_resource["1001"], 1)
        self.assertEqual(max_by_resource["2002"], 1)
        self.assertEqual(max_total, 2)

    async def test_resource_lock_retry_backoff_期间释放资源(self) -> None:
        timeline: list[str] = []
        first_attempts = 0

        async def execute(call_id, _args, _token, _update):
            nonlocal first_attempts
            if call_id == "first":
                first_attempts += 1
                timeline.append(f"first:{first_attempts}")
                if first_attempts == 1:
                    raise RetryableToolError(
                        "瞬时冲突",
                        code="temporary_conflict",
                    )
            else:
                timeline.append("second:1")
            return AgentToolResult(
                content=[{"type": "text", "text": call_id}],
                details={},
            )

        tool = AgentTool(
            name="locked_retry",
            label="同资源重试",
            description="Retry Backoff 应释放资源锁",
            execute=execute,
            execution_mode="resource_locked",
            resolve_resource_keys=lambda _args: "resource:same",
            retry_policy=ToolRetryPolicy(
                max_retries=1,
                retryable_codes=frozenset({"temporary_conflict"}),
                idempotent=True,
                initial_delay_seconds=0.03,
                max_delay_seconds=0.03,
                jitter_ratio=0,
            ),
            replay_policy="safe",
        )
        agent = Agent(
            model=self.model,
            stream_fn=self.provider([
                self.call("first", "locked_retry"),
                self.call("second", "locked_retry"),
            ]).stream,
            tools=[tool],
            max_parallel_tools=2,
        )

        await agent.prompt("测试锁与 Retry")

        self.assertEqual(timeline, ["first:1", "second:1", "first:2"])

    async def test_全局_sequential_覆盖工具_parallel(self) -> None:
        active = 0
        max_active = 0

        async def execute(_id, _args, _token, _update):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return AgentToolResult(content=[], details={})

        tools = [
            AgentTool(
                name=name,
                label=name,
                description=name,
                execute=execute,
                execution_mode="parallel",
            )
            for name in ("one", "two")
        ]
        agent = Agent(
            model=self.model,
            stream_fn=self.provider([
                self.call("one", "one"),
                self.call("two", "two"),
            ]).stream,
            tools=tools,
            tool_execution="sequential",
        )

        await agent.prompt("全局串行")
        self.assertEqual(max_active, 1)

    async def test_sequential_旧值按_exclusive_兼容(self) -> None:
        tool = AgentTool(
            name="legacy",
            label="旧串行",
            description="sequential 是 exclusive 的兼容别名",
            execute=lambda *_args: asyncio.sleep(
                0,
                result=AgentToolResult(content=[], details={}),
            ),
            execution_mode="sequential",
        )
        self.assertEqual(tool.execution_mode, "sequential")

    async def test_resource_locked_必须声明资源解析器(self) -> None:
        async def execute(_id, _args, _token, _update):
            return AgentToolResult(content=[], details={})

        with self.assertRaisesRegex(ValueError, "resolve_resource_keys"):
            AgentTool(
                name="invalid",
                label="无资源键",
                description="配置错误",
                execute=execute,
                execution_mode="resource_locked",
            )
        with self.assertRaisesRegex(ValueError, "只有 resource_locked"):
            AgentTool(
                name="invalid-two",
                label="错误资源键",
                description="配置错误",
                execute=execute,
                execution_mode="parallel",
                resolve_resource_keys=lambda _args: "x",
            )
        with self.assertRaisesRegex(ValueError, "execution_mode 无效"):
            AgentTool(
                name="invalid-three",
                label="错误模式",
                description="配置错误",
                execute=execute,
                execution_mode="unknown",  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
