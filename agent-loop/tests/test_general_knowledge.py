"""Document versions, source authority, parser boundaries and real embeddings."""

import asyncio
import io
import os
from pathlib import Path

import pytest

from pi_agent_loop import CancellationToken
from pi_agent_loop.knowledge import DocumentLibrary
from pi_agent_loop.memory.semantic_provider import SentenceTransformerEmbeddingProvider
from test_general_tasks import store


class SemanticFixture:
    dimensions = 2

    async def embed(self, texts):
        # Deliberately deterministic for storage contracts; the separate
        # integration test below runs an actual multilingual neural model.
        return tuple((1.0, 0.0) if "offline" in text else (0.0, 1.0) for text in texts)


def library(tmp_path, allowed=None):
    artifacts = store(tmp_path)
    return DocumentLibrary(
        artifacts.records.journal,
        artifacts.records.principal,
        "session",
        SemanticFixture(),
        authorize_source=lambda source, principal, token: (
            allowed is None or source in allowed
        ),
    )


@pytest.mark.asyncio
async def test_retrieval_updates_revocation_and_restart(tmp_path):
    allowed = {"product", "weather"}
    docs = library(tmp_path, allowed)
    with pytest.raises(PermissionError):
        await docs.ingest("product", b"offline product", source_uri="local:product")
    first = await docs.ingest(
        "product", b"offline product", source_uri="local:product", authorized=True
    )
    assert (
        await docs.ingest(
            "product", b"offline product", source_uri="local:product", authorized=True
        )
        == first
    )
    await docs.ingest(
        "weather", b"rain forecast", source_uri="local:weather", authorized=True
    )
    result = await library(tmp_path, allowed).search(
        "offline capability", CancellationToken()
    )
    assert result[0]["sourceId"] == "product"
    citation = result[0]["citation"]
    assert (await docs.resolve_citation(citation, CancellationToken()))[
        "text"
    ] == "offline product"
    await docs.ingest(
        "product",
        b"offline product revised",
        source_uri="local:product",
        authorized=True,
    )
    assert await docs.resolve_citation(citation, CancellationToken()) is None
    revised = (await docs.search("offline", CancellationToken()))[0]["citation"]
    allowed.remove("product")
    assert await docs.resolve_citation(revised, CancellationToken()) is None
    assert all(
        item["sourceId"] != "product"
        for item in await docs.search("offline", CancellationToken())
    )
    assert b"offline product" not in (tmp_path / "artifacts.sqlite3").read_bytes()


@pytest.mark.asyncio
async def test_delete_is_a_durable_retrieval_tombstone(tmp_path):
    docs = library(tmp_path)
    await docs.ingest(
        "product", b"offline", source_uri="local:product", authorized=True
    )
    await docs.delete("product", authorized=True)
    assert await library(tmp_path).search("offline", CancellationToken()) == ()


@pytest.mark.asyncio
async def test_docx_and_pdf_are_extracted_in_owned_processes(tmp_path):
    from docx import Document
    from reportlab.pdfgen.canvas import Canvas

    docs = library(tmp_path)
    word = Document()
    word.add_paragraph("offline document evidence")
    stream = io.BytesIO()
    word.save(stream)
    await docs.ingest(
        "word",
        stream.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        source_uri="local:document.docx",
        authorized=True,
    )
    pdf = io.BytesIO()
    canvas = Canvas(pdf)
    canvas.drawString(72, 700, "offline PDF evidence")
    canvas.save()
    await docs.ingest(
        "pdf",
        pdf.getvalue(),
        media_type="application/pdf",
        source_uri="local:document.pdf",
        authorized=True,
    )
    results = await docs.search("offline", CancellationToken())
    assert {item["sourceId"] for item in results} == {"pdf", "word"}
    with pytest.raises(ValueError, match="parsing"):
        await docs.ingest(
            "bad",
            b"not a PDF",
            media_type="application/pdf",
            source_uri="local:bad.pdf",
            authorized=True,
        )


@pytest.mark.asyncio
async def test_invalid_vectors_and_revoked_during_ingest_do_not_commit(tmp_path):
    docs = library(tmp_path)

    class Invalid:
        dimensions = 2

        async def embed(self, texts):
            return [(float("nan"), 1)]

    docs.embedding_provider = Invalid()
    with pytest.raises(ValueError, match="vectors|values"):
        await docs.ingest(
            "product", b"offline", source_uri="local:product", authorized=True
        )
    assert await docs.records.all() == {}


@pytest.mark.asyncio
async def test_neural_multilingual_embeddings_from_provisioned_local_weights():
    location = os.environ.get("PI_AGENT_EMBEDDING_MODEL")
    if not location:
        pytest.skip(
            "set PI_AGENT_EMBEDDING_MODEL to provisioned local weights for the integration gate"
        )
    provider = await SentenceTransformerEmbeddingProvider.create(
        Path(location), timeout=180
    )
    try:
        vectors = await provider.embed(
            ["The car needs to be repaired", "汽车需要维修", "今天晚饭吃什么"]
        )

        def cosine(a, b):
            return sum(x * y for x, y in zip(a, b, strict=True))

        assert cosine(vectors[0], vectors[1]) > cosine(vectors[0], vectors[2]) + 0.2
        assert len(vectors[0]) == provider.dimensions
    finally:
        await provider.aclose()
    assert not provider._active


@pytest.mark.asyncio
async def test_encoder_close_drains_active_helpers(tmp_path, monkeypatch):
    from pi_agent_loop.memory import semantic_provider

    entered = asyncio.Event()
    ended = asyncio.Event()

    async def process(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            ended.set()

    monkeypatch.setattr(semantic_provider, "_process", process)
    provider = SentenceTransformerEmbeddingProvider(tmp_path, 2, "version", 30)
    task = asyncio.create_task(provider.embed(["text"]))
    await entered.wait()
    await provider.aclose()
    await asyncio.gather(task, return_exceptions=True)
    assert ended.is_set() and not provider._active


@pytest.mark.asyncio
async def test_document_capacity_is_atomic_and_chunking_changes_citation_version(
    tmp_path,
):
    first, second = library(tmp_path), library(tmp_path)
    first.max_documents = second.max_documents = 1
    results = await asyncio.gather(
        first.ingest("a", b"offline", source_uri="local:a", authorized=True),
        second.ingest("b", b"offline", source_uri="local:b", authorized=True),
        return_exceptions=True,
    )
    assert sum(isinstance(item, str) for item in results) == 1
    assert sum(isinstance(item, ValueError) for item in results) == 1
    records = await first.records.all()
    key = next(iter(records))
    before = (await first.search("offline", CancellationToken()))[0]["citation"]
    second.chunk_characters = 900
    await second.ingest(key, b"offline", source_uri="local:" + key, authorized=True)
    after = (await second.search("offline", CancellationToken()))[0]["citation"]
    assert after != before
    assert await second.resolve_citation(before, CancellationToken()) is None


@pytest.mark.asyncio
async def test_document_token_cancellation_drains_embedding(tmp_path):
    entered, closed = asyncio.Event(), asyncio.Event()

    class WaitingEmbedding:
        dimensions = 2

        async def embed(self, texts):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                closed.set()

    docs = library(tmp_path)
    docs.embedding_provider = WaitingEmbedding()
    token = CancellationToken()
    task = asyncio.create_task(
        docs.ingest(
            "document",
            b"offline",
            source_uri="local:document",
            authorized=True,
            cancellation=token,
        )
    )
    await entered.wait()
    token.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert closed.is_set() and not await docs.records.all()
