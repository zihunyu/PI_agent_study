"""Actual local MCP transport and extension lifecycle contracts."""

import asyncio
import sys
from pathlib import Path

import pytest

from pi_agent_loop import (
    Agent,
    CancellationToken,
    Model,
    ScriptedProvider,
    assistant_message,
)
from pi_agent_loop.extensions import (
    ExtensionPackage,
    MCPClient,
    MCPServerConfig,
    MCPToolPolicy,
    SkillRegistry,
    load_extension_bundle,
)


def skill(tmp_path):
    directory = tmp_path / "compare"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: compare\ndescription: Compare documents and cite sources\n---\nRead each document and record its version.\n",
        encoding="utf-8",
    )
    return directory


def test_skills_load_on_demand_and_reject_changed_contract(tmp_path):
    directory = skill(tmp_path)
    registry = SkillRegistry([directory])
    assert "Read each" not in str(registry.metadata())
    assert "Read each" in registry.load("compare")["body"]
    (directory / "SKILL.md").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError):
        registry.load("compare")


def test_skill_name_collisions_and_disabled_model_invocation(tmp_path):
    directory = skill(tmp_path)
    with pytest.raises(ValueError, match="duplicate"):
        SkillRegistry([directory, directory])
    path = directory / "SKILL.md"
    path.write_text(
        path.read_text().replace(
            "description:", "disable-model-invocation: true\ndescription:"
        ),
        encoding="utf-8",
    )
    registry = SkillRegistry([directory])
    assert registry.metadata() == []
    with pytest.raises(PermissionError):
        registry.load("compare")
    assert registry.load("compare", user_invoked=True)["body"]


@pytest.mark.asyncio
async def test_extension_dependency_order_close_and_rollback(tmp_path):
    path = tmp_path / "extensions.toml"
    path.write_text(
        '[[packages]]\nname="second"\ndepends_on=["first"]\n[[packages]]\nname="first"\n',
        encoding="utf-8",
    )
    events = []

    class Resource:
        def __init__(self, name):
            self.name = name

        async def aclose(self):
            events.append("close:" + self.name)

    def first(settings, dependencies):
        events.append("first")
        return ExtensionPackage("first", resources=(Resource("first"),))

    def second(settings, dependencies):
        assert list(dependencies) == ["first"]
        events.append("second")
        return ExtensionPackage("second", resources=(Resource("second"),))

    bundle = await load_extension_bundle(
        path, package_factories={"first": first, "second": second}
    )
    assert events == ["first", "second"]
    await bundle.aclose()
    await bundle.aclose()
    assert events == ["first", "second", "close:second", "close:first"]
    events.clear()

    def broken(settings, dependencies):
        raise RuntimeError("failed dependency initialization")

    with pytest.raises(RuntimeError):
        await load_extension_bundle(
            path, package_factories={"first": first, "second": broken}
        )
    assert events == ["first", "close:first"]


@pytest.mark.asyncio
async def test_cyclic_extensions_rejected_before_factories(tmp_path):
    path = tmp_path / "extensions.toml"
    path.write_text(
        '[[packages]]\nname="first"\ndepends_on=["first"]\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="cyclic"):
        await load_extension_bundle(
            path,
            package_factories={
                "first": lambda *args: pytest.fail("factory must not run")
            },
        )


@pytest.mark.asyncio
async def test_activation_rollback_drains_resources_after_repeated_cancellation(
    tmp_path,
):
    path = tmp_path / "extensions.toml"
    path.write_text(
        '[[packages]]\nname="first"\n[[packages]]\nname="second"\ndepends_on=["first"]\n',
        encoding="utf-8",
    )
    starting = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    class Resource:
        async def aclose(self):
            closing.set()
            await release.wait()
            closed.set()

    async def second(settings, dependencies):
        starting.set()
        await asyncio.Future()

    activation = asyncio.create_task(
        load_extension_bundle(
            path,
            package_factories={
                "first": lambda *args: ExtensionPackage(
                    "first", resources=(Resource(),)
                ),
                "second": second,
            },
        )
    )
    try:
        await asyncio.wait_for(starting.wait(), 5)
        activation.cancel()
        await asyncio.wait_for(closing.wait(), 5)
        activation.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not activation.done()
    finally:
        release.set()
        result = await asyncio.wait_for(
            asyncio.gather(activation, return_exceptions=True), 5
        )
    assert isinstance(result[0], BaseException)
    assert closed.is_set()


async def connect_fixture():
    return await MCPClient.connect(
        MCPServerConfig(
            "fixture",
            command=(
                sys.executable,
                str(Path(__file__).parent / "fixtures" / "general_mcp_server.py"),
            ),
        ),
        tool_policies={
            "lookup": MCPToolPolicy(read_only=True, requires_approval=False),
            "slow": MCPToolPolicy(read_only=True, requires_approval=False),
        },
    )


@pytest.mark.asyncio
async def test_actual_stdio_mcp_tool_through_agent():
    client = await connect_fixture()
    model = Model(id="offline", provider="scripted")
    provider = ScriptedProvider(
        [
            assistant_message(
                model=model,
                content=[
                    {
                        "type": "toolCall",
                        "id": "m1",
                        "name": "mcp__fixture__lookup",
                        "arguments": {"key": "one"},
                    }
                ],
                stop_reason="toolUse",
            ),
            assistant_message(model=model, content=[{"type": "text", "text": "found"}]),
        ]
    )
    try:
        agent = Agent(
            model=model, stream_fn=provider.stream, tools=list(client.tools())
        )
        await agent.prompt("Find document one")
        assert "document:one" in str(agent.state.messages)
        assert not any(
            item.get("isError")
            for item in agent.state.messages
            if item["role"] == "toolResult"
        )
        with pytest.raises(ValueError):
            client.tools()[0].validate_args({"key": 42})
    finally:
        await client.aclose()
    assert client._worker.done()


@pytest.mark.asyncio
async def test_mcp_cancellation_does_not_leave_call_or_owner_task():
    client = await connect_fixture()
    token = CancellationToken()
    task = asyncio.create_task(client.call("slow", {"seconds": 60}, token))
    await asyncio.sleep(0.1)
    token.cancel("test cancellation")
    try:
        await asyncio.wait_for(task, timeout=3)
    except Exception:
        pass
    await client.aclose()
    assert client._worker.done()
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith(("mcp-owner:", "mcp-call:")) and not task.done()
    ]


@pytest.mark.parametrize(
    "config",
    [
        {"name": "../bad", "command": ("python",)},
        {"name": "ok", "url": "http://example.com"},
        {"name": "ok", "url": "https://u:p@example.com"},
        {"name": "ok"},
        {"name": "ok", "command": ("python",), "max_reconnects": -1},
    ],
)
def test_mcp_invalid_config_rejected(config):
    with pytest.raises(ValueError):
        MCPServerConfig(**config)
