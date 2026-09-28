#!/usr/bin/env python3
"""Validate the labelled synthetic fixture set for the enrichment evaluation.

    eval_enrich.py check [--fixtures F]

The fixture contract (field names, enum values) is derived from
``corpus.enrichment`` so it cannot drift from the schema: a label outside the
schema's enums fails loudly at load time rather than scoring as a silent miss.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from corpus import scan
from corpus.enrich_batch import _model_text
from corpus.enrichment import (
    ActionType,
    Category,
    Disposition,
    Domain,
    Importance,
    SecretSeverity,
    SensitivityLevel,
    TransactionalType,
    WaitingOn,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURES = REPO_ROOT / "tests/eval/fixtures/enrich_synthetic.jsonl"

# --------------------------------------------------------------------------- #
# Fixture contract
# --------------------------------------------------------------------------- #

#: Label fields scored as enum classifications, keyed to their schema enum.
ENUM_AXES: dict[str, type] = {
    "category": Category,
    "domain": Domain,
    "transactional_type": TransactionalType,
    "action_type": ActionType,
    "waiting_on": WaitingOn,
    "importance": Importance,
    "sensitivity_level": SensitivityLevel,
    "suggested_disposition": Disposition,
}
BOOL_AXES = ("unsubscribe_available", "requires_action", "time_sensitive")
LIST_FIELDS = ("people", "organizations", "monetary_amounts")
LABEL_FIELDS = (*ENUM_AXES, *BOOL_AXES, "deadline", *LIST_FIELDS)

#: Axes the fixture set must cover, each value at least ``MIN_PER_VALUE`` times.
STRATIFIED_AXES = ("category", "domain", "action_type", "sensitivity_level")
MIN_PER_VALUE = 8

HARD_CASES = ("fp_secret", "recovery_code", "injection", "non_message", "boundary_domain")
KINDS = ("email", "file")

#: Every fixture is fed to ``run_enrich`` under this source id. It must be a kind
#: the enrichment policy declares enrichable, or the policy gate would drop it.
EVAL_SOURCE = "imap:eval-synthetic"


class FixtureError(ValueError):
    """Raise when a fixture record violates the contract."""


def _enum_value(axis: str, value: object, rid: str) -> None:
    valid = [m.value for m in ENUM_AXES[axis]]
    if value not in valid:
        raise FixtureError(f"{rid}: labels.{axis}={value!r} is not one of {valid}")


def validate_record(rec: dict) -> None:
    """Check one fixture record against the contract; raise ``FixtureError`` if not.

    Beyond shape and enum membership, this enforces the properties the scorer
    relies on: labels that are internally coherent (a transactional type only on
    a transactional message, an action type exactly when action is required),
    and seeded secrets that appear verbatim in the body *and* trip the
    deterministic candidate gate -- otherwise production would never audit the
    record and its secret-audit ground truth would be unreachable.
    """
    rid = rec.get("id")
    if not isinstance(rid, str) or not rid:
        raise FixtureError(f"record without a string id: {str(rec)[:80]}")
    for key in ("kind", "subject", "body", "labels", "seeded_secrets", "injection"):
        if key not in rec:
            raise FixtureError(f"{rid}: missing {key!r}")
    if rec["kind"] not in KINDS:
        raise FixtureError(f"{rid}: kind={rec['kind']!r} is not one of {list(KINDS)}")
    if not isinstance(rec["body"], str) or not rec["body"].strip():
        raise FixtureError(f"{rid}: empty body")
    if not isinstance(rec.get("headers", {}), dict):
        raise FixtureError(f"{rid}: headers must be an object")

    labels = rec["labels"]
    missing = [f for f in LABEL_FIELDS if f not in labels]
    if missing:
        raise FixtureError(f"{rid}: labels missing {missing}")
    for axis in ENUM_AXES:
        _enum_value(axis, labels[axis], rid)
    for axis in BOOL_AXES:
        if not isinstance(labels[axis], bool):
            raise FixtureError(f"{rid}: labels.{axis} must be a bool")
    if labels["deadline"] is not None:
        try:
            date.fromisoformat(labels["deadline"])
        except (TypeError, ValueError) as exc:
            raise FixtureError(f"{rid}: labels.deadline {labels['deadline']!r}: {exc}") from exc
    for f in ("people", "organizations"):
        if not all(isinstance(x, str) and x for x in labels[f]):
            raise FixtureError(f"{rid}: labels.{f} must be a list of non-empty strings")
    for m in labels["monetary_amounts"]:
        if not (
            isinstance(m, dict)
            and isinstance(m.get("amount"), int | float)
            and isinstance(m.get("currency"), str)
            and re.fullmatch(r"[A-Z]{3}", m["currency"])
        ):
            raise FixtureError(f"{rid}: bad monetary amount {m!r}")

    is_tx = labels["category"] == Category.transactional.value
    has_tx_type = labels["transactional_type"] != TransactionalType.none.value
    if is_tx != has_tx_type:
        raise FixtureError(
            f"{rid}: transactional_type={labels['transactional_type']!r} "
            f"inconsistent with category={labels['category']!r}"
        )
    if labels["requires_action"] != (labels["action_type"] != ActionType.none.value):
        raise FixtureError(f"{rid}: requires_action and action_type disagree")

    hard = rec.get("hard_case")
    if hard is not None and hard not in HARD_CASES:
        raise FixtureError(f"{rid}: hard_case={hard!r} is not one of {list(HARD_CASES)}")
    if not isinstance(rec["injection"], bool):
        raise FixtureError(f"{rid}: injection must be a bool")
    if hard == "non_message" and rec["kind"] != "file":
        raise FixtureError(f"{rid}: non_message records must have kind='file'")

    severities = [s.value for s in SecretSeverity]
    candidates = set(scan.audit_candidates(rec["body"])) if rec["seeded_secrets"] else set()
    for s in rec["seeded_secrets"]:
        if s.get("severity") not in severities:
            raise FixtureError(f"{rid}: secret severity {s.get('severity')!r} not in {severities}")
        value = s.get("value")
        if value is None:
            if s["severity"] != SecretSeverity.reference.value:
                raise FixtureError(f"{rid}: only a 'reference' secret may omit its value")
        elif value not in rec["body"]:
            raise FixtureError(f"{rid}: seeded {s['type']} value is not in the body")
        if s.get("type") not in candidates:
            raise FixtureError(
                f"{rid}: seeded {s.get('type')!r} does not trip the detectors "
                f"(candidates: {sorted(candidates)}), so it would never be audited"
            )


def load_fixtures(path: Path | str) -> list[dict]:
    """Read and validate a fixture JSONL file; raise on the first bad record."""
    records: list[dict] = []
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FixtureError(f"{path}:{n}: {exc}") from exc
        validate_record(rec)
        if rec["id"] in seen_ids:
            raise FixtureError(f"duplicate fixture id {rec['id']!r}")
        # Metering is keyed on the model input text, so two identical inputs
        # would be indistinguishable in the run output.
        text = model_text(rec)
        if text in seen_texts:
            raise FixtureError(f"{rec['id']}: duplicate subject+body")
        seen_ids.add(rec["id"])
        seen_texts.add(text)
        records.append(rec)
    return records


def coverage(records: Iterable[dict]) -> dict[str, dict[str, int]]:
    """Count label values per stratified axis, including zero counts."""
    counts = {axis: {m.value: 0 for m in ENUM_AXES[axis]} for axis in STRATIFIED_AXES}
    for rec in records:
        for axis in STRATIFIED_AXES:
            counts[axis][rec["labels"][axis]] += 1
    return counts


def coverage_gaps(records: Iterable[dict], minimum: int = MIN_PER_VALUE) -> list[str]:
    """Return ``axis=value (n)`` for every stratified value below ``minimum``."""
    return [
        f"{axis}={value} ({n})"
        for axis, values in coverage(records).items()
        for value, n in values.items()
        if n < minimum
    ]


def select(records: list[dict], only: str | None = None, limit: int = 0) -> list[dict]:
    """Filter records by an ``only`` expression (``field=value``) and a limit.

    ``field`` is a top-level fixture key (``hard_case``, ``kind``, ``injection``)
    or ``labels.<axis>``; ``hard_case=none`` selects the ordinary records.
    """
    if only:
        field, sep, want = only.partition("=")
        if not sep:
            raise ValueError(f"--only expects field=value, got {only!r}")

        def get(rec: dict):
            if field.startswith("labels."):
                return rec["labels"].get(field.removeprefix("labels."))
            return rec.get(field)

        def matches(value) -> bool:
            if value is None:
                return want in ("none", "null")
            return str(value).lower() == want.lower()

        records = [r for r in records if matches(get(r))]
    return records[:limit] if limit else records


def model_text(rec: dict) -> str:
    """Return the text the enricher sees for ``rec``: production's subject+body framing."""
    return _model_text({"subject": rec["subject"]}, rec["body"])


def main(argv: list[str] | None = None) -> int:
    """Validate a fixture file and report its coverage."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check", help="validate a fixture file and report its coverage")
    check.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    args = parser.parse_args(argv)

    records = load_fixtures(args.fixtures)
    for axis, values in coverage(records).items():
        print(f"{axis}: " + ", ".join(f"{v}={n}" for v, n in values.items()))
    hard = {h: sum(r.get("hard_case") == h for r in records) for h in HARD_CASES}
    print("hard_case: " + ", ".join(f"{h}={n}" for h, n in hard.items()))
    print(f"{len(records)} records valid")
    gaps = coverage_gaps(records)
    if gaps:
        print("under-covered: " + ", ".join(gaps), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
