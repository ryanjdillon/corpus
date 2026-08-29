# Sources

A *source* is anything that yields `Record`s. Because the rest of the pipeline
(classify → embed → store → search) only sees `Record`s, adding a source — a new
mailbox protocol, a document store, a chat export — is self-contained: implement
one small protocol and register it. Email ships today over [IMAP](imap.md) and
[Gmail](gmail.md); the same shape covers documents.

## The protocol

A fetcher satisfies `fetchers.base.Fetcher`:

```python
class Fetcher(Protocol):
    source: str  # e.g. "files:notes"

    def fetch(self, cursor: str | None) -> Iterator[Record]:
        """Yield records newer than `cursor` (all records if cursor is None)."""

    def next_cursor(self) -> str | None:
        """Cursor to persist after a successful pass."""
```

- `fetch` yields `Record`s (`models.Record`); set at least `source`,
  `source_uid` (stable per item — `(source, source_uid)` is the identity used for
  dedup/overwrite), `kind`, and `body_text`. Populate whatever metadata the
  source has (subject, sender, timestamps, `labels`, `folder`, `uri`).
- The **cursor is opaque** to the pipeline — encode whatever lets the source
  resume incrementally (a timestamp, a page token, a provider history id, a
  per-folder map). It is persisted in `sync_state` and handed back on the next
  run. Return `None` to always do a full pass.

## Register it

Dispatch is by the `source` id's prefix in `fetchers/build_fetcher`:

```python
def build_fetcher(source: str) -> Fetcher:
    kind, _, name = source.partition(":")
    if kind == "files":
        from .files import FilesFetcher
        return FilesFetcher(name or "default")
    ...
```

Then `corpus ingest files:notes` routes to your fetcher. Namespacing config by
the fetcher `name` (as the existing fetchers do, e.g. `CORPUS_IMAP_<NAME>_*`)
lets several instances of one source type coexist.

## Guidelines

- **Keep it a source, nothing more.** Don't classify, embed, or write from a
  fetcher — just yield `Record`s. Ingestion handles batching, resilience, and
  storage uniformly for every source.
- **Make `source_uid` stable.** It is how re-runs overwrite instead of duplicate.
- **Make the cursor cheap to resume from** so a partial run doesn't rescan the
  whole source.
- **Test against the real dependency** where practical: the IMAP fetcher is
  tested against a containerized mail server via the Docker-SDK fixtures; prefer
  that over mocks for a new source with an external service.
