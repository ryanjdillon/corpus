"""The query model validates a spec, refuses what a tier's policy does not allow, and
builds SQL only from the store's fixed expressions, with every value bound."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import msgspec
import pytest

from corpus import sanitized_store, store
from corpus.query import (
    FILTERS,
    SANITIZED_POLICY,
    SEMANTIC_DEPTH,
    SENSITIVE_POLICY,
    Filters,
    GroupBy,
    QueryPolicy,
    QuerySpec,
    Range,
    build_aggregate,
    build_query,
    check,
    parse_spec,
    relative_cutoff,
    resolve_bound,
    resolve_day,
)

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
SANITIZED = sanitized_store.layout("corpus")
DOCUMENTS = store.layout("corpus.documents")

#: One valid value for every filter, so each can be set alone.
FILTER_VALUES = {
    "domain": "travel", "category": "personal", "transactional_type": "booking",
    "importance": "high", "waiting_on": "me", "action_type": "reply",
    "suggested_disposition": "keep", "requires_action": True, "time_sensitive": True,
    "due": True, "sender_domain": "example.com", "account": "me@example.com",
    "source": "imap:test", "label": "personal", "organization": "Acme",
    "sent_at": {"since": "2025-01-01"}, "deadline": {"until": "14d"},
    "max_sensitivity": "medium", "facts.travel": True,
    "facts.travel.origin_country": "NO", "facts.travel.destination_country": ["us", "CA"],
    "facts.travel.carrier": "Norwegian", "facts.travel.start": {"since": "2025-01-01"},
    "facts.travel.end": {"until": "2026-01-01"},
}


TIERS = pytest.mark.parametrize(
    "policy", [SANITIZED_POLICY, SENSITIVE_POLICY], ids=["sanitized", "sensitive"]
)


def _spec(**fields) -> QuerySpec:
    return parse_spec(fields)


def _all(policy: QueryPolicy, kind: str) -> list[str]:
    universe = {
        "filters": set(FILTER_VALUES),
        "groups": set(GroupBy.__args__),
        "orders": {"priority", "newest", "oldest", "deadline"},
    }[kind]
    allowed = {"filters": policy.filterable, "groups": policy.aggregatable,
               "orders": policy.orders}[kind]
    return sorted(universe - allowed)


def test_every_filter_has_a_value_to_test_with():
    assert set(FILTER_VALUES) == set(FILTERS)
    assert set(FILTERS.values()) == set(Filters.__struct_fields__)


def test_the_tier_policies_match_the_index_defaults():
    assert SANITIZED_POLICY.max_limit == SENSITIVE_POLICY.max_limit == 100
    assert SENSITIVE_POLICY.filterable == {"label", "account", "source", "sent_at"}
    assert SENSITIVE_POLICY.aggregatable == {"label", "month", "account"}
    assert SANITIZED_POLICY.aggregatable == {
        "domain", "sender_domain", "organization", "month", "category", "importance",
        "facts.travel.origin_country", "facts.travel.destination_country",
        "facts.travel.start_year",
    }
    assert "label" not in SANITIZED_POLICY.filterable  # the sanitized tier has no label


@TIERS
def test_every_disallowed_filter_is_refused_by_name(policy):
    refused = _all(policy, "filters")
    assert refused  # each tier refuses something
    for name in refused:
        spec = _spec(filters={name: FILTER_VALUES[name]})
        with pytest.raises(ValueError, match=f"filter '{name}' is not allowed"):
            check(spec, policy)


@TIERS
def test_every_disallowed_group_by_is_refused_by_name(policy):
    for name in _all(policy, "groups"):
        with pytest.raises(ValueError, match=f"group_by '{name}' is not allowed"):
            check(_spec(group_by=[name]), policy)


def test_every_disallowed_order_is_refused_by_name():
    refused = _all(SENSITIVE_POLICY, "orders")
    assert refused == ["deadline", "priority"]
    for name in refused:
        with pytest.raises(ValueError, match=f"order '{name}' is not allowed"):
            build_query(_spec(order=name), SENSITIVE_POLICY, DOCUMENTS)


@TIERS
def test_every_allowed_dimension_passes(policy):
    for name in policy.filterable:
        check(_spec(filters={name: FILTER_VALUES[name]}), policy)
    check(_spec(group_by=sorted(policy.aggregatable)), policy)
    for order in policy.orders:
        check(_spec(order=order), policy)


def test_text_and_semantic_are_refused_when_the_policy_disallows_them():
    closed = msgspec.structs.replace(SANITIZED_POLICY, text=False, semantic=False)
    with pytest.raises(ValueError, match="text matching is not allowed"):
        check(_spec(text="kayak"), closed)
    with pytest.raises(ValueError, match="semantic ordering is not allowed"):
        check(_spec(semantic=[0.1, 0.2]), closed)


def test_the_default_order_must_be_allowed_too():
    no_newest = msgspec.structs.replace(SANITIZED_POLICY, orders=frozenset({"priority"}))
    with pytest.raises(ValueError, match="order 'newest' is not allowed"):
        build_query(_spec(), no_newest, SANITIZED)


def test_a_policy_cannot_reach_a_dimension_the_store_lacks():
    # A permissive policy over the documents table still has no `domain` expression.
    wide = msgspec.structs.replace(
        SENSITIVE_POLICY,
        filterable=SANITIZED_POLICY.filterable,
        aggregatable=SANITIZED_POLICY.aggregatable,
        orders=SANITIZED_POLICY.orders,
    )
    with pytest.raises(ValueError, match="filter 'domain' is not available"):
        build_query(_spec(filters={"domain": "travel"}), wide, DOCUMENTS)
    with pytest.raises(ValueError, match="filter 'deadline' is not available"):
        build_query(_spec(filters={"deadline": {"since": "2025-01-01"}}), wide, DOCUMENTS)
    with pytest.raises(ValueError, match="group_by 'domain' is not available"):
        build_aggregate(_spec(group_by=["domain"]), wide, DOCUMENTS)
    with pytest.raises(ValueError, match="order 'priority' is not available"):
        build_query(_spec(order="priority"), wide, DOCUMENTS)
    no_vectors = replace(DOCUMENTS, embedding=None, text=())
    with pytest.raises(ValueError, match="semantic ordering is not available"):
        build_query(_spec(semantic=[0.1]), wide, no_vectors)
    with pytest.raises(ValueError, match="text matching is not available"):
        build_query(_spec(text="x"), wide, no_vectors)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"filters": {"colour": "red"}}, "unknown field `colour`"),
        ({"order": "random"}, "Invalid enum value 'random'"),
        ({"filters": {"domain": "astrology"}}, "Invalid enum value 'astrology'"),
        ({"filters": {"domain": []}}, "is an empty list"),
        ({"filters": {"sent_at": {"since": "last tuesday"}}}, "invalid date"),
        ({"filters": {"sent_at": {"after": "2025-01-01"}}}, "unknown field `after`"),
        ({"limit": 0}, "Expected `int` >= 1"),
        ({"semantic": []}, "length >= 1"),
        ({"semantic": [0.1], "order": "newest"}, "ordered by distance"),
        ({"group_by": ["month", "month"]}, "names a dimension twice"),
        ({"group_by": ["sender"]}, "Invalid enum value 'sender'"),
        ({"text": ""}, "length >= 1"),
        ({"filters": {"facts.travel.destination_country": "USA"}}, "'USA' is not a two-letter"),
        ({"filters": {"facts.travel.destination_country": "U1"}}, "'U1' is not a two-letter"),
        ({"filters": {"facts.travel.origin_country": ["NO", "1x"]}}, "'1x' is not a two-letter"),
        ({"filters": {"facts.travel.destination_country": []}}, "is an empty list"),
        ({"filters": {"facts.travel.start": {"since": "spring"}}}, "invalid date"),
        ({"filters": {"facts.travel.price": 3}}, "unknown field `facts.travel.price`"),
        ({"filters": {"travel_carrier": "SAS"}}, "unknown field `travel_carrier`"),
        ({"group_by": ["facts.travel.carrier"]}, "Invalid enum value"),
        ({"filters": {"facts.travel.origin_country": ["NO"] * 5000}}, "lists 5000 values"),
        ({"filters": {"facts.travel.destination_country": ["US"] * 51}}, "at most 50"),
        ({"filters": {"domain": ["travel"] * 51}}, "lists 51 values; at most 50"),
    ],
)
def test_parse_spec_rejects_an_invalid_spec(data, message):
    with pytest.raises(ValueError, match=message):
        parse_spec(data)


def test_parse_spec_takes_one_value_or_a_list():
    spec = parse_spec({"filters": {"domain": ["travel", "work"], "importance": "high"}})
    assert [d.value for d in spec.filters.domain] == ["travel", "work"]
    assert spec.filters.importance.value == "high"
    assert spec.filters.used() == ["domain", "importance"]


def test_a_list_filter_takes_up_to_fifty_values():
    spec = parse_spec({"filters": {"facts.travel.destination_country": ["US"] * 50,
                                   "domain": ["travel"] * 50}})
    assert len(spec.filters.get("facts.travel.destination_country")) == 50
    # Bounded in validation, not as maxItems: the tool schema gains no keyword.
    assert "maxItems" not in msgspec.json.encode(msgspec.json.schema(QuerySpec)).decode()


def test_the_spec_has_a_json_schema_for_tool_input():
    schema = msgspec.json.schema(QuerySpec)
    filters = schema["$defs"]["Filters"]["properties"]
    assert set(filters) == set(FILTER_VALUES)


#: A country filter's schema, exactly as the tool has always advertised it: the
#: pattern alone, with no length keyword for a model provider to trip on.
_COUNTRY_SCHEMA = (
    b'{"anyOf":[{"type":"string","pattern":"^[A-Za-z]{2}$"},'
    b'{"type":"array","items":{"type":"string","pattern":"^[A-Za-z]{2}$"}},'
    b'{"type":"null"}],"default":null}'
)


@pytest.mark.parametrize("name", ["facts.travel.origin_country",
                                  "facts.travel.destination_country"])
def test_a_country_filter_advertises_only_the_pattern(name):
    filters = msgspec.json.schema(QuerySpec)["$defs"]["Filters"]["properties"]
    assert msgspec.json.encode(filters[name]) == _COUNTRY_SCHEMA


def _converted(country):
    return parse_spec({"filters": {"facts.travel.destination_country": country}})


def _decoded(country):
    raw = msgspec.json.encode({"filters": {"facts.travel.destination_country": country}})
    return msgspec.json.decode(raw, type=QuerySpec)


@pytest.mark.parametrize("load", [_converted, _decoded], ids=["convert", "decode"])
@pytest.mark.parametrize(
    "country", ["NO\n", "N", "NOR", "N1", "", "\u00c5L", ["no", "NO\n"], ["US", "1x"]]
)
def test_a_country_code_is_two_ascii_letters(load, country):
    with pytest.raises(msgspec.ValidationError, match="is not a two-letter country code"):
        load(country)


@pytest.mark.parametrize("load", [_converted, _decoded], ids=["convert", "decode"])
@pytest.mark.parametrize("country", ["us", "No", "NO", ["no", "SE"]])
def test_a_country_code_is_taken_in_either_case(load, country):
    assert load(country).filters.travel_destination_country == country


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2025-03-01", "2025-03-01T00:00:00+00:00"),  # a date is midnight UTC
        ("2025-03-01T10:30:00", "2025-03-01T10:30:00+00:00"),  # no zone reads as UTC
        ("2025-03-01T10:30:00+02:00", "2025-03-01T10:30:00+02:00"),
        ("14d", "2026-09-28T12:00:00+00:00"),
        ("36h", "2026-10-11T00:00:00+00:00"),
        ("2w", "2026-09-28T12:00:00+00:00"),
    ],
)
def test_resolve_bound(value, expected):
    assert resolve_bound(value, now=lambda: NOW) == expected


@pytest.mark.parametrize(
    ("value", "end", "expected"),
    [
        ("14d", False, "2026-09-28"),  # the date 14 days ago
        ("14d", True, "2026-09-29"),  # its day stays inside an end bound
        ("2025-03-10", True, "2025-03-10"),  # a date end excludes its day
        ("2025-03-10T00:00:00Z", True, "2025-03-10"),
        ("2025-03-10T12:00:00", True, "2025-03-11"),  # noon keeps the 10th
        ("2025-03-10T12:00:00", False, "2025-03-10"),
        ("2025-03-10T23:30:00-05:00", False, "2025-03-11"),  # its UTC date
    ],
)
def test_resolve_day(value, end, expected):
    assert resolve_day(value, end=end, now=lambda: NOW) == expected


def test_a_date_column_range_binds_dates_not_timestamps():
    spec = _spec(filters={"deadline": {"since": "14d", "until": "2026-10-20T09:00:00"}})
    built = build_query(spec, SANITIZED_POLICY, SANITIZED, now=lambda: NOW)
    assert "deadline >= %s AND deadline < %s" in built.sql
    assert built.params[:2] == ["2026-09-28", "2026-10-21"]


def test_relative_cutoff_is_now_minus_the_window():
    assert relative_cutoff("30m", now=lambda: NOW) == "2026-10-12T11:30:00+00:00"
    with pytest.raises(ValueError, match="invalid duration"):
        relative_cutoff("soon")


def test_ranges_resolve_relative_bounds_at_query_time():
    spec = QuerySpec(filters=Filters(sent_at=Range(since="7d", until="2026-10-12")))
    built = build_query(spec, SANITIZED_POLICY, SANITIZED, now=lambda: NOW)
    assert "sent_at >= %s AND sent_at < %s" in built.sql
    assert built.params[:2] == ["2026-10-05T12:00:00+00:00", "2026-10-12T00:00:00+00:00"]


def test_every_value_is_bound_never_inlined():
    hostile = "x'; DROP TABLE messages; --"
    spec = QuerySpec(
        filters=Filters(
            domain=hostile, sender_domain=hostile, organization=hostile, account=hostile,
        ),
        text=hostile,
    )
    built = build_query(spec, SANITIZED_POLICY, SANITIZED)
    assert hostile not in built.sql
    assert built.params[0] == [hostile]  # an any-of list
    assert hostile in built.params


def test_text_and_organization_match_literally():
    spec = QuerySpec(filters=Filters(organization="50%_off\\"), text="a_b")
    built = build_query(spec, SANITIZED_POLICY, SANITIZED)
    assert built.params[:3] == ["%50\\%\\_off\\\\%", "%a\\_b%", "%a\\_b%"]
    assert "(one_line ILIKE %s OR action_summary ILIKE %s)" in built.sql


def test_max_sensitivity_binds_the_rank():
    built = build_query(
        QuerySpec(filters=Filters(max_sensitivity="low")), SANITIZED_POLICY, SANITIZED
    )
    assert "WHEN 'high' THEN 3 END <= %s" in built.sql
    assert built.params[0] == 1


def test_limit_is_clamped_and_one_extra_row_fetched():
    built = build_query(QuerySpec(limit=500, order="priority"), SANITIZED_POLICY, SANITIZED)
    assert (built.size, built.params[-2:]) == (100, [101, 0])


def test_keyset_resumes_after_the_position():
    built = build_query(
        QuerySpec(order="oldest"), SANITIZED_POLICY, SANITIZED, ["2025-01-01 00:00:00+00", "b"]
    )
    assert "(coalesce(sent_at, 'infinity'::timestamptz), id) > (%s::timestamptz, %s)" in built.sql
    assert built.params == ["2025-01-01 00:00:00+00", "b", 51]


@pytest.mark.parametrize(
    ("order", "position"),
    [("newest", 3), ("newest", ["only-one"]), ("newest", "ab"), ("priority", ["k", "id"]),
     ("priority", -1), ("priority", True)],
)
def test_a_position_of_the_wrong_shape_is_refused(order, position):
    with pytest.raises(ValueError, match="invalid cursor position"):
        build_query(QuerySpec(order=order), SANITIZED_POLICY, SANITIZED, position)


@pytest.mark.parametrize(
    "position",
    [["not a date", "id"], ["", "id"], ["2025-13-01", "id"], ["2025-01-01", 7]],
)
def test_a_garbage_timestamp_key_is_refused_before_the_database(position):
    with pytest.raises(ValueError, match="invalid cursor position"):
        build_query(QuerySpec(order="newest"), SANITIZED_POLICY, SANITIZED, position)


@pytest.mark.parametrize(
    "key", ["2025-01-01 00:00:00+00", "2025-01-01T00:00:00.5+02:00", "infinity", "-infinity"]
)
def test_a_timestamp_key_or_sentinel_resumes(key):
    built = build_query(QuerySpec(order="newest"), SANITIZED_POLICY, SANITIZED, [key, "b"])
    assert built.params[:2] == [key, "b"]


def test_a_text_key_takes_any_string():
    # the documents tier's key is the stored string, so '' (an undated row) is valid
    built = build_query(QuerySpec(order="newest"), SENSITIVE_POLICY, DOCUMENTS, ["", "b"])
    assert built.params[:2] == ["", "b"]


def test_semantic_paging_stops_at_the_ranking_depth():
    spec = QuerySpec(semantic=[0.5, 0.25], limit=100)
    first = build_query(spec, SANITIZED_POLICY, SANITIZED)
    assert "ORDER BY summary_embedding <=> %s::vector, id" in first.sql
    assert first.params == ["[0.5,0.25]", 101, 0]

    last = build_query(spec, SANITIZED_POLICY, SANITIZED, SEMANTIC_DEPTH - 30)
    assert (last.size, last.params[-2:]) == (30, [30, SEMANTIC_DEPTH - 30])
    assert last.page([("x",)] * 30, ("id",)).next_cursor is None

    with pytest.raises(ValueError, match="invalid cursor position"):
        build_query(spec, SANITIZED_POLICY, SANITIZED, SEMANTIC_DEPTH)


def test_only_a_semantic_query_is_limited_to_rows_its_vectors_rank():
    ranked = replace(SANITIZED, ranked="vector_current")
    semantic = build_query(_spec(semantic=[0.1], filters={"domain": "travel"}),
                           SANITIZED_POLICY, ranked)
    assert " AND vector_current ORDER BY summary_embedding <=> %s::vector" in semantic.sql
    for spec in (_spec(order="newest"), _spec(order="priority", text="ferry")):
        assert "vector_current" not in build_query(spec, SANITIZED_POLICY, ranked).sql


def test_semantic_paging_stops_at_the_policys_search_depth():
    shallow = msgspec.structs.replace(SANITIZED_POLICY, search_depth=40)
    spec = QuerySpec(semantic=[0.5, 0.25], limit=100)

    assert build_query(spec, shallow, SANITIZED, 30).size == 10
    with pytest.raises(ValueError, match="invalid cursor position"):
        build_query(spec, shallow, SANITIZED, 40)


@pytest.mark.parametrize("depth", [0, SEMANTIC_DEPTH + 1])
def test_a_policy_cannot_search_past_one_hnsw_scan(depth):
    data = msgspec.to_builtins(SANITIZED_POLICY) | {"search_depth": depth}
    with pytest.raises(msgspec.ValidationError, match="search_depth"):
        msgspec.convert(data, QueryPolicy)


def test_page_reports_the_next_position():
    offset = build_query(QuerySpec(limit=2, order="priority"), SANITIZED_POLICY, SANITIZED, 4)
    page = offset.page([("a",), ("b",), ("c",)], ("id",))
    assert (page.rows, page.next_cursor) == ([{"id": "a"}, {"id": "b"}], 6)

    keyset = build_query(QuerySpec(limit=1), SANITIZED_POLICY, SANITIZED)
    page = keyset.page([("a", "k1"), ("b", "k2")], ("id",))
    assert (page.rows, page.next_cursor) == ([{"id": "a"}], ["k1", "a"])
    assert keyset.page([("a", "k1")], ("id",)).next_cursor is None


def test_query_refuses_group_by_and_aggregate_requires_it():
    with pytest.raises(ValueError, match="use aggregate"):
        build_query(_spec(group_by=["domain"]), SANITIZED_POLICY, SANITIZED)
    with pytest.raises(ValueError, match="aggregate needs group_by"):
        build_aggregate(_spec(), SANITIZED_POLICY, SANITIZED)
    for extra in ({"order": "newest"}, {"semantic": [0.1]}):
        name = next(iter(extra))
        with pytest.raises(ValueError, match=f"{name} does not apply to group_by"):
            build_aggregate(_spec(group_by=["domain"], **extra), SANITIZED_POLICY, SANITIZED)


def test_aggregate_groups_in_order_and_counts_items_apart():
    spec = _spec(group_by=["organization", "month"], filters={"domain": "travel"})
    built = build_aggregate(spec, SANITIZED_POLICY, SANITIZED)
    assert "LEFT JOIN LATERAL unnest(organizations) AS org(name) ON true" in built.sql
    assert "GROUP BY 1, 2 ORDER BY 1 NULLS LAST, 2 NULLS LAST LIMIT %s" in built.sql
    assert "JOIN" not in built.count_sql  # an item naming two organizations counts once
    assert built.params == [["travel"], 51] and built.count_params == [["travel"]]
    agg = built.result([("Acme", "2025-01", 2, None, None)] * 51, 60)
    assert (len(agg.groups), agg.total, agg.truncated) == (50, 60, True)
    assert agg.groups[0] == {
        "organization": "Acme", "month": "2025-01", "count": 2,
        "min_sent_at": None, "max_sent_at": None,
    }


# --- typed facts ----------------------------------------------------------------------


def test_a_facts_filter_is_named_by_its_path_and_maps_to_a_fixed_expression():
    spec = _spec(filters={
        "facts.travel.destination_country": "us",
        "facts.travel.start": {"since": "2025-01-01", "until": "2026-01-01"},
    })
    assert spec.filters.used() == ["facts.travel.destination_country", "facts.travel.start"]

    built = build_query(spec, SANITIZED_POLICY, SANITIZED, now=lambda: NOW)

    assert "facts->'travel'->'destination'->>'country' = ANY(%s)" in built.sql
    assert (
        "(facts->'travel'->>'start')::date >= %s AND (facts->'travel'->>'start')::date < %s"
    ) in built.sql
    # A country is compared upper-case; a start bound is a day.
    assert built.params[:3] == [["US"], "2025-01-01", "2026-01-01"]


def test_a_facts_key_never_reaches_the_sql():
    spec = _spec(
        filters={name: FILTER_VALUES[name] for name in FILTERS if name.startswith("facts.")}
    )
    built = build_query(spec, SANITIZED_POLICY, SANITIZED, now=lambda: NOW)
    assert "facts.travel" not in built.sql
    assert built.params[:3] == [True, ["NO"], ["US", "CA"]]
    assert "lower(facts->'travel'->>'carrier') = lower(%s)" in built.sql

    agg = build_aggregate(
        _spec(group_by=["facts.travel.destination_country", "facts.travel.start_year"]),
        SANITIZED_POLICY, SANITIZED,
    )
    assert "facts.travel" not in agg.sql
    assert "AS g1" in agg.sql and "AS g2" in agg.sql
    assert "to_char((facts->'travel'->>'start')::date, 'YYYY') AS g2" in agg.sql


def test_an_aggregate_by_facts_reports_each_group_by_its_dimension():
    built = build_aggregate(
        _spec(group_by=["facts.travel.destination_country", "facts.travel.start_year"],
              filters={"facts.travel": True}),
        SANITIZED_POLICY, SANITIZED,
    )
    agg = built.result([("US", "2025", 3, None, None)], 3)
    assert agg.groups == [{
        "facts.travel.destination_country": "US", "facts.travel.start_year": "2025",
        "count": 3, "min_sent_at": None, "max_sent_at": None,
    }]


def test_the_documents_tier_has_no_facts():
    wide = msgspec.structs.replace(SENSITIVE_POLICY, filterable=SANITIZED_POLICY.filterable)
    with pytest.raises(ValueError, match="filter 'facts.travel.start' is not available"):
        build_query(_spec(filters={"facts.travel.start": {"since": "2025-01-01"}}), wide,
                    DOCUMENTS)


def test_a_policy_names_facts_filters_by_their_path():
    data = msgspec.to_builtins(SENSITIVE_POLICY)
    with pytest.raises(msgspec.ValidationError, match="unknown filter"):
        msgspec.convert(data | {"filterable": ["travel_start"]}, QueryPolicy)
    allowed = msgspec.convert(data | {"filterable": ["facts.travel.start"]}, QueryPolicy)
    assert allowed.filterable == {"facts.travel.start"}


def test_a_facts_filter_is_built_from_python_by_its_attribute():
    spec = QuerySpec(filters=Filters(travel_destination_country=["US"]))
    assert spec.filters.used() == ["facts.travel.destination_country"]
    assert msgspec.to_builtins(spec.filters)["facts.travel.destination_country"] == ["US"]
