"""Open task -> evidence -> correction -> artifact -> verified completion."""

import asyncio
import hashlib
import json
import sys

import pytest

from pi_agent_loop import (
    CancellationToken,
    DurableAgentHost,
    Model,
    ScriptedProvider,
    assistant_message,
)
from pi_agent_loop.artifacts import ArtifactStore
from pi_agent_loop.execution import (
    DockerExecutionEnvironment,
    LocalExecutionEnvironment,
    create_environment_tools,
)
from pi_agent_loop.general import create_general_agent_bundle
from pi_agent_loop.session import (
    JournalPrincipal,
    SQLiteSessionEventJournal,
    StaticJournalKeyProvider,
)
from pi_agent_loop.validation import ArtifactRequirement, TaskContract


MODEL = Model(id="offline", provider="scripted")


def store(tmp_path, session="session", tenant="tenant"):
    keys = StaticJournalKeyProvider({"test": b"a" * 32}, active_key_id="test")
    journal = SQLiteSessionEventJournal(
        tmp_path / "artifacts.sqlite3", key_provider=keys
    )
    return ArtifactStore(journal, JournalPrincipal.system(tenant), session)


def planned(steps):
    return assistant_message(
        model=MODEL, content=[{"type": "text", "text": json.dumps({"steps": steps})}]
    )


@pytest.mark.asyncio
async def test_open_task_reads_then_replans_and_verifies_report(tmp_path):
    (tmp_path / "product.txt").write_text(
        "Product A supports offline operation.", encoding="utf-8"
    )
    artifacts = store(tmp_path)
    environment = LocalExecutionEnvironment(tmp_path)
    bundle = create_general_agent_bundle(
        create_environment_tools(environment),
        artifact_store=artifacts,
        task_contract=TaskContract(
            artifacts=(
                ArtifactRequirement(
                    "report.md", minimum_bytes=20, required_sections=("Comparison",)
                ),
            ),
            required_tools=("environment_read",),
        ),
    )
    provider = ScriptedProvider(
        [
            planned(
                [
                    {
                        "stepId": "read",
                        "intent": "tool.environment_read",
                        "arguments": {"path": "product.txt"},
                        "dependsOn": [],
                    }
                ]
            ),
            planned(
                [
                    {
                        "stepId": "report",
                        "intent": "tool.create_artifact",
                        "arguments": {
                            "name": "report.md",
                            "content": "# Comparison\nProduct A supports offline operation.",
                        },
                        "dependsOn": [],
                    }
                ]
            ),
        ]
    )
    host = await DurableAgentHost.create(
        session_id="session",
        state_dir=tmp_path / "host",
        tenant_id="tenant",
        model=MODEL,
        stream_fn=provider.stream,
        system_prompt="Complete the task using evidence.",
        general_bundle=bundle,
    )
    try:
        result = await host.prompt(
            "Read the product information and create a comparison report."
        )
        assert result.autonomous_result.status == "completed"
        assert len(result.autonomous_result.plans) == 2
        assert "Product A supports offline" in str(provider.contexts[1])
        output = (await artifacts.list())[0]
        assert output.name == "report.md"
        assert b"Product A supports offline" in output.data
        audits = await host.model_runtime.request_audit.load()
        assert len(audits) == 2
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_artifact_claim_does_not_prove_completion(tmp_path):
    artifacts = store(tmp_path)
    environment = LocalExecutionEnvironment(tmp_path)
    bundle = create_general_agent_bundle(
        create_environment_tools(environment),
        artifact_store=artifacts,
        task_contract=TaskContract(
            artifacts=(
                ArtifactRequirement("report.md", required_sections=("Sources",)),
            )
        ),
    )
    provider = ScriptedProvider(
        [
            planned(
                [
                    {
                        "stepId": "report",
                        "intent": "tool.create_artifact",
                        "arguments": {
                            "name": "report.md",
                            "content": "I have completed everything successfully.",
                        },
                        "dependsOn": [],
                    }
                ]
            )
            for _ in range(3)
        ]
    )
    host = await DurableAgentHost.create(
        session_id="session",
        state_dir=tmp_path / "host",
        tenant_id="tenant",
        model=MODEL,
        stream_fn=provider.stream,
        system_prompt="Create a report.",
        general_bundle=bundle,
    )
    try:
        result = await host.prompt("Write a report with a Sources section")
        assert result.autonomous_result.status != "completed"
        assert len(await artifacts.list()) == 1
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_artifacts_are_deduplicated_encrypted_scoped_and_exportable(tmp_path):
    artifacts = store(tmp_path)
    first, second = await asyncio.gather(
        artifacts.put("report.md", b"private comparison report"),
        artifacts.put("report.md", b"private comparison report"),
    )
    assert first.artifact_id == second.artifact_id
    assert len(await artifacts.list()) == 1
    with pytest.raises(KeyError):
        await store(tmp_path, tenant="other").get(first.artifact_id)
    assert (
        b"private comparison report"
        not in (tmp_path / "artifacts.sqlite3").read_bytes()
    )
    environment = LocalExecutionEnvironment(tmp_path, read_only=False)
    await artifacts.export(
        first.artifact_id, environment, "output.md", CancellationToken()
    )
    assert (tmp_path / "output.md").read_bytes() == first.data
    with pytest.raises(ValueError, match="version"):
        await environment.write_file("output.md", b"overwrite", CancellationToken())
    await environment.write_file(
        "output.md", b"updated", CancellationToken(), expected_digest=first.digest
    )


@pytest.mark.asyncio
async def test_local_environment_contract_and_process_cleanup(tmp_path):
    environment = LocalExecutionEnvironment(
        tmp_path, read_only=False, allow_trusted_processes=True
    )
    token = CancellationToken()
    digest = await environment.write_file("test.txt", b"one", token)
    assert digest == hashlib.sha256(b"one").hexdigest()
    assert await environment.read_file("test.txt", token) == b"one"
    assert "test.txt" in await environment.list_dir(".", token)
    with pytest.raises(Exception):
        await environment.read_file("../outside.txt", token)
    with pytest.raises(ValueError, match="isolated"):
        create_environment_tools(environment, include_process=True)
    result = await environment.run((sys.executable, "-c", "print('ok')"), token)
    assert result.stdout.strip() == "ok"
    with pytest.raises(TimeoutError):
        await environment.run(
            (sys.executable, "-c", "import time; time.sleep(60)"), token, timeout=0.1
        )
    await environment.aclose()
    with pytest.raises(RuntimeError):
        await environment.read_file("test.txt", token)


@pytest.mark.asyncio
async def test_unavailable_sandbox_does_not_fall_back_to_host(tmp_path):
    with pytest.raises(RuntimeError, match="fallback"):
        await DockerExecutionEnvironment.create(
            tmp_path,
            image="python:3.12-slim",
            executable="pi-agent-nonexistent-docker-binary",
        )


def test_docker_arguments_cannot_become_host_docker_options(tmp_path):
    environment = DockerExecutionEnvironment(
        tmp_path, "trusted-image-id", "docker", True
    )
    command = environment._argv("owned-container", ("python", "--privileged"))
    assert "--network=none" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    assert command.index("--privileged") > command.index("trusted-image-id")
    assert ",readonly" in next(item for item in command if "target=/workspace" in item)
