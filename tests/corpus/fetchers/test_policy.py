"""Source policy is default-deny; these tests pin that down rather than the
particular sources declared today."""

from __future__ import annotations

from corpus.fetchers import build_fetcher, policy


def test_kind_is_the_prefix_so_accounts_need_no_declaration():
    assert policy.source_kind("gmail:personal") == "gmail"
    assert policy.source_kind("gmail:work") == "gmail"
    assert policy.source_kind("imap") == "imap"


def test_mail_sources_are_enrichable():
    assert policy.may_enrich("gmail:personal")
    assert policy.may_enrich("imap:fastmail")


def test_undeclared_source_is_denied():
    assert not policy.may_enrich("youtube:@chan")
    assert policy.policy_for("youtube:@chan") is None


def test_absent_source_is_denied():
    # Absence of a declaration is absence of permission, not a free pass.
    assert not policy.may_enrich(None)
    assert not policy.may_enrich("")
    assert policy.policy_for("") is None


def test_every_declaration_records_its_rationale():
    # The registry is policy; a declaration without a reason is not reviewable.
    assert all(p.rationale for p in policy.POLICIES)


def test_enrichable_kinds_reports_only_opted_in_kinds():
    kinds = policy.enrichable_kinds()
    assert set(kinds) == {p.kind for p in policy.POLICIES if p.enrich}


def test_declared_kinds_are_known_to_build_fetcher():
    # A policy for a kind build_fetcher cannot dispatch would be dead
    # configuration. Actually constructing one needs credentials, so assert only
    # that the kind is dispatched: it may fail on config, never as "unknown".
    for p in policy.POLICIES:
        try:
            build_fetcher(f"{p.kind}:test")
        except ValueError as exc:
            assert "unknown fetcher source" not in str(exc)


def test_build_fetcher_still_rejects_an_undeclared_kind():
    # The counterpart: policy and dispatch agree on what exists.
    try:
        build_fetcher("youtube:@chan")
    except ValueError as exc:
        assert "unknown fetcher source" in str(exc)
    else:  # pragma: no cover - a youtube fetcher does not exist yet
        raise AssertionError("expected build_fetcher to reject an unknown kind")
