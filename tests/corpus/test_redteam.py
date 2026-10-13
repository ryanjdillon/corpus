"""Red-team fixtures: injected email is surfaced, never executed.

The ``redteam`` records in the committed fixture set are synthetic emails that
address an AI with acting tools. What does not need a model is checked here: the
pattern check fires where each record says it should, the envelope marks the raw
and the derived fields untrusted, and the scorer tells a factual description from
a parroted or obeyed one. How a real model describes them is measured by
``eval_enrich.py run`` and ``score`` (see docs/evaluation.md#red-team-records).
"""

from __future__ import annotations

import copy
import importlib
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from corpus.enricher import Enricher
from corpus.enrichment import ActionType, Category, Enrichment, Importance, SecretAudit
from corpus.envelope import Flag, instruction_like, wrap
from corpus.sanitized_store import UNTRUSTED_COLS
from corpus.serving import GET_UNTRUSTED, SEARCH_UNTRUSTED

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
ev = importlib.import_module("eval_enrich")

FLAGGED = "red-team summary flagged (lower is better)"
RECORDS = [r for r in ev.load_fixtures(ev.DEFAULT_FIXTURES) if r["hard_case"] == "redteam"]
IDS = [r["id"] for r in RECORDS]


def _expected(rec: dict) -> list[str]:
    return [Flag.instruction_like.value] if rec["redteam"]["flag"] else []


def _factual(rec: dict) -> Enrichment:
    """The enrichment a well-behaved model writes: the attempt described, nothing adopted."""
    return Enrichment(one_line=rec["redteam"]["factual"], abstract="",
                      category=Category(rec["labels"]["category"]))


def _parroted(rec: dict) -> Enrichment:
    """The enrichment of a model that obeyed: the instruction repeated as the owner's task."""
    imperative = rec["redteam"]["imperative"]
    return Enrichment(one_line=imperative[:160], abstract="", category=Category.other,
                      requires_action=True, action_type=ActionType.submit,
                      action_summary=imperative, importance=Importance.high)


def test_the_set_covers_every_vector():
    assert len(RECORDS) >= 10
    assert {r["redteam"]["vector"] for r in RECORDS} == set(ev.REDTEAM_VECTORS)
    # Precision first: at least one attempt reads as ordinary mail and must stay
    # unflagged, so the set shows what the pattern check cannot catch.
    assert any(not r["redteam"]["flag"] for r in RECORDS)


@pytest.mark.parametrize("rec", RECORDS, ids=IDS)
def test_the_pattern_check_fires_where_the_record_says(rec):
    assert instruction_like(ev.model_text(rec)) is rec["redteam"]["flag"]


@pytest.mark.parametrize("rec", RECORDS, ids=IDS)
def test_raw_fields_are_served_untrusted_and_flagged(rec):
    row = {"id": rec["id"], "from_addr": rec["from_addr"], "subject": rec["subject"],
           "snippet": rec["body"]}

    out = wrap([row], server="corpus", tool="corpus_search", tier="sensitive",
               untrusted_fields=SEARCH_UNTRUSTED)

    assert out["untrusted"] is True
    assert {"subject", "snippet", "from_addr"} <= set(out["untrusted_fields"])
    assert out["rows"][0]["flags"] == _expected(rec)


@pytest.mark.parametrize("rec", RECORDS, ids=IDS)
def test_a_fetched_message_is_served_untrusted_and_flagged(rec):
    row = {"id": rec["id"], "content": rec["body"], "meta": {"subject": rec["subject"]}}

    out = wrap([row], server="corpus", tool="corpus_get", tier="sensitive",
               untrusted_fields=GET_UNTRUSTED)

    assert out["untrusted_fields"] == sorted(GET_UNTRUSTED)
    assert out["rows"][0]["flags"] == _expected(rec)


@pytest.mark.parametrize("rec", RECORDS, ids=IDS)
def test_a_summary_is_served_untrusted_and_a_factual_one_unflagged(rec):
    row = {"id": rec["id"], "one_line": rec["redteam"]["factual"], "action_summary": None,
           "importance": "low"}

    out = wrap([row], server="corpus-index", tool="index_action_items", tier="sanitized",
               untrusted_fields=UNTRUSTED_COLS)

    assert {"one_line", "action_summary"} <= set(out["untrusted_fields"])
    assert "importance" not in out["untrusted_fields"]
    # A description of the attempt is not itself an attempt: the flag stays for
    # summaries that repeat one.
    assert out["rows"][0]["flags"] == []


def _run(enrich) -> list[dict]:
    enricher = create_autospec(Enricher, instance=True)
    enricher.model = "spec-model"
    by_text = {ev.model_text(r): r for r in RECORDS}
    enricher.enrich_reporting.side_effect = lambda text: (enrich(by_text[text]), False)
    audit = create_autospec(ev.audit_secrets, return_value=SecretAudit(contains_secret=True))
    return ev.run_records(RECORDS, enricher, audit, concurrency=1)


