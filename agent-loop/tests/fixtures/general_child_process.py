"""A real parent process exits after its child commits, before mailbox delivery."""

import asyncio
import json
import os
from pathlib import Path
import sys

# Reuse the trusted offline fixture, not application or historical callbacks.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_general_children import ALLOCATION, setup  # noqa: E402
from test_general_tasks import store  # noqa: E402


async def main():
    mode, directory = sys.argv[1:]
    path = Path(directory)
    create, provider, _ = setup(path, lease_seconds=3)
    manager = create()
    session = await manager.enqueue(
        "research",
        "request-1",
        "Create the report",
        factory="research",
        allocation=ALLOCATION,
    )
    if mode == "crash":

        async def crash(message_id, result, claim):
            (path / "child-completed.json").write_text(
                json.dumps(
                    {"status": result["status"], "modelCalls": len(provider.contexts)}
                ),
                encoding="utf-8",
            )
            # No finally blocks, resource closes or lease releases can run.
            os._exit(91)

        manager._finish = crash
    else:
        assert mode == "recover"
    try:
        async with asyncio.timeout(30):
            result = None
            while result is None:
                result = await manager.run_next("research")
                if result is None:
                    await asyncio.sleep(0.1)
        assert result["status"] == "completed"
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "recoveryModelCalls": len(provider.contexts),
                    "results": len(await manager.results()),
                    "artifacts": len(await store(path, session=session).list()),
                }
            )
        )
    finally:
        await manager.aclose()


if __name__ == "__main__":
    asyncio.run(main())
