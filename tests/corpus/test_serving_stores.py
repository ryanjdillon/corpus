"""The built MCP servers against real Postgres: tool call to SQL and back.

Covers the DIL-636 acceptance queries on the sanitized tier, the DIL-638 typed
facts ("trips to the US in 2025"), and the sensitive tier's paging through
``DocumentStore.query_spec``. Every row is synthetic.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from functools import partial
from unittest.mock import create_autospec

import psycopg
import pytest

from corpus import sanitize, serving, tiers
from corpus.config import settings
from corpus.embeddings import Embedder, embed_query
from corpus.models import Classification, Record
from corpus.sanitize import DEFAULT_POLICY
from corpus.sanitized_store import COLS, SanitizedStore
from corpus.store import DocumentStore, to_document

pytestmark = pytest.mark.integration

SCHEMA = "corpus"
DIM = 16  # the pg fixture's embedding dimension (conftest.TEST_DIM)


def _vec(*head: float) -> list[float]:
    return [*head, *([0.0] * (DIM - len(head)))]


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


#: Synthetic sanitized rows: id -> (domain, sent_at, embedding). t2 and t3 tie on
#: sent_at; t5 and t6 sit just outside 2025.
MESSAGES = {
    "t1": ("travel", _at("2025-01-15T09:00"), _vec(1.0)),
    "t2": ("travel", _at("2025-03-02T10:00"), _vec(0.9, 0.1)),
    "t3": ("travel", _at("2025-03-02T10:00"), _vec(0.8, 0.2)),
    "t4": ("travel", _at("2025-12-31T23:30"), _vec(0.7, 0.3)),
    "t5": ("travel", _at("2024-12-31T23:59"), _vec(0.0, 1.0)),
    "t6": ("travel", _at("2026-01-01T00:00"), _vec(0.1, 0.9)),
    "w1": ("work", _at("2025-03-02T10:00"), _vec(0.0, 0.0, 1.0)),
}


def call(server, tool: str, **args) -> dict:
    _, structured = asyncio.run(server.call_tool(tool, args))
    return structured


@pytest.fixture
def embed():
    """A spec-bound query embedder: every query lands next to t1."""
    return create_autospec(embed_query, return_value=_vec(1.0))


@pytest.fixture
def index(pg, embed):
    """The sanitized tier's server over ``MESSAGES``, read through a read-only store."""
    with SanitizedStore(dsn=pg, schema=SCHEMA) as store:
        for id_, (domain, sent_at, vec) in MESSAGES.items():
            row = {c: None for c in COLS} | {
                "id": id_, "domain": domain, "sent_at": sent_at, "importance": "low",
                "requires_action": False, "time_sensitive": False, "sensitivity_level": "none",
                "one_line": f"synthetic item {id_}",
            }
            store.save_message(row, vec)
    open_store = partial(SanitizedStore, dsn=pg, schema=SCHEMA, read_only=True)
    return serving.build_mcp("sanitized", open_store=open_store, embed=embed)


TRAVEL_2025 = {"domain": "travel", "sent_at": {"since": "2025-01-01", "until": "2026-01-01"}}


def test_travel_in_2025_newest_first(index):
    out = call(index, "query", spec={"filters": TRAVEL_2025, "order": "newest"})

    assert [r["id"] for r in out["rows"]] == ["t4", "t3", "t2", "t1"]
    assert (out["tier"], out["next_cursor"]) == ("sanitized", None)


def test_travel_in_2025_pages_by_keyset_without_skipping_a_tie(index):
    spec = {"filters": TRAVEL_2025, "order": "newest", "limit": 2}
    first = call(index, "query", spec=spec)
    second = call(index, "query", spec={**spec, "cursor": first["next_cursor"]})

    assert [r["id"] for r in first["rows"] + second["rows"]] == ["t4", "t3", "t2", "t1"]
    assert second["next_cursor"] is None


def test_count_of_travel_per_month_in_2025(index):
    out = call(index, "aggregate", spec={"filters": TRAVEL_2025}, group_by=["month"])

    assert [(g["month"], g["count"]) for g in out["rows"]] == [
        ("2025-01", 1), ("2025-03", 2), ("2025-12", 1),
    ]
    assert (out["total"], out["truncated"]) == (4, False)


