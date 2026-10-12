"""corpus.id/v1: the published vectors pin the id builder and the parser.

The vectors file is the contract other services copy, so these tests read it
rather than restating it. Every id in it is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corpus.models import ID_VERSION, MessageRef, Record, parse_id

VECTORS = json.loads(
    (Path(__file__).parents[2] / "docs" / "message-ids.vectors.json").read_text(encoding="utf-8")
)


def test_the_vectors_are_this_version():
    assert VECTORS["version"] == ID_VERSION


@pytest.mark.parametrize("vector", VECTORS["valid"], ids=lambda v: v["id"])
def test_a_valid_id_parses_into_its_parts(vector):
    ref = parse_id(vector["id"])

    assert ref == MessageRef(vector["kind"], vector["mailbox"], vector["uid"])
    assert str(ref) == vector["id"]


@pytest.mark.parametrize("vector", VECTORS["valid"], ids=lambda v: v["id"])
def test_the_record_key_builds_the_same_id(vector):
    source = f"{vector['kind']}:{vector['mailbox']}"

    assert Record.key_for(source, vector["uid"]) == vector["id"]
    assert parse_id(vector["id"]).source == source


@pytest.mark.parametrize(
    "vector", [v for v in VECTORS["valid"] if "imap" in v], ids=lambda v: v["id"]
)
def test_an_imap_uid_splits_from_the_right(vector):
    parts = vector["imap"]

    assert parse_id(vector["id"]).imap_parts() == (
        parts["folder"], parts["uidvalidity"], parts["uid"]
    )


@pytest.mark.parametrize("vector", VECTORS["invalid"], ids=lambda v: v["why"])
def test_an_invalid_id_is_refused(vector):
    with pytest.raises(ValueError):
        parse_id(vector["id"])


def test_imap_parts_is_only_for_imap():
    gmail = next(v for v in VECTORS["valid"] if v["kind"] == "gmail")

    with pytest.raises(ValueError, match="not an imap"):
        parse_id(gmail["id"]).imap_parts()


@pytest.mark.parametrize("source", ["imap:Kbl", "imap:kbl.post", "gmail:" + "a" * 33])
def test_a_source_name_outside_the_grammar_is_refused(source):
    from corpus.fetchers import build_fetcher

    with pytest.raises(ValueError, match="invalid source name"):
        build_fetcher(source)
