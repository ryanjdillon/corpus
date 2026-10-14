"""Purging stored mail from excluded IMAP folders, against the Docker Postgres tier.

The stores are real; the IMAP server is either a spec-bound client injected
through the fetcher (for a ``\\Junk``-flagged folder, which GreenMail cannot
mark) or a real GreenMail account (the command end to end).
"""

from __future__ import annotations

import contextlib
import logging
import socket
import textwrap
import threading
from unittest.mock import create_autospec

import psycopg
import pytest
from click.testing import CliRunner
from imapclient import IMAPClient

from corpus import cli, purge, sources, tiers, vault
from corpus.config import settings
from corpus.enrich_store import EnrichStore
from corpus.fetchers.imap import ImapFetcher
from corpus.models import Classification, Record
from corpus.sanitized_store import COLS, SanitizedStore
from corpus.sources import ImapSource
from corpus.store import DocumentStore, to_document

DIM = 16  # the pg fixture's embedding dimension (conftest.TEST_DIM)
USER = "user@example.org"

CONFIG = """\
sources:
  - {name: inbox-a, kind: imap, enrich: true}
  - {name: inbox-b, kind: imap, enrich: true}
  - {name: personal, kind: gmail, enrich: true}
"""

#: inbox-a's account: a SPECIAL-USE server with a junk folder under a local name.
LISTING = [
    ((b"\\HasNoChildren",), b"/", "INBOX"),
    ((b"\\HasNoChildren", b"\\Sent"), b"/", "Sent"),
    ((b"\\HasNoChildren", b"\\Junk"), b"/", "Quarantine"),
    ((b"\\HasNoChildren",), b"/", "Junk Mail"),
]


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """Point the process at a config file declaring *text*, reloading its registries."""

    def use(text: str = CONFIG) -> None:
        path = tmp_path / "corpus.yaml"
        path.write_text(textwrap.dedent(text))
        monkeypatch.setattr(settings, "config_file", str(path))
        monkeypatch.setattr(settings, "tiers_file", "")
        tiers.registry.cache_clear()
        sources.registry.cache_clear()

    yield use
    tiers.registry.cache_clear()
    sources.registry.cache_clear()


# --------------------------------------------------------------------------- #
# choosing the sources and tiers (no I/O)
# --------------------------------------------------------------------------- #
def test_every_declared_imap_source_is_purged_when_none_is_named(configured):
    configured()

    assert [s.id for s in purge.imap_sources(None)] == ["imap:inbox-a", "imap:inbox-b"]
    assert [s.id for s in purge.imap_sources("imap:inbox-b")] == ["imap:inbox-b"]


@pytest.mark.parametrize(
    ("config", "name", "message"),
    [
        (CONFIG, "gmail:personal", "not an IMAP source"),
        (CONFIG, "imap:unknown", "no source 'imap:unknown'"),
        ("sources: []\n", None, "declares no IMAP source"),
    ],
)
def test_a_source_that_cannot_be_purged_is_refused(configured, config, name, message):
    configured(config)

    with pytest.raises(ValueError, match=message):
        purge.imap_sources(name)


