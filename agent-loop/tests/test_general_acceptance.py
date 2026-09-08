"""Cross-module acceptance, transport failures and deployment contracts."""

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import httpx
import pytest

from pi_agent_loop import (
    Agent,
    CancellationToken,
    ContentSafetyPipeline,
    ContextBudget,
    DockerExecutionEnvironment,
    ExecutionPolicy,
    ExtensionPackage,
    LocalExecutionEnvironment,
    MCPClient,
    MCPServerConfig,
    MCPToolPolicy,
    ModelCallRuntime,
    SafetyDecision,
    ScriptedProvider,
    create_environment_tools,
    load_general_agent_bundle,
)
from pi_agent_loop.planning import ResultValidation
from pi_agent_loop.providers import OpenAICompatibleProvider
from test_general_context import answer, audit_store
from test_general_tasks import MODEL, store
from test_generic_extensions import profile


@pytest.mark.asyncio
async def test_toml_loads_extension_validators_and_full_tool_catalogue(tmp_path):
    (tmp_path / "extensions.toml").write_text(
        '[[packages]]\nname="checks"\n', encoding="utf-8"
    )
    config = tmp_path / "agent.toml"
    config.write_text(
        '[task]\nchecks=["accept"]\n[extensions]\nconfig="extensions.toml"\n[[tools]]\nname="environment_read"\nfactory="read"\n',
        encoding="utf-8",
    )
    closed = []

    class Resource:
        async def aclose(self):
            closed.append(True)

    def package(settings, dependencies):
        return ExtensionPackage(
            "checks",
            validators={"accept": lambda *args: ResultValidation.valid()},
            resources=(Resource(),),
        )

    read = create_environment_tools(LocalExecutionEnvironment(tmp_path))[0]
    bundle = await load_general_agent_bundle(
        config,
        artifact_store=store(tmp_path),
        tool_factories={"read": lambda settings: read},
        package_factories={"checks": package},
    )
    assert set(bundle.tool_bindings) == {
        "tool.environment_read",
        "tool.create_artifact",
    }
    assert len(bundle.validator.checks) == 1
    await bundle.resources[0].aclose()
    assert closed == [True]
    config.write_text(
        config.read_text().replace('checks=["accept"]', 'checks=["missing"]')
    )
    with pytest.raises(ValueError, match="validator"):
        await load_general_agent_bundle(
            config,
            artifact_store=store(tmp_path),
            tool_factories={
                "read": lambda settings: pytest.fail(
                    "must validate before tool factories"
                )
            },
            package_factories={"checks": package},
        )
    assert closed == [True, True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        '[[tools]]\nname="read"\nfactory="missing"',
        '[[tools]]\nname="read"\nfactory="read"\n[[tools]]\nname="read"\nfactory="read"',
    ],
)
async def test_bad_general_config_fails_before_factory(tmp_path, text):
    config = tmp_path / "agent.toml"
    config.write_text('[[artifacts]]\nname="report.md"\n' + text)
    with pytest.raises(ValueError):
        await load_general_agent_bundle(
            config,
            artifact_store=store(tmp_path),
            tool_factories={"read": lambda settings: pytest.fail("must not execute")},
        )


@pytest.mark.asyncio
async def test_actual_http_body_is_audited_after_output_budget_and_each_retry(tmp_path):
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\ndata: [DONE]\n\n',
        )

    from dataclasses import replace
    from pi_agent_loop.retry.types import ModelRetryPolicy

    configured = replace(
        profile(max_completion_tokens=2048, temperature=0.3),
        retry_policy=ModelRetryPolicy(
            enabled=True,
            max_retries=1,
            initial_delay_seconds=0.001,
            max_delay_seconds=0.001,
            jitter_ratio=0,
        ),
    )
    audit = audit_store(tmp_path / "audit.db")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider(configured, client=client)
        runtime = ModelCallRuntime(
            provider.stream,
            execution_policy=ExecutionPolicy(
                context_budget=ContextBudget(4096, output_reserve=256)
            ),
            request_audit=audit,
        )
        try:
            final = await runtime.invoke(
                MODEL,
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "read sources"}],
                        }
                    ],
                    "tools": [],
                },
            )
            assert final["stopReason"] == "stop", final
            records = await audit.load()
            assert len(captured) == len(records) == 2
            assert [item["wireBody"] for item in records] == captured
            assert all(item["max_completion_tokens"] == 256 for item in captured)
            assert "Authorization" not in json.dumps(records)
        finally:
            await runtime.aclose()
            await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("regular", [False, True])
