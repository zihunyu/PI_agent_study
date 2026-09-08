"""Public APIs only. Scripted model, fictional input, no network or paid calls."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from pi_agent_loop import (
    ArtifactStore,
    DurableAgentHost,
    LocalExecutionEnvironment,
    Model,
    ScriptedProvider,
    assistant_message,
    create_environment_tools,
    load_general_agent_bundle,
)
from pi_agent_loop.session import (
    JournalPrincipal,
    SQLiteSessionEventJournal,
    StaticJournalKeyProvider,
)


async def main() -> None:
    with TemporaryDirectory(prefix="general-agent-demo-") as temporary:
        root = Path(temporary)
        (root / "products.txt").write_text(
            "Fictional product A works offline; product B requires a connection.",
            encoding="utf-8",
        )
        # Demo-only ephemeral key. Real deployments inject a persistent key
        # provider; a restarted session must receive the same trusted identity.
        journal = SQLiteSessionEventJournal(
            root / "artifacts.sqlite3",
            key_provider=StaticJournalKeyProvider(
                {"demo": b"d" * 32}, active_key_id="demo"
            ),
        )
        artifacts = ArtifactStore(
            journal, JournalPrincipal.system("demo"), "comparison"
        )
        environment = LocalExecutionEnvironment(root)
        read = create_environment_tools(environment)[0]
        bundle = await load_general_agent_bundle(
            Path(__file__).with_name("agent.toml"),
            artifact_store=artifacts,
            tool_factories={"workspace_read": lambda settings: read},
        )
        model = Model(id="offline", provider="scripted")

        def response(intent, arguments):
            return assistant_message(
                model=model,
                content=[
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "steps": [
                                    {
                                        "stepId": "work",
                                        "intent": intent,
                                        "arguments": arguments,
                                        "dependsOn": [],
                                    }
                                ]
                            }
                        ),
                    }
                ],
            )

        provider = ScriptedProvider(
            [
                response("tool.environment_read", {"path": "products.txt"}),
                response(
                    "tool.create_artifact",
                    {
                        "name": "comparison.md",
                        "content": "# Comparison\nProduct A supports offline work; B needs a connection.\n# Evidence\nBased on the supplied fictional products.txt.",
                    },
                ),
            ]
        )
        host = await DurableAgentHost.create(
            session_id="comparison",
            tenant_id="demo",
            state_dir=root / "state",
            model=model,
            stream_fn=provider.stream,
            system_prompt="Read evidence and create the requested artifact.",
            general_bundle=bundle,
            owned_resources=(environment,),
        )
        try:
            result = await host.submit_task(
                "comparison-request-1",
                "Read products.txt and compare offline operation.",
            )
            duplicate = await host.submit_task(
                "comparison-request-1",
                "Read products.txt and compare offline operation.",
            )
            assert result.plan_id == duplicate.plan_id and len(provider.contexts) == 2
            outputs = await artifacts.list()
            assert len(outputs) == 1
            assert result.autonomous_result is not None
            print(
                json.dumps(
                    {
                        "status": result.autonomous_result.status,
                        "artifact": outputs[0].name,
                        "modelCalls": len(provider.contexts),
                        "duplicateSubmission": "reused",
                    }
                )
            )
        finally:
            await host.close()


if __name__ == "__main__":
    asyncio.run(main())