def test_an_unset_projected_tier_store_refuses_the_purge(configured, monkeypatch):
    configured()
    monkeypatch.delenv("CORPUS_SANITIZED_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="CORPUS_SANITIZED_DATABASE_URL not set"):
        purge.projected_tiers()
    result = CliRunner().invoke(cli.main, ["purge-excluded-folders", "--yes"])
    assert result.exit_code == 2
    assert "CORPUS_SANITIZED_DATABASE_URL not set" in result.output


def test_the_command_refuses_a_source_that_is_not_imap(configured):
    configured()

    result = CliRunner().invoke(cli.main, ["purge-excluded-folders", "gmail:personal"])

    assert result.exit_code == 2
    assert "not an IMAP source" in result.output


def test_vault_files_lists_only_files_that_exist(tmp_path):
    vault.write("imap:inbox-a::INBOX:7:1", "synthetic", {}, root=tmp_path)

    found = purge.vault_files(["imap:inbox-a::INBOX:7:1", "imap:inbox-a::INBOX:7:2"], tmp_path)

    assert found == [vault.vault_path("imap:inbox-a::INBOX:7:1", tmp_path)]


def test_a_vault_file_resolving_outside_the_vault_is_left_alone(tmp_path, caplog):
    root, outside = tmp_path / "vault", tmp_path / "elsewhere"
    inside_id, linked_id = "imap:inbox-a::Junk:7:1", "imap:inbox-b::Junk:7:1"
    vault.write(inside_id, "synthetic", {}, root=root)
    outside.mkdir()
    (root / "imap" / "inbox-b").symlink_to(outside, target_is_directory=True)
    vault.write(linked_id, "synthetic", {}, root=root)
    documents = create_autospec(DocumentStore, instance=True)
    documents.delete.return_value = {"documents": 2, "enrichments": 0}
    plan = purge.Plan("imap:inbox-a", {"Junk": (inside_id, linked_id)})

    with caplog.at_level(logging.WARNING, logger="corpus.purge"):
        counts = purge.apply([plan], documents, {}, vault_root=root)

    assert counts == {"vault": 1, "documents": 2, "enrichments": 0}
    assert vault.read(inside_id, root) is None
    assert vault.read(linked_id, root) is not None
    assert "resolves outside the vault" in caplog.text
    documents.delete.assert_called_once_with([inside_id, linked_id])


# --------------------------------------------------------------------------- #
# the stores (Docker Postgres)
# --------------------------------------------------------------------------- #
def _vec(i: int) -> list[float]:
    return [1.0 if d == i % DIM else 0.0 for d in range(DIM)]


def _seed(dsn: str, source: str, folders: dict[str, int]) -> list[str]:
    """Store *n* synthetic documents per folder of *source*, enriched and projected."""
    docs = []
    for folder, n in folders.items():
        for uid in range(1, n + 1):
            record = Record(
                source=source, source_uid=f"{folder}:7:{uid}", kind="email",
                account="inbox-a@example.org", folder=folder,
                from_addr="sender@example.org", subject=f"synthetic note {uid}",
                body_text=f"an invented message {uid} in {folder}",
            )
            docs.append(to_document(record, Classification(label="personal", confidence=0.9),
                                    _vec(len(docs))))
    with DocumentStore() as documents:
        documents.write(docs)
    with EnrichStore() as enrichments:
        for d in docs:
            enrichments.save_enrichment(d.id, {"one_line": "invented"}, "test-model", "v1")
            enrichments.save_audit(d.id, ["api_key"], {"verdict": "none"}, "test-model", "s1")
    with SanitizedStore(dsn, "corpus") as store:
        for i, d in enumerate(docs):
            store.save_message({c: None for c in COLS} | {"id": d.id, "source": source}, _vec(i))
    return [d.id for d in docs]


def _stored(dsn: str) -> dict[str, set[str]]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT id FROM corpus.{settings.documents_table}")
        documents = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT doc_id FROM corpus.enrichments")
        enrichments = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT id FROM corpus.messages")
        messages = {r[0] for r in cur.fetchall()}
    return {"documents": documents, "enrichments": enrichments, "messages": messages}


@pytest.fixture
def stores(pg, configured, monkeypatch, tmp_path):
    """The test database as both the sensitive and the sanitized tier's store."""
    configured()
    monkeypatch.setenv("CORPUS_SANITIZED_DATABASE_URL", pg)
    monkeypatch.setattr(settings, "vault_path", str(tmp_path / "vault"))
    return pg


@pytest.fixture
def client():
    m = create_autospec(IMAPClient, instance=True)
    m.__enter__.return_value = m
    m.list_folders.return_value = LISTING
    return m


IMAP_ENV = {
    "CORPUS_IMAP_INBOX-A_HOST": "mail.example.org",
    "CORPUS_IMAP_INBOX-A_USER": "inbox-a",
    "CORPUS_IMAP_INBOX-A_PASSWORD": "x",
}


@pytest.mark.integration
def test_only_excluded_folders_are_purged_from_every_store(stores, client, tmp_path):
    # A special-use server's flags decide: an unflagged Junk Mail is kept, and a
    # folder it no longer lists goes with the \\Junk folder above it.
    keep = _seed(stores, "imap:inbox-a", {"INBOX": 2, "Sent": 1, "Junk Mail": 1})
    drop = _seed(stores, "imap:inbox-a", {"Quarantine": 2, "Quarantine/Old": 1})
    other = _seed(stores, "imap:inbox-b", {"Quarantine": 1})
    root = tmp_path / "vault"
    for doc_id in (keep[0], drop[0]):
        vault.write(doc_id, "synthetic", {}, root=root)
    source = sources.source("imap:inbox-a")
    assert isinstance(source, ImapSource)

    def make(s: ImapSource) -> ImapFetcher:
        return ImapFetcher(s, environ=IMAP_ENV, connect=lambda *_a, **_k: client)

    with DocumentStore() as documents:
        plan = purge.plan(source, documents, make_fetcher=make)
        assert {f: len(ids) for f, ids in plan.folders.items()} == {
            "Quarantine": 2, "Quarantine/Old": 1,
        }
        assert _stored(stores)["documents"] == set(keep + drop + other)  # planning changes nothing

        with SanitizedStore(stores, "corpus") as sanitized:
            counts = purge.apply([plan], documents, {"sanitized": sanitized}, vault_root=root)
            assert counts == {
                "tier:sanitized": 3, "vault": 1, "documents": 3, "enrichments": 3,
            }
            again = purge.plan(source, documents, make_fetcher=make)
            assert again.folders == {}
            assert purge.apply([again], documents, {"sanitized": sanitized}, vault_root=root) == {
                "tier:sanitized": 0, "vault": 0, "documents": 0, "enrichments": 0,
            }

    remaining = set(keep + other)
    assert _stored(stores) == {k: remaining for k in ("documents", "enrichments", "messages")}
    assert vault.read(keep[0], root) is not None
    assert vault.read(drop[0], root) is None


@pytest.mark.integration
def test_a_source_with_nothing_stored_does_not_connect(stores, client):
    _seed(stores, "imap:inbox-b", {"INBOX": 1})
    source = sources.source("imap:inbox-a")
    assert isinstance(source, ImapSource)
    make = create_autospec(ImapFetcher)

    with DocumentStore() as documents:
        assert purge.plan(source, documents, make_fetcher=make).folders == {}
    make.assert_not_called()


@pytest.mark.integration
def test_the_command_dry_runs_then_purges_against_a_real_server(stores, greenmail, configured,
                                                                  monkeypatch):
    configured("sources:\n  - {name: test, kind: imap, enrich: true}\n")
    for key, value in {
        "HOST": "127.0.0.1", "PORT": str(greenmail["imap_port"]), "USER": USER,
        "PASSWORD": "test", "SSL": "false",
    }.items():
        monkeypatch.setenv(f"CORPUS_IMAP_TEST_{key}", value)
    with IMAPClient("127.0.0.1", port=greenmail["imap_port"], ssl=False) as imap:
        imap.login(USER, "test")
        for folder in ("Sent", "Junk Mail"):
            imap.create_folder(folder)
    keep = _seed(stores, "imap:test", {"INBOX": 2, "Sent": 1})
    drop = _seed(stores, "imap:test", {"Junk Mail": 2})
    runner = CliRunner()

    dry = runner.invoke(cli.main, ["purge-excluded-folders"])
    assert dry.exit_code == 0, dry.output
    assert "imap:test: 2 documents in 1 excluded folders\n  Junk Mail: 2\n" in dry.output
    assert "dry run: 2 documents, 0 vault files" in dry.output
    assert not any(doc_id in dry.output for doc_id in drop)
    assert _stored(stores)["documents"] == set(keep + drop)

    applied = runner.invoke(cli.main, ["purge-excluded-folders", "imap:test", "--yes"])
    assert applied.exit_code == 0, applied.output
    assert "deleted: tier:sanitized 2, vault 0, documents 2, enrichments 2" in applied.output
    assert _stored(stores) == {k: set(keep) for k in ("documents", "enrichments", "messages")}

    repeat = runner.invoke(cli.main, ["purge-excluded-folders", "--yes"])
    assert repeat.exit_code == 0, repeat.output
    assert "deleted: tier:sanitized 0, vault 0, documents 0, enrichments 0" in repeat.output


@pytest.mark.integration
def test_deleting_before_any_table_exists_deletes_nothing(pg):
    with DocumentStore() as documents:
        assert documents.delete(["imap:inbox-a::INBOX:7:1"]) == {"documents": 0, "enrichments": 0}


@pytest.mark.integration
def test_an_id_outside_the_grammar_is_left_alone(stores):
    source = sources.source("imap:inbox-a")
    assert isinstance(source, ImapSource)
    with DocumentStore() as documents:
        documents.write([to_document(
            Record(source="imap:inbox-a", source_uid="no-folder", kind="email", body_text="x"),
            Classification(label="personal", confidence=0.9), _vec(0),
        )])
        plan = purge.plan(source, documents)
    assert plan.folders == {}


@pytest.mark.integration
def test_the_command_fails_when_a_sources_credentials_are_unset(stores):
    _seed(stores, "imap:inbox-a", {"Junk Mail": 1})

    result = CliRunner().invoke(cli.main, ["purge-excluded-folders", "imap:inbox-a"])

    assert result.exit_code == 1
    assert "CORPUS_IMAP_INBOX-A_HOST" in result.output
    assert len(_stored(stores)["documents"]) == 1


@pytest.mark.integration
def test_a_failure_between_stores_leaves_the_documents_for_a_rerun(stores, client):
    drop = _seed(stores, "imap:inbox-a", {"Quarantine": 2})
    source = sources.source("imap:inbox-a")
    assert isinstance(source, ImapSource)

    def make(s: ImapSource) -> ImapFetcher:
        return ImapFetcher(s, environ=IMAP_ENV, connect=lambda *_a, **_k: client)

    failing = create_autospec(DocumentStore, instance=True)
    failing.delete.side_effect = psycopg.OperationalError("connection lost")
    with DocumentStore() as documents:
        plan = purge.plan(source, documents, make_fetcher=make)
    with SanitizedStore(stores, "corpus") as sanitized:
        with pytest.raises(psycopg.OperationalError, match="connection lost"):
            purge.apply([plan], failing, {"sanitized": sanitized})

        after = _stored(stores)
        assert after["messages"] == set()
        assert after["documents"] == after["enrichments"] == set(drop)

        with DocumentStore() as documents:
            again = purge.plan(source, documents, make_fetcher=make)
            assert again == plan
            assert purge.apply([again], documents, {"sanitized": sanitized}) == {
                "tier:sanitized": 0, "vault": 0, "documents": 2, "enrichments": 2,
            }
    assert _stored(stores) == {k: set() for k in ("documents", "enrichments", "messages")}


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def refusing_server():
    """A local port whose server greets its one client with BYE and hangs up."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(10)

        def serve() -> None:
            with contextlib.suppress(OSError):
                conn, _addr = server.accept()
                with conn:
                    conn.sendall(b"* BYE synthetic server refuses\r\n")

        thread = threading.Thread(target=serve)
        thread.start()
        yield server.getsockname()[1]
        thread.join()


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["unreachable", "refusing"])
def test_an_unreachable_or_refusing_server_fails_the_command_before_deleting(
    stores, configured, monkeypatch, request, failure
):
    configured("sources:\n  - {name: test, kind: imap, enrich: true}\n")
    port = _closed_port() if failure == "unreachable" else request.getfixturevalue(
        "refusing_server"
    )
    for key, value in {
        "HOST": "127.0.0.1", "PORT": str(port), "USER": USER,
        "PASSWORD": "not-the-password", "SSL": "false",
    }.items():
        monkeypatch.setenv(f"CORPUS_IMAP_TEST_{key}", value)
    stored = _seed(stores, "imap:test", {"Junk Mail": 1})

    result = CliRunner().invoke(cli.main, ["purge-excluded-folders", "--yes"])

    assert result.exit_code == 1, result.output
    assert "imap:test: could not connect or log in to the IMAP server" in result.output
    assert "nothing was deleted" in result.output
    assert "not-the-password" not in result.output
    assert _stored(stores) == {k: set(stored) for k in ("documents", "enrichments", "messages")}
