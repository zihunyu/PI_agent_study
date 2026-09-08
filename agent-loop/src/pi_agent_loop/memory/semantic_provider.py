"""Real local semantic embeddings in a cancellable, bounded subprocess."""

from __future__ import annotations

import asyncio
import json
import math
import sys
from pathlib import Path
from typing import Any
from collections.abc import Sequence

from ..cancellation import CancellationToken
from ..execution import _process


class SentenceTransformerEmbeddingProvider:
    """Load operator-provisioned local weights; no remote code or model download.

    The separate process prevents a cancelled encoder from retaining a background
    Python thread/GPU job. CPU inference is the predictable default.
    """

    def __init__(
        self, model_path: Path, dimensions: int, fingerprint: str, timeout: float
    ) -> None:
        self.model_path = model_path
        self.dimensions = dimensions
        self.fingerprint = fingerprint
        self.timeout = timeout
        self._active: set[asyncio.Task[Any]] = set()
        self._closed = False

    @classmethod
    async def create(
        cls, model_path: str | Path, *, timeout: float = 120
    ) -> SentenceTransformerEmbeddingProvider:
        path = Path(model_path).resolve(strict=True)
        if not path.is_dir():
            raise ValueError("embedding model must be a provisioned local directory")
        provider = cls(path, 0, "", timeout)
        try:
            raw = await provider._request([], probe=True)
            dimensions = raw["dimensions"]
            if type(dimensions) is not int or not 0 < dimensions <= 4096:
                raise ValueError("embedding model dimensions exceed supported limits")
            provider.dimensions = dimensions
            provider.fingerprint = raw["fingerprint"]
            return provider
        except BaseException:
            await provider.aclose()
            raise

    async def _request(
        self, texts: list[str], *, probe: bool = False
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("embedding provider is closed")
        payload = {
            "modelPath": str(self.model_path),
            "texts": texts,
            "probe": probe,
            "fingerprint": self.fingerprint,
        }
        task = asyncio.create_task(
            _process(
                (
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("_semantic_worker.py")),
                ),
                CancellationToken(),
                data=json.dumps(payload).encode(),
                timeout=self.timeout,
                max_bytes=8 * 1024 * 1024,
            ),
            name="semantic-embedding-process",
        )
        self._active.add(task)
        try:
            result = await task
            if result.exit_code != 0:
                raise RuntimeError(
                    "local semantic model failed; install the knowledge extra and provision compatible weights"
                )
            return json.loads(result.stdout)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._active.discard(task)

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if (
            isinstance(texts, str | bytes)
            or not 0 < len(texts) <= 128
            or any(not isinstance(text, str) or not text.strip() for text in texts)
        ):
            raise ValueError("embedding batch requires 1-128 non-empty texts")
        if sum(len(text.encode()) for text in texts) > 1_048_576:
            raise ValueError("embedding batch exceeds input limit")
        raw = await self._request(list(texts))
        vectors = raw.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("embedding model returned an invalid batch")
        result = []
        for vector in vectors:
            if (
                not isinstance(vector, list)
                or len(vector) != self.dimensions
                or any(
                    isinstance(item, bool)
                    or not isinstance(item, (int, float))
                    or not math.isfinite(item)
                    for item in vector
                )
            ):
                raise ValueError("embedding model returned invalid vectors")
            result.append(tuple(float(item) for item in vector))
        return tuple(result)

    async def aclose(self) -> None:
        self._closed = True
        for task in tuple(self._active):
            task.cancel()
        await asyncio.gather(*tuple(self._active), return_exceptions=True)
