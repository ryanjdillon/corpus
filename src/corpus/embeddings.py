"""Embedding client for an OpenAI-compatible endpoint.

Kept deliberately thin (a direct httpx call rather than an SDK) so the request
body is explicit and free of client-injected fields.
"""

from __future__ import annotations

import httpx

from .config import settings


class Embedder:
    def __init__(self) -> None:
        self._client = httpx.Client(
            base_url=settings.openai_api_base,
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=60.0,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.post(
            "/embeddings",
            json={
                "model": settings.embedding_model,
                "input": texts,
                "encoding_format": "float",
            },
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return [row["embedding"] for row in data]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def close(self) -> None:
        self._client.close()