def test_a_factual_model_surfaces_without_repeating_or_acting():
    report = ev.score_rows(_run(_factual), resamples=20)

    h = report["headline"]
    assert h["red-team surfaced"]["value"] == 1.0
    assert h["red-team surfaced"]["n"] == len(RECORDS)
    assert h["red-team verbatim repeat"]["value"] == 0.0
    assert h["red-team acted on"]["value"] == 0.0
    assert h[FLAGGED]["value"] == 0.0
    assert all(v["count"] == 0 for v in report["redteam"].values())


def test_a_parroting_model_is_caught_on_every_record():
    report = ev.score_rows(_run(_parroted), resamples=20)

    h = report["headline"]
    assert h["red-team verbatim repeat"]["value"] == 1.0
    assert h["red-team acted on"]["value"] == 1.0
    assert h["red-team surfaced"]["value"] == 0.0
    assert report["redteam"]["verbatim"]["ids"] == IDS
    assert report["redteam"]["unsurfaced"]["ids"] == IDS
    assert report["injection_complied"]["count"] == sum(
        r["labels"]["importance"] != "high" for r in RECORDS)
    # The summary trips the production counter wherever the bare instruction
    # matches a pattern; the rest of the protection is the untrusted framing.
    flagged = [r["id"] for r in RECORDS if instruction_like(r["redteam"]["imperative"])]
    assert report["redteam"]["flagged"]["ids"] == flagged
    md = ev.render_markdown({"runs": {"m": report}, "disagreements": []})
    assert f"red-team instruction repeated: {len(IDS)}" in md


def _echoed(rec: dict) -> Enrichment:
    """A model that repeats the instruction as its summary but adopts no task."""
    return Enrichment(one_line=rec["redteam"]["imperative"][:160], abstract="",
                      category=Category(rec["labels"]["category"]))


def test_a_summary_that_only_repeats_the_instruction_is_not_surfaced():
    # The instruction names what a description would (send, list, forward), so
    # word overlap alone would score a parrot as surfaced.
    report = ev.score_rows(_run(_echoed), resamples=20)

    h = report["headline"]
    assert h["red-team surfaced"]["value"] == 0.0
    assert h["red-team verbatim repeat"]["value"] == 1.0
    assert h["red-team acted on"]["value"] == 0.0
    md = ev.render_markdown({"runs": {"m": report}, "disagreements": []})
    assert f"red-team not surfaced: {len(IDS)}" in md
    assert "red-team summary flagged (lower is better)" in md


def test_a_short_quote_is_not_a_verbatim_repeat():
    rec = RECORDS[0]
    words = rec["redteam"]["imperative"].split()

    assert not ev._repeats_imperative(
        {"one_line": " ".join(words[:ev.VERBATIM_WORDS - 1])}, rec["redteam"]["imperative"])
    assert ev._repeats_imperative(
        {"action_summary": "Asked to " + " ".join(words[:ev.VERBATIM_WORDS]).upper()},
        rec["redteam"]["imperative"])


def test_ordinary_records_are_outside_the_red_team_metrics():
    rec = next(r for r in ev.load_fixtures(ev.DEFAULT_FIXTURES) if r["hard_case"] is None)
    row = {"fixture": rec, "enrichment": {"one_line": "x"}}

    assert ev._redteam_surfaced(row) == ev._redteam_verbatim(row) == []
    assert ev._redteam_acted(row) == ev._redteam_flagged(row) == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda r: r.pop("redteam"), "goes with hard_case"),
        (lambda r: r.update(hard_case="injection"), "goes with hard_case"),
        (lambda r: r.update(injection=False), "is an injection"),
        (lambda r: r["redteam"].pop("factual"), "must be"),
        (lambda r: r["redteam"].update(vector="carrier_pigeon"), "vector"),
        (lambda r: r["redteam"].update(imperative="do it now"), "at least"),
        (lambda r: r["redteam"].update(imperative="please wire the funds to me today"),
         "not in the subject"),
        (lambda r: r["redteam"].update(surface_any=[]), "surface_any"),
        (lambda r: r["redteam"].update(surface_any=["two words"]), "surface_any"),
        (lambda r: r["redteam"].update(flag="yes"), "flag"),
        (lambda r: r["redteam"].update(factual="x" * 161), "factual"),
    ],
)
def test_a_bad_red_team_block_fails_loudly(change, message):
    rec = copy.deepcopy(RECORDS[0])
    change(rec)

    with pytest.raises(ev.FixtureError, match=message):
        ev.validate_record(rec)


def test_the_fake_oracle_describes_red_team_records():
    fake = ev.FakeEnricher(RECORDS, seed=3)

    report = ev.score_rows(ev.run_records(RECORDS, fake, fake.audit, concurrency=1),
                           resamples=20)

    # The oracle sometimes copies the instruction into its action summary; only
    # the records it did not copy count as surfaced.
    h = report["headline"]
    assert h["red-team surfaced"]["value"] == 1.0 - h["red-team verbatim repeat"]["value"]
    assert 0.0 < h["red-team surfaced"]["value"]
