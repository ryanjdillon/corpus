"""Runtime configuration, sourced from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CORPUS_", extra="ignore")

    # Postgres (pgvector). The DB user owns a dedicated schema.
    database_url: str = "postgresql://corpus_app@localhost:5432/ai"
    db_schema: str = "corpus"
    documents_table: str = "documents"

    # Canonical raw vault (local-only volume): one markdown file per document.
    vault_path: str = "/data/vault"

    # Any endpoint serving the OpenAI embeddings API — a locally hosted model or
    # a cloud provider.
    openai_api_base: str = "http://localhost:8080/v1"
    openai_api_key: str = ""
    embedding_model: str = "local-embed"
    embedding_dimensions: int = 1024
    # Generous: a slow endpoint may take a while for a batch under load.
    embed_timeout: float = 300.0

    # Optional local model used only to break low-confidence classification ties.
    classify_model: str = ""  # empty => rule + prototype classification only

    # Local model for batch enrichment (structured per-message summary +
    # classification via guided decoding). Empty => enrichment disabled.
    enrich_model: str = ""
    enrich_timeout: float = 120.0
    # Concurrent in-flight enrichment requests; the local server batches them, so a
    # multi-hour sequential backfill becomes a few hours. 1 = fully sequential.
    enrich_concurrency: int = 8

    # External credential scanner (Betterleaks). Empty => local regexes only; set to
    # the binary name/path to union in its full ruleset (the image sets this).
    leaks_bin: str = ""
    leaks_timeout: float = 30.0

    # Chunking for long bodies.
    chunk_tokens: int = 512
    chunk_overlap: int = 64

    # HTTP service.
    host: str = "0.0.0.0"
    port: int = 8000
    mcp_port: int = 9000


settings = Settings()
