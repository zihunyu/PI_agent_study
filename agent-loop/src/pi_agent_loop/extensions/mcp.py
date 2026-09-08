"""MCP 1.x client bridge; SDK transports live and close in one owner task.

Remote annotations are descriptive. Only locally supplied policies can mark a
tool read-only/replayable. Failed calls are never automatically resubmitted.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from ..cancellation import CancellationToken
from ..async_utils import await_owned_cleanup
from ..context import content_digest
from ..types import AgentTool, AgentToolResult, ToolUpdateCallback
from .bundle import ExtensionPackage


@dataclass(frozen=True, slots=True)
class MCPToolPolicy:
    read_only: bool = False
    requires_approval: bool = True
    timeout_seconds: float = 30

    def __post_init__(self) -> None:
        if type(self.read_only) is not bool or type(self.requires_approval) is not bool:
            raise ValueError("MCP policy flags must be booleans")
        if not self.read_only and not self.requires_approval:
            raise ValueError("MCP writes require local approval policy")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("MCP timeout must be finite and positive")


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    name: str
    command: tuple[str, ...] = ()
    url: str | None = None
    environment: dict[str, str] = field(default_factory=dict, repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    allow_insecure_loopback: bool = False
    startup_timeout: float = 30
    max_tools: int = 128
    max_result_bytes: int = 1_048_576
    max_reconnects: int = 2

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}", self.name):
            raise ValueError("invalid MCP server namespace")
        if bool(self.command) == bool(self.url):
            raise ValueError("configure exactly one MCP command or URL")
        if any(
            not isinstance(item, str) or not item or "\x00" in item
            for item in self.command
        ):
            raise ValueError("MCP command must contain non-empty argv strings")
        if self.url is not None:
            parsed = urlparse(self.url)
            local_http = (
                self.allow_insecure_loopback
                and parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            )
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
                or (parsed.scheme != "https" and not local_http)
            ):
                raise ValueError(
                    "MCP requires HTTPS or explicitly allowed loopback HTTP"
                )
        if (
            isinstance(self.startup_timeout, bool)
            or not isinstance(self.startup_timeout, (int, float))
            or not math.isfinite(self.startup_timeout)
            or self.startup_timeout <= 0
        ):
            raise ValueError("MCP startup timeout must be positive")
        for name in ("max_tools", "max_result_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("MCP limits must be positive integers")
        if type(self.max_reconnects) is not int or self.max_reconnects < 0:
            raise ValueError("max_reconnects must be non-negative")


class MCPContractChangedError(RuntimeError):
    """A reconnect discovered different schemas; session migration is required."""


class MCPClient:
    def __init__(
        self, config: MCPServerConfig, policies: dict[str, MCPToolPolicy]
    ) -> None:
        if any(
            not isinstance(name, str)
            or not name
            or not isinstance(policy, MCPToolPolicy)
            for name, policy in policies.items()
        ):
            raise ValueError(
                "MCP policies must explicitly map tool names to MCPToolPolicy"
            )
        if any(len(f"mcp__{config.name}__{name}") > 64 for name in policies):
            raise ValueError("qualified MCP tool names must be at most 64 characters")
        self.config = config
        self.policies = dict(policies)
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=128)
        self._worker: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._active: asyncio.Task[Any] | None = None
        self._active_future: asyncio.Future[Any] | None = None
        self._closed = False
        self._catalogue: tuple[dict[str, Any], ...] = ()
        self._digest: str | None = None

    @classmethod
    async def connect(
        cls, config: MCPServerConfig, *, tool_policies: dict[str, MCPToolPolicy]
    ) -> MCPClient:
        client = cls(config, tool_policies)
        client._ready = asyncio.get_running_loop().create_future()
        client._worker = asyncio.create_task(
            client._serve(), name=f"mcp-owner:{config.name}"
        )
        try:
            async with asyncio.timeout(config.startup_timeout):
                await asyncio.shield(client._ready)
        except BaseException:
            await client.aclose()
            raise
        return client

    async def _connect(self, stack: AsyncExitStack) -> Any:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as error:
            raise ImportError("MCP requires the 'mcp' installation extra") from error
        if self.config.command:
            errlog = stack.enter_context(open(os.devnull, "w"))
            transport = stdio_client(
                StdioServerParameters(
                    command=self.config.command[0],
                    args=list(self.config.command[1:]),
                    env=dict(self.config.environment),
                ),
                errlog=errlog,
            )
        else:
            import httpx

            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers=dict(self.config.headers),
                    timeout=httpx.Timeout(self.config.startup_timeout),
                    follow_redirects=False,
                    trust_env=False,
                )
            )
            transport = streamable_http_client(str(self.config.url), http_client=http)
        streams = await stack.enter_async_context(transport)
        session = await stack.enter_async_context(
            ClientSession(
                streams[0],
                streams[1],
                read_timeout_seconds=timedelta(seconds=self.config.startup_timeout),
            )
        )
        await session.initialize()
        catalogue = []
        cursor = None
        cursors: set[str] = set()
        while True:
            page = await session.list_tools(cursor=cursor)
            for item in page.tools:
                raw = item.model_dump(mode="json", by_alias=True, exclude_none=True)
                _validate_schema(raw.get("inputSchema"))
                catalogue.append(raw)
                if len(catalogue) > self.config.max_tools:
                    raise ValueError("MCP tool catalogue exceeds limit")
            cursor = page.nextCursor
            if cursor is None:
                break
            if cursor in cursors:
                raise ValueError("MCP repeated pagination cursor")
            cursors.add(cursor)
        names = [item["name"] for item in catalogue]
        if len(names) != len(set(names)):
            raise ValueError("MCP returned duplicate tool names")
        if set(self.policies) - set(names):
            raise ValueError("configured MCP tool is missing from the server")
        # Unconfigured tools are not admitted at all. Changes to description or
        # schema require migration, even if a server says they are compatible.
        selected = tuple(
            sorted(
                (item for item in catalogue if item["name"] in self.policies),
                key=lambda item: item["name"],
            )
        )
        digest = content_digest(selected)
        if self._digest is not None and digest != self._digest:
            raise MCPContractChangedError(
                "MCP tool contract changed; reload and migrate session"
            )
        self._catalogue = selected
        self._digest = digest
        return session

    async def _serve(self) -> None:
        reconnects = 0
        try:
            while not self._closed:
                try:
                    async with AsyncExitStack() as stack:
                        session = await self._connect(stack)
                        if self._ready is not None and not self._ready.done():
                            self._ready.set_result(None)
                        while not self._closed:
                            request = await self._queue.get()
                            if request is None:
                                return
                            name, arguments, future = request
                            if future.done():
                                continue
                            self._active_future = future
                            self._active = asyncio.create_task(
                                session.call_tool(
                                    name,
                                    arguments,
                                    read_timeout_seconds=timedelta(
                                        seconds=self.policies[name].timeout_seconds
                                    ),
                                ),
                                name=f"mcp-call:{self.config.name}:{name}",
                            )
                            active = self._active
                            future.add_done_callback(
                                lambda done, task=active: (
                                    task.cancel()
                                    if done.cancelled() and not task.done()
                                    else None
                                )
                            )
                            try:
                                result = await active
                                raw = result.model_dump(
                                    mode="json", by_alias=True, exclude_none=True
                                )
                                if (
                                    len(json.dumps(raw, ensure_ascii=False).encode())
                                    > self.config.max_result_bytes
                                ):
                                    raise ValueError("MCP tool result exceeds limit")
                                if not future.done():
                                    future.set_result(raw)
                            except asyncio.CancelledError:
                                current = asyncio.current_task()
                                if self._closed or (
                                    current is not None and current.cancelling()
                                ):
                                    raise
                                if not future.done():
                                    future.cancel()
                            except Exception:
                                if not future.done():
                                    future.set_exception(
                                        ConnectionError(
                                            "MCP tool call failed; submission outcome may be unknown"
                                        )
                                    )
                                raise
                            finally:
                                if not active.done():
                                    active.cancel()
                                await asyncio.gather(active, return_exceptions=True)
                                self._active = None
                                self._active_future = None
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if self._ready is not None and not self._ready.done():
                        self._ready.set_exception(
                            RuntimeError("MCP initialization failed")
                        )
                        return
                    reconnects += 1
                    if (
                        isinstance(error, MCPContractChangedError)
                        or reconnects > self.config.max_reconnects
                    ):
                        return
                    await asyncio.sleep(min(0.1 * 2 ** (reconnects - 1), 1.0))
        finally:
            self._closed = True
            if self._ready is not None and not self._ready.done():
                self._ready.cancel()
            if self._active_future is not None and not self._active_future.done():
                self._active_future.cancel()
            while not self._queue.empty():
                request = self._queue.get_nowait()
                if request is not None and not request[2].done():
                    request[2].set_exception(
                        ConnectionError("MCP connection is closed")
                    )

    async def call(
        self, name: str, arguments: dict[str, Any], cancellation: CancellationToken
    ) -> dict[str, Any]:
        if self._closed or self._worker is None or self._worker.done():
            raise RuntimeError("MCP client is closed")
        if name not in self.policies:
            raise PermissionError("MCP tool is not locally authorized")
        raw = next(item for item in self._catalogue if item["name"] == name)
        validator = self._tool(raw).validate_args
        if validator is not None:
            arguments = validator(arguments)
        cancellation.throw_if_cancelled()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        waiter = asyncio.create_task(cancellation.wait())
        try:
            async with asyncio.timeout(self.policies[name].timeout_seconds):
                await self._queue.put((name, copy.deepcopy(arguments), future))
                done, _ = await asyncio.wait(
                    {future, waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                if waiter in done:
                    cancellation.throw_if_cancelled()
                return await future
        finally:
            if not future.done():
                future.cancel()
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    def tools(self) -> tuple[AgentTool, ...]:
        return tuple(self._tool(raw) for raw in self._catalogue)

    def _tool(self, raw: dict[str, Any]) -> AgentTool:
        remote = raw["name"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", remote):
            raise ValueError("MCP remote tool name cannot be represented safely")
        policy = self.policies[remote]
        schema = copy.deepcopy(raw["inputSchema"])
        schema.setdefault("additionalProperties", False)
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(schema)

        def validate(value: Any) -> Any:
            if not isinstance(value, dict) or not validator.is_valid(value):
                raise ValueError("MCP arguments violate the installed tool schema")
            return copy.deepcopy(value)

        async def execute(
            call_id: str,
            arguments: dict[str, Any],
            cancellation: CancellationToken,
            update: ToolUpdateCallback,
        ) -> AgentToolResult:
            result = await self.call(remote, arguments, cancellation)
            if result.get("isError", False):
                raise RuntimeError(
                    "MCP server reported tool failure; side-effect outcome requires verification"
                )
            content = result.get("content", [])
            # Resource links and other protocol-specific objects remain bounded
            # JSON evidence until the application installs a dedicated adapter.
            public = [
                item
                if item.get("type") == "text"
                else {"type": "text", "text": json.dumps(item, ensure_ascii=False)}
                for item in content
            ]
            return AgentToolResult(
                content=public,
                details={
                    "mcpServer": self.config.name,
                    "remoteTool": remote,
                    "structuredContent": result.get("structuredContent"),
                    "isError": False,
                },
            )

        return AgentTool(
            name=f"mcp__{self.config.name}__{remote}",
            label=remote,
            description=str(raw.get("description", remote)),
            parameters=schema,
            validate_args=validate,
            execute=execute,
            execution_mode="parallel" if policy.read_only else "exclusive",
            replay_policy="safe" if policy.read_only else "never",
            requires_approval=policy.requires_approval,
            timeout_seconds=policy.timeout_seconds,
            security_policy_version=content_digest(
                {
                    "contract": raw,
                    "readOnly": policy.read_only,
                    "approval": policy.requires_approval,
                }
            ),
        )

    def package(self) -> ExtensionPackage:
        return ExtensionPackage(
            self.config.name, self.tools(), (self,), version=str(self._digest)
        )

    async def aclose(self) -> None:
        self._closed = True
        if self._active_future is not None and not self._active_future.done():
            self._active_future.cancel()
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
        if self._worker is not None:
            await await_owned_cleanup(
                asyncio.gather(self._worker, return_exceptions=True)
            )


def _validate_schema(schema: Any) -> None:
    from jsonschema import Draft202012Validator

    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("MCP inputSchema must be an object schema")
    if len(json.dumps(schema).encode()) > 128_000:
        raise ValueError("MCP schema exceeds limit")

    def inspect_value(value: Any) -> None:
        if isinstance(value, dict):
            if "$ref" in value and not str(value["$ref"]).startswith("#/"):
                raise ValueError("external schema references are forbidden")
            for item in value.values():
                inspect_value(item)
        elif isinstance(value, list):
            for item in value:
                inspect_value(item)

    inspect_value(schema)
    Draft202012Validator.check_schema(schema)
