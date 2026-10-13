"""Embedding client for an OpenAI-compatible endpoint.

Kept deliberately thin (a direct httpx call rather than an SDK) so the request
body is explicit and free of client-injected fields. Batch embedding retries
transient failures (timeouts, 5xx) since a remote embedder can be slow under
load; a query embedding, which a tool call waits on, makes one short attempt.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from .config import settings

_RETRIES = 4


class EmbedInputError(Exception):
    """A non-retryable client error (4xx) from the embedding endpoint.

    The input itself was rejected (e.g. too long). Callers can isolate and skip the
    offending record rather than abort a whole batch.
    """


class EmbedUnavailableError(Exception):
    """The embedding endpoint is unavailable (5xx or transport error) after all retries.

    A systemic failure, not a bad record. Callers should abort (and resume later)
    rather than skip records.
    """


class Embedder:
    """Embed text via an OpenAI-compatible endpoint.

    The defaults suit batch work (ingest, the sanitized sync): a generous timeout
    and several attempts with backoff. An interactive caller passes fewer
    attempts and a shorter timeout, as :func:`embed_query` does.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        attempts: int = _RETRIES,
        timeout: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client or httpx.Client(
            base_url=settings.openai_api_base,
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=settings.embed_timeout if timeout is None else timeout,
        )
        self._attempts = max(1, attempts)
        self._sleep = sleep

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return an embedding vector for each input text."""
        if not texts:
            return []
        payload = {
            "model": settings.embedding_model,
            "input": texts,
            "encoding_format": "float",
        }
        last: Exception | None = None
        for attempt in range(self._attempts):
            try:
                resp = self._client.post("/embeddings", json=payload)
                resp.raise_for_status()
                return [row["embedding"] for row in resp.json()["data"]]
            except httpx.HTTPStatusError as exc:
                # Client errors (4xx) are not transient: the input was rejected.
                # Signal it distinctly so the caller can skip the offending record.
                if exc.response.status_code < 500:
                    raise EmbedInputError(
                        f"{exc.response.status_code}: {exc.response.text[:200]}"
                    ) from exc
                last = exc
            except httpx.TransportError as exc:  # timeouts, connection resets
                last = exc
            if attempt + 1 < self._attempts:  # no wait once the last attempt failed
                self._sleep(min(2**attempt, 20))
        assert last is not None
        raise EmbedUnavailableError(str(last)) from last

    def embed_one(self, text: str) -> list[float]:
        """Return the embedding vector for a single text."""
        return self.embed([text])[0]

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()


def query_embedder(client: httpx.Client | None = None) -> Embedder:
    """An embedder for a query a tool call waits on.

    It makes one attempt within ``settings.embed_query_timeout`` rather than the
    batch retry policy, so an unavailable embedder fails the call in seconds, not
    minutes.
    """
    return Embedder(client, attempts=1, timeout=settings.embed_query_timeout)


def embed_query(text: str, *, client: httpx.Client | None = None) -> list[float]:
    """Embed one query text with the configured model, failing fast (see :func:`query_embedder`).

    The same endpoint and model embed what the stores hold (document bodies at
    ingest, summaries at the sanitized sync), so a query vector is comparable to
    either tier's embeddings.
    """
    embedder = query_embedder(client)
    try:
        return embedder.embed_one(text)
    finally:
        embedder.close()
