"""The MCP tool-call audit: one JSON event per call, with the caller's principal.

The principal is read from the request the MCP server is handling, so these tests
set that request context the way the server does. Arguments are synthetic.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import create_autospec

import pytest
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
from starlette.requests import Request
from starlette.testclient import TestClient

from corpus import index_query, index_server
from corpus.telemetry import audited, filter_summary


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/mcp", "headers": raw})


@pytest.fixture
def calling_as():
    """Run the next calls as if the MCP server were handling a request with *headers*."""
    tokens = []

    def set_headers(headers: dict[str, str]) -> None:
        ctx = RequestContext(
            request_id=1, meta=None, session=None, lifespan_context=None,
            request=_request(headers),
        )
        tokens.append(request_ctx.set(ctx))

    yield set_headers
    for token in reversed(tokens):
        request_ctx.reset(token)


def _events(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "corpus.audit"]


@audited(server="corpus-index", tier="sanitized")
def lookup(query: str, limit: int = 10, domain: str | None = None) -> dict:
    return {"count": 2, "rows": [{}, {}]}


@audited(server="corpus-index", tier="sanitized")
def broken(id: str) -> dict:
    raise RuntimeError("database unavailable")


def test_a_call_is_audited_with_its_principal_and_row_count(calling_as, caplog):
    calling_as({"x-client-id": "orchestrator"})
    caplog.set_level(logging.INFO, logger="corpus.audit")

    lookup(query="invoice from the bank", limit=5, domain="banking")

    (event,) = _events(caplog)
    assert event["event"] == "mcp.tool_call"
    assert (event["server"], event["tier"], event["tool"]) == ("corpus-index", "sanitized", "lookup")
    assert event["principal"] == "orchestrator"
    assert (event["rows"], event["outcome"]) == (2, "ok")
    assert event["duration_ms"] >= 0


def test_the_audit_never_logs_a_free_text_query(calling_as, caplog):
    calling_as({"x-client-id": "pi"})
    caplog.set_level(logging.INFO, logger="corpus.audit")

    lookup("invoice from the bank", limit=5)

    (event,) = _events(caplog)
    assert event["filters"] == {"query": {"chars": 21}, "limit": 5}
    assert "invoice" not in caplog.text


@pytest.mark.parametrize("headers", [{}, {"x-client-id": ""}])
def test_a_call_without_a_principal_is_logged_as_unknown(calling_as, caplog, headers):
    calling_as(headers)
    caplog.set_level(logging.INFO, logger="corpus.audit")

    lookup(query="x")

    assert _events(caplog)[0]["principal"] == "unknown"


def test_a_call_outside_a_request_is_logged_as_unknown(caplog):
    caplog.set_level(logging.INFO, logger="corpus.audit")

    lookup(query="x")

    assert _events(caplog)[0]["principal"] == "unknown"


def test_a_failed_call_is_audited_and_reraised(calling_as, caplog):
    calling_as({"x-client-id": "pi"})
    caplog.set_level(logging.INFO, logger="corpus.audit")

    with pytest.raises(RuntimeError):
        broken(id="gmail:personal::1")

    (event,) = _events(caplog)
    assert (event["outcome"], event["rows"]) == ("RuntimeError", None)
    assert event["filters"] == {"id": "gmail:personal::1"}


@pytest.mark.parametrize(
    ("args", "summary"),
    [
        ({"since": "1d", "label": "personal", "limit": 50}, {"since": "1d", "label": "personal", "limit": 50}),
        ({"query": "my passport number"}, {"query": {"chars": 18}}),
        ({"domain": None, "top_k": 3}, {"domain": None, "top_k": 3}),
    ],
)
def test_filter_summary_keeps_shapes_and_hides_free_text(args, summary):
    assert filter_summary(args) == summary


def test_audited_tools_keep_their_input_schema():
    tools = {t.name: t for t in asyncio.run(index_server.mcp.list_tools())}

    assert set(tools["index_action_items"].inputSchema["properties"]) == {
        "limit", "domain", "importance"
    }


def test_the_principal_reaches_the_audit_through_a_real_mcp_request(monkeypatch, caplog):
    # End to end through FastMCP's streamable-HTTP app: the header the gateway
    # sets must be what the audit records.
    due_soon = create_autospec(index_query.due_soon, return_value=[{"id": "a"}])
    monkeypatch.setattr(index_server.index_query, "due_soon", due_soon)
    caplog.set_level(logging.INFO, logger="corpus.audit")
    call = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "index_due_soon", "arguments": {"limit": 3}},
    }
    headers = {"accept": "application/json, text/event-stream", "x-client-id": "orchestrator"}

    with TestClient(index_server.mcp.streamable_http_app()) as client:
        response = client.post("/mcp", headers=headers, json=call)

    assert response.status_code == 200
    (event,) = _events(caplog)
    assert (event["principal"], event["tool"], event["rows"]) == ("orchestrator", "index_due_soon", 1)
