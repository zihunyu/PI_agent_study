"""Run with: python examples/business_package/run_demo.py (installed package)."""

import asyncio
import json
import tempfile
from pathlib import Path

from pi_agent_loop import (
    DurableAgentHost,
    Model,
    ScriptedProvider,
    assistant_message,
    load_business_bundle,
)
from tools import tool_factories


class FictionalClient:
    async def lookup(self, key):
        return f"Fictional entry: {key}"


async def main():
    bundle = load_business_bundle(
        Path(__file__).with_name("business.toml"),
        tool_factories=tool_factories(FictionalClient()),
    )
    model = Model(id="offline", provider="test", api="scripted")
    plan_json = {
        "steps": [
            {
                "stepId": "lookup",
                "intent": "catalogue.lookup",
                "arguments": {"key": "BLUE"},
            }
        ]
    }
    provider = ScriptedProvider(
        [
            assistant_message(
                model=model, content=[{"type": "text", "text": json.dumps(plan_json)}]
            )
        ]
    )
    with tempfile.TemporaryDirectory() as state:
        host = await DurableAgentHost.create(
            session_id="demo",
            state_dir=state,
            model=model,
            stream_fn=provider.stream,
            system_prompt="Offline demo",
            business_bundle=bundle,
        )
        try:
            plan = await host.plan("Read BLUE")
            result = await host.execute_plan(plan.plan_id)
            print(result.state.phase)
            print(result.state.steps["lookup"].result)
        finally:
            await host.close()


if __name__ == "__main__":
    asyncio.run(main())
