"""Shared fixtures.

Unit tests need none of this. Integration tests (marked `integration`) get
Docker-backed services pinned to the versions used in deployment, and are
skipped automatically when Docker is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Iterator

import pytest

PG_IMAGE = "pgvector/pgvector:pg17"
GREENMAIL_IMAGE = "greenmail/standalone:2.1.3"

TEST_DIM = 16  # small embedding dimension keeps integration tests light


# --------------------------------------------------------------------------- #
# Docker plumbing
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_port(host: str, port: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(1.0)
            if s.connect_ex((host, port)) == 0:
                return
        time.sleep(0.5)
    raise TimeoutError(f"{host}:{port} not reachable within {timeout}s")


@pytest.fixture(scope="session")
def docker_client():
    try:
        import docker

        client = docker.from_env()
        client.ping()
    except Exception:  # noqa: BLE001 - any failure means "no docker here"
        pytest.skip("Docker unavailable")
    return client


# --------------------------------------------------------------------------- #
# Postgres / pgvector
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def pg_dsn(docker_client) -> Iterator[str]:
    port = _free_port()
    container = docker_client.containers.run(
        PG_IMAGE,
        detach=True,
        remove=True,
        environment={"POSTGRES_PASSWORD": "test", "POSTGRES_DB": "ai"},
        ports={"5432/tcp": port},
    )
    dsn = f"postgresql://postgres:test@127.0.0.1:{port}/ai"
    try:
        _wait_port("127.0.0.1", port)
        _wait_pg(dsn)
        yield dsn
    finally:
        container.stop()


def _wait_pg(dsn: str, timeout: float = 60.0) -> None:
    import psycopg

    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=2):
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.5)
    raise TimeoutError(f"postgres not ready: {last}")


@pytest.fixture
def pg(pg_dsn, monkeypatch) -> str:
    """A clean `corpus` schema (extension + sync_state) plus wired settings.

    Mirrors the schema the deployment's init job creates, so tests exercise the
    same object layout.
    """
    import psycopg

    from corpus.config import settings

    schema = "corpus"
    with psycopg.connect(pg_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(
            f"""
            CREATE TABLE {schema}.sync_state (
                source     text PRIMARY KEY,
                cursor     text,
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    monkeypatch.setattr(settings, "database_url", pg_dsn)
    monkeypatch.setattr(settings, "db_schema", schema)
    monkeypatch.setattr(settings, "embedding_dimensions", TEST_DIM)
    return pg_dsn


# --------------------------------------------------------------------------- #
# Fake embedding endpoint (deterministic, in-process, no model)
# --------------------------------------------------------------------------- #
def deterministic_vector(text: str, dim: int = TEST_DIM) -> list[float]:
    seed = int(hashlib.sha256(text.encode()).hexdigest(), 16)
    return [((seed >> (i * 8)) & 0xFF) / 255.0 for i in range(dim)]


class _EmbedServer(ThreadingHTTPServer):
    daemon_threads = True
    dim = TEST_DIM


class _EmbedHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        inp = body.get("input", [])
        if isinstance(inp, str):
            inp = [inp]
        data = [
            {"object": "embedding", "index": i,
             "embedding": deterministic_vector(t, self.server.dim)}
            for i, t in enumerate(inp)
        ]
        payload = json.dumps(
            {"object": "list", "data": data, "model": body.get("model"),
             "usage": {"prompt_tokens": 0, "total_tokens": 0}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # silence
        pass


@pytest.fixture
def fake_embeddings(monkeypatch):
    from corpus.config import settings

    server = _EmbedServer(("127.0.0.1", 0), _EmbedHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(settings, "openai_api_base", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setattr(settings, "openai_api_key", "test")
    monkeypatch.setattr(settings, "embedding_dimensions", TEST_DIM)
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- #
# GreenMail (IMAP + SMTP)
# --------------------------------------------------------------------------- #
@pytest.fixture
def greenmail(docker_client) -> Iterator[dict[str, int]]:
    imap_port = _free_port()
    smtp_port = _free_port()
    container = docker_client.containers.run(
        GREENMAIL_IMAGE,
        detach=True,
        remove=True,
        environment={
            "GREENMAIL_OPTS": (
                "-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 "
                "-Dgreenmail.auth.disabled -Dgreenmail.verbose"
            )
        },
        ports={"3143/tcp": imap_port, "3025/tcp": smtp_port},
    )
    try:
        _wait_port("127.0.0.1", imap_port)
        _wait_port("127.0.0.1", smtp_port)
        time.sleep(1.0)  # let the IMAP service finish binding

        def send(to_addr: str, subject: str, body: str,
                 from_addr: str = "sender@example.org",
                 extra_headers: dict[str, str] | None = None) -> None:
            _send_mail(smtp_port, to_addr, subject, body, from_addr, extra_headers)

        yield {"imap_port": imap_port, "smtp_port": smtp_port, "send": send}
    finally:
        container.stop()


def _send_mail(smtp_port: int, to_addr: str, subject: str, body: str,
               from_addr: str = "sender@example.org",
               extra_headers: dict[str, str] | None = None) -> None:
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    for k, v in (extra_headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    with smtplib.SMTP("127.0.0.1", smtp_port, timeout=10) as smtp:
        smtp.send_message(msg)