def test_search_ranks_by_the_embedded_text(index, embed):
    out = call(index, "search", text="a synthetic trip", spec={"filters": TRAVEL_2025, "limit": 2})

    embed.assert_called_once_with("a synthetic trip")
    assert [r["id"] for r in out["rows"]] == ["t1", "t2"]
    assert out["next_cursor"] is not None


#: Synthetic rows under three digests: the served policy's, another policy's,
#: and none (synced before the digest existed). Only "cur" is served as stored.
STALE = "0123456789abcdef"
PROJECTED = {
    "cur": (DEFAULT_POLICY.digest(), "travel", "a friend booked the ferry", "Pay the deposit",
            ["Fjordline"]),
    "old": (STALE, "travel", "Kari Nordmann booked the ferry", "Call Kari Nordmann",
            ["Nordmann Holding AS"]),
    "pre": (None, "banking", "Bjørn Ås sent card 4111 1111 1111 1111", "Reply to Bjørn Ås",
            ["Ås Regnskap AS"]),
}


@pytest.fixture
def projected_index(pg, embed):
    """The sanitized tier's server as configured, over ``PROJECTED``.

    Built with the tier's own opener, so the store knows its projection policy
    the way a deployed index does.
    """
    with SanitizedStore(dsn=pg, schema=SCHEMA) as store:
        for i, (id_, (digest, domain, one_line, action, orgs)) in enumerate(PROJECTED.items()):
            row = {c: None for c in COLS} | {
                "id": id_, "domain": domain, "sent_at": _at(f"2025-0{i + 1}-01T09:00"),
                "importance": "low", "requires_action": True, "time_sensitive": False,
                "sensitivity_level": "low", "one_line": one_line, "action_summary": action,
                "organizations": orgs, "policy_hash": digest,
                "embedded_policy_hash": digest,  # each vector is of its stored text
            }
            store.save_message(row, _vec(1.0, i / 10))
    environ = {"CORPUS_SANITIZED_DATABASE_URL": pg, "CORPUS_INDEX_DATABASE_URL": pg}
    tier = tiers.Registry(tiers.DEFAULT, settings, environ).tier("sanitized")
    return serving.build_mcp("sanitized", config=tier, embed=embed)


def _free_text(row: dict) -> tuple:
    return row["one_line"], row["action_summary"], row["organizations"]


SERVED = {
    "cur": ("a friend booked the ferry", "Pay the deposit", ["Fjordline"]),
    "old": ("[pending redaction] travel item", None, None),
    "pre": ("[pending redaction] banking item", None, None),
}


def test_a_row_under_another_policy_is_served_degraded_by_query(projected_index):
    out = call(projected_index, "query", spec={"order": "newest"})

    assert {r["id"]: _free_text(r) for r in out["rows"]} == SERVED


def test_a_row_under_another_policy_never_ranks_by_its_old_vector(projected_index, embed):
    # "pre" has neither hash and "old" another policy's: each vector is of the
    # text the placeholder hides, so neither ranks, though keyword finds both.
    out = call(projected_index, "search", text="the ferry")
    by_keyword = call(projected_index, "query", spec={"text": "pending redaction"})

    assert {r["id"]: _free_text(r) for r in out["rows"]} == {"cur": SERVED["cur"]}
    assert {r["id"]: _free_text(r) for r in by_keyword["rows"]} == {
        k: SERVED[k] for k in ("old", "pre")
    }


def test_a_row_under_another_policy_is_served_degraded_by_get(projected_index):
    for id_, expected in SERVED.items():
        (row,) = call(projected_index, "get", id=id_)["rows"]
        assert _free_text(row) == expected


def test_no_filter_or_group_reaches_a_stale_rows_stored_text(projected_index):
    def ids(**spec) -> list[str]:
        return [r["id"] for r in call(projected_index, "query", spec=spec)["rows"]]

    assert ids(text="Nordmann") == ids(text="4111") == []
    assert ids(text="ferry") == ["cur"]
    assert ids(text="pending redaction", order="newest") == ["pre", "old"]
    assert ids(filters={"organization": "Nordmann"}) == []
    groups = call(projected_index, "aggregate", spec={}, group_by=["organization"])["rows"]
    assert [(g["organization"], g["count"]) for g in groups] == [("Fjordline", 1), (None, 2)]


