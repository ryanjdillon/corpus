"""The Helm chart's schema for ``tiers`` and ``sources`` keeps up with corpus.

The chart's ``values.schema.json`` describes the config file's entries by hand:
it relaxes what corpus requires (a tier named like a built-in inherits what it
leaves out), flattens the per-kind source structs into one entry and adds the
chart-only keys. So it is compared with corpus's own schema rather than
generated from it: at every object, the keys the chart accepts must be exactly
the keys corpus accepts, plus the chart's own, and no unknown key may pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import msgspec
import pytest

from corpus.tiers import ConfigFile

CHART_SCHEMA = Path(__file__).parents[2] / "deploy/helm/corpus/values.schema.json"

#: Keys the chart adds and strips before writing the config file, by path.
CHART_ONLY = {
    "tiers[]": {"deploy"},
    "tiers[].serve": {"enabled"},
    "sources[]": {"runtime"},
}

Schema = dict[str, Any]


@pytest.fixture(scope="module")
def corpus_schema() -> Schema:
    return msgspec.json.schema(ConfigFile)


@pytest.fixture(scope="module")
def chart_schema() -> Schema:
    return json.loads(CHART_SCHEMA.read_text())


def _resolve(node: Schema, root: Schema) -> Schema:
    """Follow a ``$ref`` and drop a nullable ``anyOf`` to its non-null branch."""
    while True:
        if "$ref" in node:
            node = root["$defs"][node["$ref"].rsplit("/", 1)[-1]]
        elif "anyOf" in node:
            branches = [b for b in node["anyOf"] if b.get("type") != "null"]
            assert len(branches) == 1, f"a union of {len(branches)} types is compared by hand"
            node = branches[0]
        else:
            return node


def _compare(corpus: Schema, chart: Schema, root: Schema, path: str) -> None:
    corpus = _resolve(corpus, root)
    if "properties" in corpus:
        assert chart.get("additionalProperties") is False, f"{path} accepts unknown keys"
        accepted = set(chart.get("properties", {})) - CHART_ONLY.get(path, set())
        assert accepted == set(corpus["properties"]), (
            f"{path}: missing from the chart {sorted(set(corpus['properties']) - accepted)}, "
            f"unknown to corpus {sorted(accepted - set(corpus['properties']))}"
        )
        for key, sub in corpus["properties"].items():
            _compare(sub, chart["properties"][key], root, f"{path}.{key}")
    elif "items" in corpus:
        _compare(corpus["items"], chart["items"], root, f"{path}[]")
    elif "enum" in corpus and "enum" in chart:
        assert set(chart["enum"]) == set(corpus["enum"]), f"{path}: enum differs"


def _chart_source(chart_source: Schema, kind: str) -> Schema:
    """The chart's source entry as it applies to one kind: options narrowed by ``allOf``."""
    assert kind in chart_source["properties"]["kind"]["enum"]
    options = dict(chart_source["properties"]["options"])
    for rule in chart_source.get("allOf", []):
        if rule["if"]["properties"]["kind"]["const"] == kind:
            allowed = rule["then"]["properties"]["options"]["propertyNames"]["enum"]
            options["properties"] = {
                k: v for k, v in options["properties"].items() if k in allowed
            }
    properties = {**chart_source["properties"], "kind": {"enum": [kind]}, "options": options}
    return {**chart_source, "properties": properties}


def test_tier_keys_match_corpus(corpus_schema: Schema, chart_schema: Schema) -> None:
    corpus_tiers = _resolve(corpus_schema["$defs"]["ConfigFile"]["properties"]["tiers"], corpus_schema)
    _compare(corpus_tiers, chart_schema["properties"]["tiers"], corpus_schema, "tiers")


def test_source_keys_match_corpus_per_kind(corpus_schema: Schema, chart_schema: Schema) -> None:
    sources = _resolve(corpus_schema["$defs"]["ConfigFile"]["properties"]["sources"], corpus_schema)
    variants = [_resolve(v, corpus_schema) for v in sources["items"]["anyOf"]]
    chart_source = chart_schema["properties"]["sources"]["items"]
    kinds = [v["properties"]["kind"]["enum"][0] for v in variants]
    assert set(chart_source["properties"]["kind"]["enum"]) == set(kinds)
    for kind, variant in zip(kinds, variants, strict=True):
        _compare(variant, _chart_source(chart_source, kind), corpus_schema, "sources[]")


def test_a_drifted_key_is_reported(corpus_schema: Schema, chart_schema: Schema) -> None:
    chart_tiers = json.loads(json.dumps(chart_schema["properties"]["tiers"]))
    chart_tiers["items"]["properties"]["store"]["properties"]["dns_env"] = {"type": "string"}
    corpus_tiers = corpus_schema["$defs"]["ConfigFile"]["properties"]["tiers"]
    with pytest.raises(AssertionError, match=r"unknown to corpus \['dns_env'\]"):
        _compare(corpus_tiers, chart_tiers, corpus_schema, "tiers")
