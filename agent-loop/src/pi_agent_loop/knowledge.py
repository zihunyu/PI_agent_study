"""Versioned document ingestion, semantic retrieval and revocable citations."""

from __future__ import annotations

import base64
import asyncio
import hashlib
import inspect
import json
import math
import re
import sys
from uuid import uuid4
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .cancellation import CancellationToken
from .async_utils import await_owned_cleanup
from .context import content_digest
from .execution import _process
from .memory.embeddings import EmbeddingProvider
from .session.journal import JournalPrincipal, SessionEventJournal
from .session.records import JournalRecordStore
from .tools.workspace_validators import object_args, text_arg
from .types import AgentTool, AgentToolResult, ToolUpdateCallback


class DocumentLibrary:
    def __init__(
        self,
        journal: SessionEventJournal,
        principal: JournalPrincipal,
        session_id: str,
        embedding_provider: EmbeddingProvider,
        *,
        authorize_source: Callable[..., Any],
        embedding_version: str = "1",
        max_documents: int = 256,
        chunk_characters: int = 1800,
    ) -> None:
        if not callable(authorize_source):
            raise TypeError("documents require a trusted source authorization callback")
        if (
            type(max_documents) is not int
            or not 0 < max_documents <= 10_000
            or type(chunk_characters) is not int
            or not 128 <= chunk_characters <= 8000
        ):
            raise ValueError("document/chunk limits are invalid")
        if not isinstance(embedding_version, str) or not embedding_version:
            raise ValueError("embedding_version is required")
        self.records = JournalRecordStore(journal, principal, session_id, "documents")
        self.embedding_provider = embedding_provider
        self.embedding_version = str(
            getattr(embedding_provider, "fingerprint", embedding_version)
        )
        self.authorize_source = authorize_source
        self.max_documents = max_documents
        self.chunk_characters = chunk_characters

    async def _allowed(self, source_id: str, token: CancellationToken) -> bool:
        token.throw_if_cancelled()
        result = self.authorize_source(source_id, self.records.principal, token)
        allowed = await result if inspect.isawaitable(result) else result
        if type(allowed) is not bool:
            raise TypeError("source authorization must return a boolean")
        token.throw_if_cancelled()
        return allowed

    async def ingest(
        self,
        source_id: str,
        data: bytes,
        *,
        media_type: str = "text/plain",
        source_uri: str,
        authorized: bool = False,
        cancellation: CancellationToken | None = None,
    ) -> str:
        token = cancellation or CancellationToken()
        self.records.operation_id(source_id)
        if authorized is not True:
            raise PermissionError("document storage requires explicit authorization")
        if not await self._allowed(source_id, token):
            raise PermissionError("document source is not authorized")
        if (
            not isinstance(source_uri, str)
            or not source_uri.strip()
            or len(source_uri) > 512
        ):
            raise ValueError("document source URI must be bounded")
        if not isinstance(data, bytes) or not 0 < len(data) <= 5 * 1024 * 1024:
            raise ValueError("document must be non-empty and at most 5 MiB")
        existing = await self.records.read(source_id)
        if not existing and len(await self.records.all()) >= self.max_documents:
            raise ValueError("document catalogue exceeds limit")
        if media_type in {
            "text/plain",
            "text/markdown",
            "text/csv",
            "application/json",
        }:
            text = data.decode("utf-8-sig")
        else:
            result = await _process(
                (
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("_document_worker.py")),
                ),
                token,
                data=json.dumps(
                    {"mediaType": media_type, "data": base64.b64encode(data).decode()}
                ).encode(),
                timeout=30,
                max_bytes=4 * 1024 * 1024,
            )
            if result.exit_code != 0:
                raise ValueError("document parsing failed or exceeded limits")
            text = json.loads(result.stdout)["text"]
        if not text.strip() or len(text) > 250_000:
            raise ValueError("document text is empty or exceeds extraction limit")
        chunks = [
            text[index : index + self.chunk_characters]
            for index in range(0, len(text), self.chunk_characters)
        ]
        if len(chunks) > 128:
            raise ValueError("document has too many chunks")
        token.throw_if_cancelled()
        vectors = await self._embed(chunks, token)
        checked = _vectors(vectors, len(chunks), self.embedding_provider.dimensions)
        token.throw_if_cancelled()
        if not await self._allowed(source_id, token):
            raise PermissionError("document authorization was revoked during ingestion")
        revision = content_digest(
            {
                "content": hashlib.sha256(data).hexdigest(),
                "mediaType": media_type,
                "sourceUri": source_uri,
                "embeddingVersion": self.embedding_version,
                "chunkCharacters": self.chunk_characters,
                "parserVersion": "1",
            }
        )
        payload = {
            "sourceId": source_id,
            "sourceUri": source_uri,
            "mediaType": media_type,
            "revision": revision,
            "embeddingVersion": self.embedding_version,
            "deleted": False,
            "chunks": [
                {"index": index, "text": chunk, "vector": vector}
                for index, (chunk, vector) in enumerate(
                    zip(chunks, checked, strict=True)
                )
            ],
        }
        if existing and existing[-1].payload == payload:
            return revision
        # Reserve catalogue capacity under a cross-process fenced claim. Two
        # concurrently ingested new sources must not both observe the last slot.
        journal, principal = self.records.journal, self.records.principal
        claim = None
        async with asyncio.timeout(10):
            while claim is None:
                token.throw_if_cancelled()
                claim = await journal.acquire_fenced_claim(
                    principal,
                    "document_catalog",
                    self.records.session_id,
                    str(uuid4()),
                    lease_seconds=30,
                )
                if claim is None:
                    await asyncio.sleep(0.02)
        try:
            if not existing and len(await self.records.all()) >= self.max_documents:
                raise ValueError("document catalogue exceeds limit")
            if not await self._allowed(source_id, token):
                raise PermissionError(
                    "document authorization was revoked before commit"
                )
            await self.records.append(
                source_id,
                "document_indexed",
                payload,
                expected_version=existing[-1].sequence if existing else -1,
                claim=claim,
            )
        finally:
            await await_owned_cleanup(journal.release_fenced_claim(principal, claim))
        return revision

    async def delete(
        self,
        source_id: str,
        *,
        authorized: bool = False,
        cancellation: CancellationToken | None = None,
    ) -> None:
        token = cancellation or CancellationToken()
        if authorized is not True or not await self._allowed(source_id, token):
            raise PermissionError(
                "document deletion requires explicit source authorization"
            )
        events = await self.records.read(source_id)
        if not events or events[-1].payload.get("deleted"):
            return
        await self.records.append(
            source_id,
            "document_deleted",
            {"sourceId": source_id, "deleted": True},
            expected_version=events[-1].sequence,
        )

    async def search(
        self, query: str, cancellation: CancellationToken, *, top_k: int = 5
    ) -> tuple[dict[str, Any], ...]:
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query.encode()) > 16_384
            or type(top_k) is not int
            or not 0 < top_k <= 50
        ):
            raise ValueError("invalid document query or result limit")
        cancellation.throw_if_cancelled()
        records = await self.records.all()
        if len(records) > self.max_documents:
            raise ValueError("document catalogue exceeds configured limit")
        query_vector = _vectors(
            await self._embed([query], cancellation),
            1,
            self.embedding_provider.dimensions,
        )[0]
        matches = []
        for source_id, events in records.items():
            cancellation.throw_if_cancelled()
            current = events[-1].payload
            if current.get("deleted") or not await self._allowed(
                source_id, cancellation
            ):
                continue
            if current.get("embeddingVersion") != self.embedding_version:
                raise ValueError(
                    "embedding model changed; reindex documents before retrieval"
                )
            for chunk in current["chunks"]:
                vector = _vectors(
                    [chunk["vector"]], 1, self.embedding_provider.dimensions
                )[0]
                score = _cosine(query_vector, vector)
                matches.append(
                    {
                        "citation": f"{source_id}@{current['revision']}#{chunk['index']}",
                        "sourceId": source_id,
                        "sourceUri": current["sourceUri"],
                        "revision": current["revision"],
                        "text": chunk["text"],
                        "score": score,
                    }
                )
        matches.sort(key=lambda item: (-item["score"], item["citation"]))
        return tuple(matches[:top_k])

    async def _embed(self, texts: list[str], token: CancellationToken) -> Any:
        token.throw_if_cancelled()
        work = asyncio.ensure_future(self.embedding_provider.embed(texts))
        cancelled = asyncio.create_task(token.wait())
        try:
            await asyncio.wait({work, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            token.throw_if_cancelled()
            return await work
        finally:
            if not work.done():
                work.cancel()
            cancelled.cancel()
            await await_owned_cleanup(
                asyncio.gather(work, cancelled, return_exceptions=True)
            )

    async def resolve_citation(
        self, citation: str, cancellation: CancellationToken
    ) -> dict[str, Any] | None:
        match = re.fullmatch(
            r"([a-zA-Z0-9_.-]{1,160})@([a-f0-9]{64})#([0-9]{1,4})", citation
        )
        if match is None:
            return None
        source_id, revision, index = match.groups()
        if not await self._allowed(source_id, cancellation):
            return None
        events = await self.records.read(source_id)
        if not events:
            return None
        current = events[-1].payload
        if current.get("deleted") or current.get("revision") != revision:
            return None
        chunks = current["chunks"]
        if int(index) >= len(chunks):
            return None
        return {
            "citation": citation,
            "sourceUri": current["sourceUri"],
            "text": chunks[int(index)]["text"],
            "revision": revision,
        }

    def create_search_tool(self) -> AgentTool:
        def validate(value: Any) -> dict[str, Any]:
            args = object_args(value, allowed={"query"}, required={"query"})
            return {"query": text_arg(args["query"], "query")}

        async def execute(
            call_id: str,
            args: dict[str, Any],
            token: CancellationToken,
            update: ToolUpdateCallback,
        ) -> AgentToolResult:
            results = await self.search(args["query"], token)
            return AgentToolResult(
                content=[
                    {"type": "text", "text": json.dumps(results, ensure_ascii=False)}
                ],
                details={"sources": list(results)},
            )

        return AgentTool(
            name="knowledge_search",
            label="检索授权资料",
            description="对已授权、当前有效版本的资料进行语义检索，返回可用于报告引用的 citation。",
            execute=execute,
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "需要查找的信息"}
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            validate_args=validate,
            execution_mode="parallel",
            replay_policy="safe",
            timeout_seconds=180,
            security_policy_version=hashlib.sha256(
                f"{self.records.principal.tenant_id}:{self.records.session_id}:{self.embedding_version}:{self.chunk_characters}:parser1".encode()
            ).hexdigest(),
        )


def _vectors(values: Any, count: int, dimensions: int) -> list[list[float]]:
    if len(values) != count:
        raise ValueError("embedding batch length mismatch")
    result = []
    for vector in values:
        if len(vector) != dimensions or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            for item in vector
        ):
            raise ValueError("embedding vector dimensions or values are invalid")
        result.append([float(item) for item in vector])
    return result


def _cosine(a: list[float], b: list[float]) -> float:
    magnitude = math.sqrt(
        sum(value * value for value in a) * sum(value * value for value in b)
    )
    return (
        sum(x * y for x, y in zip(a, b, strict=True)) / magnitude if magnitude else 0.0
    )
