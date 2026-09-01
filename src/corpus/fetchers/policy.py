"""Per-source policy: what corpus may do with a source's documents.

A *source kind* is the prefix of a source id (``gmail:personal`` -> ``gmail``),
the same taxonomy :func:`corpus.fetchers.build_fetcher` dispatches on. Policy
lives beside that taxonomy so the set of known kinds is declared once rather than
duplicated into a second list that drifts.

**Enrichment is default-deny.** A source with no declaration here is not
enrichable, and adding one is a deliberate act. The reason is that enrichment is
not a neutral summarisation step: the ``Enrichment`` schema encodes the questions
you ask of *personal correspondence* -- what action does this ask of me, who am I
waiting on, how important is it -- and it drives guided decoding, so the model
cannot answer "none of this applies". Point it at a document that is not a
message and it returns well-formed, confidently-typed, fabricated metadata, which
then ranks alongside real obligations in any priority view. Silence is the
failure mode, so the default has to be the safe one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourcePolicy:
    """What may be done with documents from one source kind.

    Attributes:
        kind: Source-kind prefix, e.g. ``"gmail"``.
        enrich: Whether documents from this kind may be enriched.
        rationale: Why that decision was made; recorded so the declaration
            carries its own justification.
    """

    kind: str
    enrich: bool = False
    rationale: str = ""


# The declared source kinds. Absence from this tuple means "not enrichable".
POLICIES: tuple[SourcePolicy, ...] = (
    SourcePolicy(
        kind="gmail",
        enrich=True,
        rationale="Personal correspondence -- the domain the enrichment schema was built for.",
    ),
    SourcePolicy(
        kind="imap",
        enrich=True,
        rationale="Personal correspondence -- the domain the enrichment schema was built for.",
    ),
)


def source_kind(source: str | None) -> str:
    """Return the kind prefix of a source id (``"gmail:personal"`` -> ``"gmail"``)."""
    return (source or "").partition(":")[0]


def policy_for(source: str | None) -> SourcePolicy | None:
    """Return the policy for *source*'s kind, or ``None`` if undeclared."""
    kind = source_kind(source)
    if not kind:
        return None
    for p in POLICIES:
        if p.kind == kind:
            return p
    return None


def may_enrich(source: str | None) -> bool:
    """Whether documents from *source* may be enriched. Undeclared => ``False``."""
    policy = policy_for(source)
    return bool(policy and policy.enrich)


def enrichable_kinds() -> tuple[str, ...]:
    """Kinds currently declared enrichable, for error messages and docs."""
    return tuple(p.kind for p in POLICIES if p.enrich)