def test_an_embed_stale_row_is_left_out_of_search_until_re_embedded(pg, projected_index):
    # "cur" is re-projected but its vector is of an earlier, unredacted one_line.
    digest = DEFAULT_POLICY.digest()
    with psycopg.connect(pg, autocommit=True) as conn:
        conn.execute(
            f"UPDATE {SCHEMA}.messages SET embedded_policy_hash = %s WHERE id = 'cur'", (STALE,)
        )

    def ids(tool: str, **args) -> list[str]:
        return [r["id"] for r in call(projected_index, tool, **args)["rows"]]

    assert ids("search", text="the ferry") == []
    assert ids("query", spec={"text": "ferry"}) == ["cur"]
    assert ids("query", spec={"filters": {"organization": "Fjordline"}}) == ["cur"]
    assert ids("get", id="cur") == ["cur"]

    with SanitizedStore(dsn=pg, schema=SCHEMA) as store:
        written = store.save_embeddings(digest, [("cur", SERVED["cur"][0], _vec(1.0))])

    assert written == 1
    assert ids("search", text="the ferry") == ["cur"]


def _document(uid: str, sent_at: str, label: str = "personal"):
    record = Record(
        source="imap:test", source_uid=uid, kind="email", account="me@example.com",
        from_addr="sender@example.org", subject=f"synthetic {uid}", sent_at=_at(sent_at),
        body_text=f"synthetic body {uid}",
    )
    return to_document(record, Classification(label=label, confidence=0.9), _vec(0.5, 0.5))


@pytest.fixture
def corpus(pg, embed):
    """The sensitive tier's server over three synthetic documents, two tied on sent_at."""
    with DocumentStore() as documents:
        documents.write([
            _document("1", "2026-01-01T08:00"),
            _document("2", "2026-01-02T08:00"),
            _document("3", "2026-01-02T08:00"),
            _document("4", "2026-01-03T08:00", label="newsletter"),
        ])
    return serving.build_mcp("sensitive", embed=embed)


def test_corpus_query_pages_through_the_document_store(corpus):
    args = {"label": "personal", "after": "2026-01-01"}
    first = call(corpus, "corpus_query", **args, limit=2)
    second = call(corpus, "corpus_query", **args, limit=2, cursor=first["next_cursor"])

    ids = [r["id"] for r in first["rows"] + second["rows"]]
    assert ids == ["imap:test::3", "imap:test::2", "imap:test::1"]
    assert second["next_cursor"] is None


def test_the_sensitive_tier_counts_per_label(corpus):
    out = call(corpus, "aggregate", spec={}, group_by=["label"])

    assert [(g["label"], g["count"]) for g in out["rows"]] == [("newsletter", 1), ("personal", 3)]


# --- typed facts: trips to the US in 2025 (DIL-638) ------------------------------------
#
# Each trip goes the production way: an enrichment's facts are projected by the
# sync under the configured sanitized tier's policy, then read by its server.
# Places and carriers are invented.


def _trip(start, country, *, end=None, carrier="Fjordair", origin="NO"):
    return {"travel": {"start": start, "end": end, "carrier": carrier,
                       "origin": {"name": "Oslo", "country": origin},
                       "destination": {"name": "a city", "country": country}}}


#: id -> (sent_at, facts). h1 is a stay (no carrier) that checks in on New
#: Year's Eve; n1 is not travel.
TRIPS = {
    "f1": ("2025-04-01T09:00", _trip("2025-06-02", "US", end="2025-06-16")),
    "f2": ("2024-11-01T09:00", _trip("2024-12-28", "US")),
    "r1": ("2025-07-01T09:00", _trip("2025-08-01", "NO", carrier="Nordrail")),
    "h1": ("2025-11-01T09:00", _trip("2025-12-31", "us", end="2026-01-02", carrier=None,
                                    origin=None)),
    "f3": ("2025-12-01T09:00", _trip("2026-01-01", "US")),
    "n1": ("2025-05-01T09:00", {"travel": None}),
}
US_2025 = {"facts.travel.destination_country": "US",
           "facts.travel.start": {"since": "2025-01-01", "until": "2026-01-01"}}


