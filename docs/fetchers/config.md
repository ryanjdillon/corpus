# Source configuration

Each mailbox corpus ingests is one entry under `sources:` in the config file
(`CORPUS_CONFIG_FILE`), the same file that holds the [tiers](../tiers.md). The
entry says what kind of source it is, where its credentials are, whether its
documents may be enriched, what to fetch, and the defaults for
[`corpus pipeline`](../pipeline.md). `corpus.sources` loads it; `corpus ingest`,
`corpus enrich` and `corpus pipeline` take everything about a source from it.

```yaml
sources:
  - name: personal
    kind: gmail
    enrich: true
    options: {labels: [INBOX, Receipts]}
  - name: inbox-a
    kind: imap
    credentials_env_prefix: MAIL_INBOX_A
    enrich: true
    options: {folders: [INBOX, Archive]}
    pipeline: {limit: 400, tier: sanitized}
```

## Keys

| Key | Meaning |
| --- | --- |
| `name` | The mailbox name: 1–32 of `a-z`, `0-9`, `_` and `-`. |
| `kind` | `gmail` or `imap`; any other kind is refused. |
| `enrich` | Required. Whether the source's documents may be enriched (see [Enrichment policy](policy.md)). |
| `credentials_env_prefix` | The prefix of the source's credential variables, `[A-Z_][A-Z0-9_]*`. Default: `CORPUS_<KIND>_<NAME>`. |
| `options` | What to fetch; the keys depend on the kind (below). |
| `pipeline.batch_size` | Records ingest embeds per request and writes per batch (default `50`). `corpus ingest --batch-size` and `corpus pipeline --batch-size` override it. |
| `pipeline.limit` | At most this many documents enriched, and new or re-enriched rows embedded by sync, per run (sync still re-projects every stored row); `0` is no limit (default). `corpus pipeline --limit` overrides it. |
| `pipeline.tier` | The projected tier sync writes (default `sanitized`). `corpus pipeline --tier` overrides it. |

| Kind | `options` key | Meaning |
| --- | --- | --- |
| `gmail` | `labels` | Label names to fetch. Unset: `<prefix>_LABELS`, and all mail if that is unset too. |
| `imap` | `folders` | Folders to fetch. Unset: `<prefix>_FOLDERS`, and every selectable folder if that is unset too. |

A source's id is `<kind>:<name>`, e.g. `gmail:personal`. It is the argument to
`corpus ingest` and `corpus pipeline`, the `--source` filter of the other
commands, and the prefix of every [message id](../message-ids.md) the source
stores. The same name under two kinds is two sources.

## Credentials stay in the environment

The file holds no secret. A source's credentials are read from the variables
under its prefix, so they keep their injection path (a Kubernetes secret, a
systemd credential) and the file can be committed and reviewed.

| Kind | Variables under the prefix |
| --- | --- |
| `gmail` | `_CLIENT_ID`, `_CLIENT_SECRET`, `_REFRESH_TOKEN`; optionally `_LABELS` |
| `imap` | `_HOST`, `_USER`, `_PASSWORD`; optionally `_PORT` (`993`), `_SSL` (`true`), `_FOLDERS` |

The default prefix is the naming the fetchers used before sources were
configured: the kind and the name, upper-cased, so `imap:example` reads
`CORPUS_IMAP_EXAMPLE_HOST`. A deployment's existing variables therefore keep
working when it adds a `sources:` entry without a prefix. A name with a `-`
keeps the `-` in its default prefix (`CORPUS_IMAP_INBOX-B_HOST`), which most
shells cannot set; give such a source an explicit `credentials_env_prefix`.

An explicit prefix replaces the default: `imap:inbox-a` above reads
`MAIL_INBOX_A_HOST` and never `CORPUS_IMAP_INBOX_A_HOST`. A missing credential
fails when the fetcher is built, with an error that names the variables.

These are the fetcher's mailbox credentials only. The keys for the embedding
and enrichment endpoints (`CORPUS_EMBED_API_KEY`, `CORPUS_ENRICH_API_KEY`,
falling back to `CORPUS_OPENAI_API_KEY`; see
[Endpoints and keys](../configuration.md#endpoints-and-keys)) are per process,
not per source, and are never read under a source's prefix.

## Validation

The whole file is checked when a command first reads it — its tiers and its
sources alike, so `serve` and `sync`, which use only the tiers, still refuse a
file whose sources are invalid — and an invalid file stops the command with an
error that names the problem:

- an unknown key, at any level, is an error, so a `password:` in an entry is
  refused rather than ignored;
- `kind` must be `gmail` or `imap`, and `options` may hold only that kind's keys;
- `name` must match the message-id grammar, and `enrich` must be given;
- a source declared twice (the same kind and name) is an error;
- `labels` and `folders`, when given, must list at least one name;
  `pipeline.batch_size` must be at least 1 and `pipeline.limit` at least 0.

A source not in the file is refused by `ingest` and `pipeline`, and its stored
documents are not enrichable. `sources: []` declares no sources at all.

## Without `sources:` (deprecated)

A config file without `sources:`, or no config file at all, keeps the behaviour
from before sources were configured, so an existing deployment is unchanged:

- any `gmail:<name>` or `imap:<name>` with a valid name is a source, with the
  default prefix, no `options` and the default `pipeline`;
- enrichment is decided by kind: `gmail` and `imap` documents are enrichable,
  any other kind is not, whatever the name.

A process in this mode logs a deprecation warning once. To migrate, list each
source the deployment runs, with `enrich: true` for the mail sources, and move
any `_LABELS` or `_FOLDERS` into `options` if you want them reviewed with the
rest of the file. Once `sources:` is present, a source missing from it is no
longer ingested or enriched, so list every source whose documents are stored.

## The config file

`CORPUS_CONFIG_FILE` is the path of the file. `CORPUS_TIERS_FILE`, its name from
when it held only tiers, still works and logs a deprecation warning; setting both
to different paths is an error. A file may hold `tiers:`, `sources:` or both;
the part that is missing keeps its default.
