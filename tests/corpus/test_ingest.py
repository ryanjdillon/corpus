"""Ingestion pipeline tests.

A length-capping unit test plus Postgres-backed integration tests driven by an
injected, spec-bound fetcher (no mail server needed).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import create_autospec

import pytest

from corpus.fetchers import Fetcher
from corpus.ingest import _MAX_EMBED_CHARS, _embed_text, ingest
from corpus.models import Record

_TEST_DIM = 16
_POISON = "POISON"


@pytest.fixture
def make_fetcher():
    """Build a spec-bound Fetcher yielding fixed records and a fixed next cursor."""

    def _make(records: list[Record], cursor: str | None = "1:2") -> Fetcher:
        fetcher = create_autospec(Fetcher, instance=True)
        fetcher.source = "imap:test"
        fetcher.fetch.side_effect = lambda _cursor: iter(records)
        fetcher.next_cursor.return_value = cursor
        return fetcher

    return _make


def _records(n: int, bad_index: int | None = None) -> list[Record]:
    recs = []
    for i in range(n):
        body = f"{_POISON} body {i}" if i == bad_index else f"promotional body {i}"
        recs.append(
            Record(
                source="imap:test",
                source_uid=str(i),
                kind="email",
                from_addr="a@b.com",
                subject=f"subject {i}",
                sent_at=datetime(2026, 1, i + 1, tzinfo=UTC),
                headers={"List-Unsubscribe": "<mailto:x>", "Precedence": "bulk"},
                body_text=body,
            )
        )
    return recs


# --------------------------------------------------------------------------- #
# unit
# --------------------------------------------------------------------------- #
def test_embed_text_is_length_capped():
    # A message whose body is one giant unbroken "word" (e.g. inline base64)
    # must still produce a bounded embed input.
    rec = Record(
        source="imap:test",
        source_uid="x",
        kind="email",
        subject="s",
        body_text="A" * (_MAX_EMBED_CHARS * 3),
    )
    assert len(_embed_text(rec)) == _MAX_EMBED_CHARS


# --------------------------------------------------------------------------- #
# integration
# --------------------------------------------------------------------------- #
def _vec(text: str) -> list[float]:
    seed = int(hashlib.sha256(text.encode()).hexdigest(), 16)
    return [((seed >> (i * 8)) & 0xFF) / 255.0 for i in range(_TEST_DIM)]


class _RejectingHandler(BaseHTTPRequestHandler):
    """Embedder stand-in that 400s any batch containing a poisoned input (mimics
    rejecting a bad/too-long message), and 200s everything else."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        inp = body.get("input", [])
        if isinstance(inp, str):
            inp = [inp]
        if any(_POISON in t for t in inp):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"input rejected"}')
            return
        data = [
            {"object": "embedding", "index": i, "embedding": _vec(t)}
            for i, t in enumerate(inp)
        ]
        payload = json.dumps({"object": "list", "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # silence
        pass


@pytest.fixture
def rejecting_embeddings(monkeypatch):
    from corpus.config import settings

    server = ThreadingHTTPServer(("127.0.0.1", 0), _RejectingHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "openai_api_base", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setattr(settings, "openai_api_key", "test")
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.integration
def test_ingest_writes_and_advances_cursor(pg, fake_embeddings, make_fetcher):
    fetcher = make_fetcher(_records(3), cursor="1:3")

    count = ingest("imap:test", batch_size=2, fetcher=fetcher)
    assert count == 3

    from corpus.store import get_cursor, get_document_store

    assert get_document_store().count_documents() == 3
    assert get_cursor("imap:test") == "1:3"


@pytest.mark.integration
def test_ingest_applies_classification(pg, fake_embeddings, make_fetcher):
    fetcher = make_fetcher(_records(1), cursor="1:1")
    ingest("imap:test", fetcher=fetcher)

    from corpus.store import get_document_store

    doc = get_document_store().filter_documents()[0]
    assert doc.meta["label"] == "promotional"


@pytest.mark.integration
def test_ingest_is_idempotent(pg, fake_embeddings, make_fetcher):
    fetcher = make_fetcher(_records(2), cursor="1:2")
    ingest("imap:test", fetcher=fetcher)
    ingest("imap:test", fetcher=fetcher)  # same ids overwrite, not duplicate

    from corpus.store import get_document_store

    assert get_document_store().count_documents() == 2


@pytest.mark.integration
def test_ingest_skips_bad_record_and_stores_rest(pg, rejecting_embeddings, make_fetcher):
    # Record #2 is poisoned: the embedder 400s any batch containing it. The good
    # records must still be stored; only the offending one is skipped.
    fetcher = make_fetcher(_records(4, bad_index=2), cursor="1:4")

    count = ingest("imap:test", batch_size=4, fetcher=fetcher)
    assert count == 3

    from corpus.store import get_document_store

    store = get_document_store()
    assert store.count_documents() == 3
    ids = {d.id for d in store.filter_documents()}
    assert "imap:test::2" not in ids  # the poisoned record was skipped
    assert {"imap:test::0", "imap:test::1", "imap:test::3"} <= ids


class _StatusHandler(BaseHTTPRequestHandler):
    """Always responds with a fixed status (set via .status on the server)."""

    def do_POST(self) -> None:
        self.send_response(self.server.status)
        self.end_headers()
        self.wfile.write(b'{"error":"x"}')

    def log_message(self, *args) -> None:
        pass


def _serve_status(status, monkeypatch):
    from corpus.config import settings

    server = ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
    server.status = status
    server.daemon_threads = True
    port = server.server_address[1]
    Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "openai_api_base", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setattr(settings, "openai_api_key", "test")
    monkeypatch.setattr("corpus.embeddings.time.sleep", lambda *_: None)  # no retry backoff
    return server


@pytest.mark.integration
def test_ingest_aborts_when_embedder_unavailable(pg, monkeypatch, make_fetcher):
    from corpus.embeddings import EmbedUnavailableError

    server = _serve_status(503, monkeypatch)  # 5xx after retries -> unavailable
    try:
        fetcher = make_fetcher(_records(4), cursor="1:4")
        with pytest.raises(EmbedUnavailableError):
            ingest("imap:test", batch_size=4, fetcher=fetcher)
        from corpus.store import get_document_store

        assert get_document_store().count_documents() == 0  # aborted, nothing stored
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.integration
def test_ingest_aborts_after_too_many_consecutive_skips(pg, monkeypatch, make_fetcher):
    server = _serve_status(400, monkeypatch)  # every record rejected as bad input
    try:
        fetcher = make_fetcher(_records(30), cursor="1:30")
        with pytest.raises(RuntimeError):  # the consecutive-skip guard trips
            ingest("imap:test", batch_size=8, fetcher=fetcher)
    finally:
        server.shutdown()
        server.server_close()


def test_strip_nul_removes_nul_recursively():
    from corpus.store import _strip_nul

    assert _strip_nul("a\x00b") == "ab"
    assert _strip_nul(["x\x00", "y"]) == ["x", "y"]
    assert _strip_nul({"k": "v\x00"}) == {"k": "v"}
    assert _strip_nul(42) == 42
    assert _strip_nul(None) is None