@pytest.fixture
def trips(pg, embed):
    """The configured sanitized tier's server over ``TRIPS``, synced, plus a stale row.

    ``s1`` was projected under another policy: its stored facts are a US trip in
    2025, which the gate must not serve, filter or count.
    """
    environ = {"CORPUS_SANITIZED_DATABASE_URL": pg, "CORPUS_INDEX_DATABASE_URL": pg}
    tier = tiers.Registry(tiers.DEFAULT, settings, environ).tier("sanitized")
    docs = [
        (id_, {"source": "imap:test", "sent_at": sent_at},
         {"one_line": f"synthetic trip {id_}", "domain": "travel", "sensitivity_level": "low",
          "people": [], "references": [], "facts": facts}, _at(sent_at))
        for id_, (sent_at, facts) in TRIPS.items()
    ]
    documents = create_autospec(DocumentStore, instance=True)
    documents.iter_enriched.side_effect = lambda source=None: (d for d in docs)
    embedder = create_autospec(Embedder, instance=True)
    embedder.embed.side_effect = lambda texts: [_vec(1.0) for _ in texts]
    with SanitizedStore(dsn=pg, schema=SCHEMA) as store:
        sanitize.run_sync(store, documents=documents, embedder=embedder,
                          policy=tier.projection.policy)
        stale = {c: None for c in COLS} | {
            "id": "s1", "domain": "travel", "sent_at": _at("2025-02-01T09:00"),
            "one_line": "synthetic trip s1", "policy_hash": STALE,
            "facts": _trip("2025-03-03", "US"),
        }
        store.save_message(stale, _vec(1.0))
    return serving.build_mcp("sanitized", config=tier, embed=embed)


def test_trips_to_the_us_in_2025_in_one_query(trips):
    out = call(trips, "query", spec={"filters": US_2025, "order": "oldest"})

    assert [r["id"] for r in out["rows"]] == ["f1", "h1"]
    # The default policy projects a place's country, never its name.
    assert out["rows"][0]["facts"]["travel"]["destination"] == {"country": "US"}
    assert "facts" in out["untrusted_fields"]
    assert out["next_cursor"] is None


def test_trips_page_by_cursor_bound_to_their_facts_filters(trips):
    spec = {"filters": US_2025, "order": "oldest", "limit": 1}
    first = call(trips, "query", spec=spec)
    second = call(trips, "query", spec={**spec, "cursor": first["next_cursor"]})

    assert [r["id"] for r in first["rows"] + second["rows"]] == ["f1", "h1"]
    moved = {**US_2025, "facts.travel.destination_country": "NO"}
    with pytest.raises(Exception, match="cursor belongs to a different query"):
        call(trips, "query", spec={**spec, "filters": moved, "cursor": first["next_cursor"]})


def test_trips_count_per_destination_country_and_year(trips):
    out = call(trips, "aggregate", spec={"filters": {"facts.travel": True}},
               group_by=["facts.travel.destination_country", "facts.travel.start_year"])

    groups = [(g["facts.travel.destination_country"], g["facts.travel.start_year"], g["count"])
              for g in out["rows"]]
    assert groups == [("NO", "2025", 1), ("US", "2024", 1), ("US", "2025", 2), ("US", "2026", 1)]
    assert out["total"] == 5


def test_a_carrier_matches_whole_ignoring_case_and_ends_bound_the_return(trips):
    def ids(**filters) -> list[str]:
        spec = {"filters": filters, "order": "oldest"}
        return [r["id"] for r in call(trips, "query", spec=spec)["rows"]]

    assert ids(**{"facts.travel.carrier": "NORDRAIL"}) == ["r1"]
    assert ids(**{"facts.travel.carrier": "Nord"}) == []
    assert ids(**{"facts.travel.end": {"since": "2026-01-01"}}) == ["h1"]
    assert ids(**{"facts.travel.origin_country": ["no", "SE"],
                  "facts.travel.start": {"since": "2025-01-01"}}) == ["f1", "r1", "f3"]


def test_a_stale_row_serves_no_facts(trips):
    (row,) = call(trips, "get", id="s1")["rows"]
    assert row["facts"] is None
    assert row["one_line"] == "[pending redaction] travel item"
    out = call(trips, "query", spec={"filters": {"facts.travel": False}, "order": "oldest"})
    assert [r["id"] for r in out["rows"]] == ["s1", "n1"]
