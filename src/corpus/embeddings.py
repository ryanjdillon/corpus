"""Embedding client for an OpenAI-compatible endpoint.

Kept deliberately thin (a direct httpx call rather than an SDK) so the request
body is explicit and free of client-injected fields. Retries transient failures
(timeouts, 5xx) since a remote embedder can be slow under load.
"""

from __future__ import annotations

import time

import httpx

from .config import settings

_RETRIES = 4


class EmbedInputError(Exception):
    """A non-retryable client error (4xx) from the embedding endpoint: the input
    itself was rejected (e.g. too long). Callers can isolate and skip the
    offending record rather than abort a whole batch."""


class EmbedUnavailableError(Exception):
    """The embedding endpoint is unavailable (5xx or transport error) after all
    retries — a systemic failure, not a bad record. Callers should abort (and
    resume later) rather than skip records."""


class Embedder:
    def __init__(self) -> None:
        self._client = httpx.Client(
            base_url=settings.openai_api_base,
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=settings.embed_timeout,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = {
            "model": settings.embedding_model,
            "input": texts,
            "encoding_format": "float",
        }
        last: Exception | None = None
        for attempt in range(_RETRIES):
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
            time.sleep(min(2**attempt, 20))
        assert last is not None
        raise EmbedUnavailableError(str(last)) from last

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def close(self) -> None:
        self._client.close()
