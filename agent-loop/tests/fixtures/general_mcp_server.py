"""Local MCP fixture; never contacts a model or external business service."""

import asyncio
import os
import sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP

server = FastMCP(
    "offline-regression",
    host="127.0.0.1",
    port=int(sys.argv[1]) if len(sys.argv) > 1 else 8000,
)


@server.tool()
async def lookup(key: str) -> str:
    """Return a fixture document for a key."""
    return f"document:{key}"


@server.tool()
async def slow(seconds: float) -> str:
    """Wait to exercise cancellation."""
    await asyncio.sleep(seconds)
    return "finished"


@server.tool()
async def crash() -> str:
    """Terminate this fixture server to exercise transport recovery."""
    os._exit(9)


revision = os.environ.get("MCP_FIXTURE_REVISION_FILE")
if revision and Path(revision).exists():

    @server.tool()
    async def added() -> str:
        """A changed catalogue after restart."""
        return "new"


if __name__ == "__main__":
    server.run(transport="streamable-http" if len(sys.argv) > 1 else "stdio")
