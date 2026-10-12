"""The corpus-index presets are fixed queries over the sanitized store, read as the
restricted index role, and never select raw content, sender, or secrets."""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest

from corpus import index_query
from corpus.query import Aggregate, Filters, Page, QuerySpec
from corpus.sanitized_store import COLS, SanitizedStore


@pytest.fixture
def store():
    """A spec-bound SanitizedStore returning one canned sanitized row."""
    s = create_autospec(SanitizedStore, instance=True)
    s.__enter__.return_value = s
    s.query.return_value = Page([{"id": "gmail:personal::1", "one_line": "x"}], None)
    return s


@pytest.fixture
def open_store(store):
    """A spec-bound reader factory handing back ``store``."""
    f = create_autospec(index_query.reader)
    f.return_value = store
    return f


def test_action_items_filters_and_orders(store, open_store):
    rows = index_query.action_items(
        limit=10, domain="banking", importance="high", offset=20, open_store=open_store
    )

    assert rows == [{"id": "gmail:personal::1", "one_line": "x"}]
    store.query.assert_called_once_with(
        QuerySpec(
            filters=Filters(requires_action=True, domain="banking", importance="high"),
            order="priority",
            limit=10,
        ),
        index_query.PRESETS,
        20,
    )
    store.__exit__.assert_called_once()  # the reader connection is closed


def test_action_items_no_filters(store, open_store):
    index_query.action_items(open_store=open_store)
    spec, _, offset = store.query.call_args.args

    assert spec.filters == Filters(requires_action=True)
    assert (spec.limit, offset) == (50, 0)  # the default limit and offset


def test_waiting_on_binds_who(store, open_store):
    index_query.waiting_on(who="me", limit=5, open_store=open_store)
    spec, _, offset = store.query.call_args.args

    assert spec.filters == Filters(waiting_on="me")
    assert (spec.order, spec.limit, offset) == ("priority", 5, 0)


def test_by_domain_binds_domain(store, open_store):
    index_query.by_domain("health", open_store=open_store)
    spec, _, offset = store.query.call_args.args
    assert (spec.filters, spec.order, spec.limit, offset) == (
        Filters(domain="health"), "priority", 50, 0,
    )


def test_due_soon_is_due_items_by_deadline(store, open_store):
    index_query.due_soon(limit=7, open_store=open_store)
    spec, _, offset = store.query.call_args.args
    assert (spec.filters, spec.order, spec.limit, offset) == (Filters(due=True), "deadline", 7, 0)


def test_summary_returns_the_row_or_none(store, open_store):
    store.get.return_value = {"id": "x"}
    assert index_query.summary("x", open_store=open_store) == {"id": "x"}
    store.get.return_value = None
    assert index_query.summary("x", open_store=open_store) is None
    store.get.assert_called_with("x")


def test_stats_aggregates(store, open_store):
    store.aggregate.side_effect = [
        Aggregate([{"domain": "banking", "count": 2}], 3, False),
        Aggregate([{"suggested_disposition": "keep", "count": 3}], 3, False),
    ]

    assert index_query.stats(open_store=open_store) == {
        "total": 3,
        "by_domain": {"banking": 2},
        "by_disposition": {"keep": 3},
    }
    dims = [c.args[0].group_by for c in store.aggregate.call_args_list]
    assert dims == [["domain"], ["suggested_disposition"]]


def test_presets_stay_inside_the_sanitized_policy_plus_their_own_dimensions():
    sanitized = index_query.SANITIZED_POLICY
    assert index_query.PRESETS.filterable - sanitized.filterable == {"due"}
    assert index_query.PRESETS.aggregatable - sanitized.aggregatable == {"suggested_disposition"}
    # the cap is the MCP page plus the one row fetched to learn whether more exist
    assert index_query.PRESETS.max_limit == index_query.MAX_LIMIT + 1


def test_projection_stays_cloud_safe():
    # the priority signal the planner needs is in the projection…
    for col in ("one_line", "requires_action", "time_sensitive", "importance", "waiting_on"):
        assert col in COLS
    # …and raw content, sender, secrets, and the embedding are never selected
    for banned in ("content", "subject", "from_addr", "summary_embedding", "secret"):
        assert not any(banned in c for c in COLS)


def test_query_layer_raises_without_index_url(monkeypatch):
    # a real query (default reader) refuses to run if the restricted DSN isn't set
    monkeypatch.setattr(index_query.settings, "index_database_url", "")
    with pytest.raises(RuntimeError, match="CORPUS_INDEX_DATABASE_URL"):
        index_query.action_items()