async def test_safety_expansion_cannot_bypass_context_budget(regular):
    class ExpandingSafety:
        async def inspect(self, inspection, token):
            if inspection.stage == "model_input":
                return SafetyDecision.replace(
                    [{"role": "user", "content": "x" * 20_000}]
                )
            return SafetyDecision.allow()

    policy = ExecutionPolicy(
        content_safety=ContentSafetyPipeline([ExpandingSafety()]),
        context_budget=ContextBudget(1024, output_reserve=128, safety_margin=64),
    )
    provider = ScriptedProvider([answer()])
    if regular:
        agent = Agent(model=MODEL, stream_fn=provider.stream, execution_policy=policy)
        await agent.prompt("hello")
    else:
        runtime = ModelCallRuntime(provider.stream, execution_policy=policy)
        try:
            await runtime.invoke(
                MODEL, {"messages": [{"role": "user", "content": "hello"}]}
            )
        finally:
            await runtime.aclose()
    assert not provider.contexts


@pytest.mark.asyncio
async def test_real_streamable_http_mcp_transport():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    fixture = Path(__file__).parent / "fixtures" / "general_mcp_server.py"
    from pi_agent_loop.execution import _process

    token = CancellationToken()
    process = asyncio.create_task(
        _process((sys.executable, str(fixture), str(port)), token, timeout=30)
    )
    client = None
    try:
        async with httpx.AsyncClient(trust_env=False) as http:
            async with asyncio.timeout(15):
                while True:
                    try:
                        await http.get(f"http://127.0.0.1:{port}/mcp", timeout=0.2)
                        break
                    except httpx.TransportError:
                        await asyncio.sleep(0.1)
        client = await MCPClient.connect(
            MCPServerConfig(
                "http", url=f"http://127.0.0.1:{port}/mcp", allow_insecure_loopback=True
            ),
            tool_policies={
                "lookup": MCPToolPolicy(read_only=True, requires_approval=False)
            },
        )
        result = await client.call("lookup", {"key": "two"}, CancellationToken())
        assert "document:two" in str(result)
    finally:
        if client is not None:
            await client.aclose()
        token.cancel()
        await asyncio.gather(process, return_exceptions=True)


@pytest.mark.asyncio
async def test_mcp_disconnect_reconnect_does_not_replay_failed_call():
    fixture = Path(__file__).parent / "fixtures" / "general_mcp_server.py"
    client = await MCPClient.connect(
        MCPServerConfig("restart", command=(sys.executable, str(fixture))),
        tool_policies={
            name: MCPToolPolicy(read_only=True, requires_approval=False)
            for name in ("lookup", "crash")
        },
    )
    try:
        with pytest.raises(ConnectionError):
            await client.call("crash", {}, CancellationToken())
        result = await client.call(
            "lookup", {"key": "after restart"}, CancellationToken()
        )
        assert "document:after restart" in str(result)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_real_docker_environment_isolation_and_cleanup():
    image = os.environ.get("PI_AGENT_DOCKER_IMAGE")
    if not image:
        pytest.skip(
            "set PI_AGENT_DOCKER_IMAGE for the real Linux-container integration gate"
        )
    from tempfile import TemporaryDirectory

    with TemporaryDirectory(prefix="pi-agent-docker-") as temporary:
        workspace = Path(temporary)
        workspace.chmod(0o755)
        await _check_docker_environment(workspace, image)


