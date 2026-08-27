"""Parallel、Exclusive 和 Resource-Locked 调度离线演示。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pi_agent_loop import (  # noqa: E402
    Agent,
    AgentTool,
    AgentToolResult,
    Model,
    ScriptedProvider,
    assistant_message,
)


async def main() -> None:
    model = Model(id="scheduler-demo", provider="scripted", api="fake")
    timeline: list[str] = []

    def create_tool(name: str, mode: str, delay: float, *, locked=False):
        async def execute(call_id, arguments, _token, _update):
            resource = arguments.get("order_id", "-")
            timeline.append(f"start {call_id} resource={resource}")
            await asyncio.sleep(delay)
            timeline.append(f"end   {call_id} resource={resource}")
            return AgentToolResult(
                content=[{"type": "text", "text": call_id}],
                details={},
            )

        return AgentTool(
            name=name,
            label=name,
            description=name,
            execute=execute,
            validate_args=lambda value: value,
            execution_mode=mode,  # type: ignore[arg-type]
            resolve_resource_keys=(
                (lambda args: f"order:{args['order_id']}") if locked else None
            ),
            replay_policy="never" if mode != "parallel" else "safe",
        )

    tools = [
        create_tool("parallel_read", "parallel", 0.02),
        create_tool("exclusive_write", "exclusive", 0.005),
        create_tool("locked_update", "resource_locked", 0.015, locked=True),
    ]
    calls = [
        {"type": "toolCall", "id": "read-a", "name": "parallel_read", "arguments": {}},
        {"type": "toolCall", "id": "read-b", "name": "parallel_read", "arguments": {}},
        {"type": "toolCall", "id": "exclusive", "name": "exclusive_write", "arguments": {}},
        {"type": "toolCall", "id": "order-1001-a", "name": "locked_update", "arguments": {"order_id": "1001"}},
        {"type": "toolCall", "id": "order-1001-b", "name": "locked_update", "arguments": {"order_id": "1001"}},
        {"type": "toolCall", "id": "order-2002", "name": "locked_update", "arguments": {"order_id": "2002"}},
    ]
    provider = ScriptedProvider([
        assistant_message(model=model, stop_reason="toolUse", content=calls),
        assistant_message(
            model=model,
            content=[{"type": "text", "text": "调度演示完成"}],
        ),
    ])
    agent = Agent(
        model=model,
        stream_fn=provider.stream,
        tools=tools,
        max_parallel_tools=3,
    )
    await agent.prompt("演示工具调度")

    print("调度时间线：")
    for item in timeline:
        print(" ", item)
    print("\n观察重点：")
    print("1. read-a/read-b 同时开始。")
    print("2. exclusive 等两个 read 结束后才开始。")
    print("3. exclusive 结束后才开始 locked_update。")
    print("4. order:1001 两次更新串行；order:2002 可以并行。")


if __name__ == "__main__":
    asyncio.run(main())
