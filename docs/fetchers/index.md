# Sources

A *source* is anything that yields `Record`s. Because the rest of the pipeline
(classify → embed → store → search) only sees `Record`s, adding a source — a new
mailbox protocol, a document store, a chat export — is self-contained: implement
one small protocol and register it. Email ships today over [IMAP](imap.md) and
[Gmail](gmail.md); the same shape covers documents. Each configured source is
one entry in the config file (see [Source configuration](config.md)).

## The protocol

A fetcher satisfies `fetchers.base.Fetcher`:

```python
class Fetcher(Protocol):
    source: str  # e.g. "files:notes"

    def fetch(
        self, cursor: str | None, known: Container[str] = frozenset()
    ) -> Iterator[Record]:
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
- `known` holds the keys (`Record.key()`, i.e. `source::source_uid`) already in
  the store. A fetcher should skip *downloading* those items when it can tell
  their key before fetching them. Then a run that resumes a partly stored page
  costs a listing, not a re-download of everything it already has, which
  matters for providers with per-user quotas (Gmail throttles bursts of raw
  downloads). Ignoring `known` is correct, just slower; ingest skips stored keys
  again before writing.

## Register it

The kinds are a closed set, declared once in `corpus.sources`: one `Source`
subclass per kind, tagged by `kind`, with that kind's `options`. Adding a kind
adds a subclass, puts it in `SourceEntry`, and adds a case to
`sources.build_fetcher`:

```python
class FilesOptions(_Strict):
    root: str | None = None


class FilesSource(Source, tag="files"):
    kind: ClassVar[str] = "files"
    options: FilesOptions = FilesOptions()


def build_fetcher(source: Source, *, environ=os.environ) -> Fetcher:
    match source:
        case FilesSource():
            from ..fetchers.files import FilesFetcher
            return FilesFetcher(source, environ=environ)
        ...
```

The fetcher takes its `Source` and reads any secret from `environ` through
`source.env_name(KEY)`, under the source's `credentials_env_prefix`, never from a
name it composes itself. Then a `files:notes` entry in the config file routes
`corpus ingest files:notes` to your fetcher, and several sources of one kind
coexist, each with its own prefix. Message ids of a new kind also need a uid
grammar in `models.parse_id` (see [Message ids](../message-ids.md)).

A mail fetcher maps each raw RFC 822 message with
`sources.rfc822.record_from_rfc822`, passing only what the provider knows (its
id, thread, labels, folder, link), so every mail source reads subjects, dates,
addresses and bodies the same way.

`corpus.fetchers.build_fetcher("<kind>:<name>")`, the older entry point by id,
still works for one release and warns; it resolves the id through the configured
sources.

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
