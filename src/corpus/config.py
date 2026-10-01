"""Runtime configuration, sourced from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from ``CORPUS_``-prefixed environment variables."""

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
    # Model for the secret audit. Empty => the enrichment model. Set it apart when
    # enrichment runs on a remote endpoint: the audit reads the very secrets the
    # egress gate redacts, so it belongs on a local model. Both calls go to the same
    # OpenAI-compatible base, so locality is the gateway's model-name routing: name
    # a model the gateway serves on-prem.
    audit_model: str = ""
    enrich_timeout: float = 120.0
    # Concurrent in-flight enrichment requests; the local server batches them, so a
    # multi-hour sequential backfill becomes a few hours. 1 = fully sequential.
    enrich_concurrency: int = 8
    # Riding out a busy endpoint: attempts per request (the first included) and the
    # cap on the exponential backoff between them. A saturated server sheds load as
    # 5xx and returns within minutes, so the defaults wait out roughly four minutes
    # of that rather than aborting a whole backfill mid-pass.
    enrich_retries: int = 10
    enrich_retry_max_wait: float = 60.0
    # Character cap on one enrichment prompt; 0 = unlimited (send the whole body).
    # On a KV-cache-bound backend long bodies inflate prefill and cache use until
    # the server preempts; capping trades tail-of-body fidelity for throughput. Only
    # the enrichment call is capped — the secret audit always sees the full text.
    enrich_max_input_chars: int = 0
    # Per-model request options, as JSON: {"<model>": {...}}. Keys:
    #   inline_schema_refs  inline the response schema's $refs (llama.cpp cannot
    #                       resolve the nested refs msgspec emits, and silently
    #                       drops the grammar)
    #   extra_body          fields merged into the request, e.g. reasoning_effort
    #   context_tokens      the model's per-request context, to budget inputs
    # A model with no entry is called exactly as before.
    model_options: dict[str, dict] = {}

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

    # scan-gate: the egress policy (corpus.egress) inline on the path to model
    # providers, behind a gateway protocol adapter.
    scan_gate_adapter: str = "envoy-ext-proc"
    scan_gate_port: int = 9002
    scan_gate_workers: int = 8
    # Fail-open passes an unredactable body through unchanged (log-only, for a
    # Phase-2 shadow rollout); the default fails closed — an unparseable or
    # erroring body is blocked rather than leaked.
    scan_gate_fail_open: bool = False
    # Comma-separated secret types that force an outright 403 block instead of
    # redaction. Kept small and high-confidence.
    scan_gate_block_types: str = "private_key"
    # Comma-separated model names passed unscanned because they are served
    # locally. Exact names only, so a typo can only make the gate scan more.
    scan_gate_skip_models: str = ""
    # Comma-separated client ids (the gateway's x-client-id) of batch jobs, and the
    # largest body they may send to a scanned model; bigger bodies get a 413.
    scan_gate_batch_clients: str = ""
    scan_gate_batch_max_bytes: int = 65536
    # ext_proc transport: the largest gRPC message the gate accepts and returns.
    # With the request body in Buffered mode, the proxy sends the whole body as
    # one message, so this must be at least the proxy's body buffer limit. grpc's
    # 4 MiB default refuses a chat body carrying an inline image.
    scan_gate_grpc_max_message_bytes: int = 50 * 1024 * 1024

    # corpus-index: the sanitized query surface. Connects as a restricted DB role
    # (corpus_index_ro) that reads only the sanitized DB, never a raw body — the
    # trust gate for cloud-model consumers. Empty => index disabled.
    index_database_url: str = ""
    # The sanitized DB the one-way sync WRITES to (corpus_app @ ai_sanitized). The
    # projection drops raw content/subject/sender; only cloud-safe fields land here.
    # Empty => sync disabled.
    sanitized_database_url: str = ""
    # Schema of the sanitized tier's ``messages`` table (sync writes it, the index
    # reads it). Empty means ``db_schema``. Set it when several raw schemas (one
    # per mailbox) project into one sanitized view.
    sanitized_db_schema: str = ""
    # sensitivity_level at/above which richer summary detail (abstract, key_points)
    # is withheld from the sanitized surface. one_line + classification still shown.
    index_sensitivity_gate: str = "high"


settings = Settings()