async def _check_docker_environment(tmp_path, image):
    (tmp_path / "evidence.txt").write_text("task evidence", encoding="utf-8")
    environment = await DockerExecutionEnvironment.create(tmp_path, image=image)
    try:
        assert (
            await environment.read_file("evidence.txt", CancellationToken())
            == b"task evidence"
        )
        assert "evidence.txt" in await environment.list_dir(".", CancellationToken())
        checks = "import os,socket; assert os.geteuid()!=0; assert socket.if_nameindex()==[(1,'lo')]; print('isolated')"
        result = await environment.run(("python", "-c", checks), CancellationToken())
        assert result.exit_code == 0 and "isolated" in result.stdout
        write = await environment.run(
            ("python", "-c", "open('/workspace/escape','w').write('x')"),
            CancellationToken(),
        )
        assert write.exit_code != 0 and not (tmp_path / "escape").exists()
        with pytest.raises(TimeoutError):
            await environment.run(
                ("python", "-c", "import time; time.sleep(60)"),
                CancellationToken(),
                timeout=0.2,
            )
        assert not environment._active
        assert (
            create_environment_tools(environment, include_process=True)[-1].name
            == "sandbox_run"
        )
    finally:
        await environment.aclose()


@pytest.mark.asyncio
async def test_complete_evidence_chain_mcp_documents_artifact_and_idempotent_delivery(
    tmp_path,
):
    from pi_agent_loop import (
        DurableAgentHost,
        ExtensionBundle,
        create_general_agent_bundle,
    )
    from pi_agent_loop.validation import ArtifactRequirement, TaskContract
    from test_general_extensions import connect_fixture
    from test_general_knowledge import library
    from test_general_tasks import planned

    artifacts = store(tmp_path)
    docs = library(tmp_path)
    await docs.ingest(
        "manual",
        b"offline support is available",
        source_uri="fixture:manual",
        authorized=True,
    )
    citation = (await docs.search("offline", CancellationToken()))[0]["citation"]
    (tmp_path / "requirements.txt").write_text(
        "Compare offline support and cite evidence."
    )
    client = await connect_fixture()
    extension = ExtensionBundle([client.package()])
    environment = LocalExecutionEnvironment(tmp_path)
    bundle = create_general_agent_bundle(
        [create_environment_tools(environment)[0], docs.create_search_tool()],
        artifact_store=artifacts,
        extension_bundle=extension,
        task_contract=TaskContract(
            artifacts=(
                ArtifactRequirement(
                    "report.md", minimum_citations=1, required_sections=("Sources",)
                ),
            ),
            required_tools=(
                "knowledge_search",
                "environment_read",
                "mcp__fixture__lookup",
            ),
        ),
        citation_resolver=docs.resolve_citation,
    )
    provider = ScriptedProvider(
        [
            planned(
                [
                    {
                        "stepId": "read",
                        "intent": "tool.environment_read",
                        "arguments": {"path": "requirements.txt"},
                        "dependsOn": [],
                    },
                    {
                        "stepId": "search",
                        "intent": "tool.knowledge_search",
                        "arguments": {"query": "offline"},
                        "dependsOn": [],
                    },
                    {
                        "stepId": "external",
                        "intent": "tool.mcp__fixture__lookup",
                        "arguments": {"key": "one"},
                        "dependsOn": [],
                    },
                ]
            ),
            planned(
                [
                    {
                        "stepId": "report",
                        "intent": "tool.create_artifact",
                        "arguments": {
                            "name": "report.md",
                            "content": "Offline support is available.\n# Sources\nThe supplied manual and MCP fixture document:one.",
                            "citations": [citation],
                        },
                        "dependsOn": [],
                    }
                ]
            ),
        ]
    )
    host = await DurableAgentHost.create(
        session_id="session",
        tenant_id="tenant",
        state_dir=tmp_path / "host",
        model=MODEL,
        stream_fn=provider.stream,
        system_prompt="Use only observed evidence",
        general_bundle=bundle,
    )
    try:
        result = await host.submit_task("evidence-chain", "Compare offline support")
        assert result.autonomous_result.status == "completed"
        assert "document:one" in str(provider.contexts[1])
        assert citation in str(provider.contexts[1])
        assert (
            await docs.resolve_citation(
                (await artifacts.list())[0].citations[0], CancellationToken()
            )
        )["sourceUri"] == "fixture:manual"
        repeat = await host.submit_task("evidence-chain", "Compare offline support")
        assert repeat.plan_id == result.plan_id and len(provider.contexts) == 2
        assert len(await host.model_runtime.request_audit.load()) == 2
    finally:
        await host.close()
    assert client._worker.done() and extension.closed
