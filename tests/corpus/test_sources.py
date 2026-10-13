"""Sources: one config entry each, validated strictly, with the pre-file behaviour kept.

Registries are built from a file or from ``None`` (no ``sources:``) directly, and
fetchers get their environment injected, so nothing reads the process's own
configuration except the tests of the process-level functions. Every credential
here is a placeholder.
"""

from __future__ import annotations

import logging
import textwrap
from typing import ClassVar

import msgspec
import pytest

from corpus import fetchers, sources, tiers
from corpus.config import Settings, settings
from corpus.fetchers.gmail import GmailFetcher
from corpus.fetchers.imap import ImapFetcher
from corpus.sources import (
    GmailOptions,
    GmailSource,
    ImapOptions,
    ImapSource,
    PipelineOptions,
    Registry,
    SourceConfigError,
    build_fetcher,
)

FILE = """\
sources:
  - name: personal
    kind: gmail
    enrich: true
    options: {labels: [INBOX, Receipts]}
  - name: kasserar
    kind: imap
    credentials_env_prefix: KBL_KASSERAR
    enrich: true
    options: {folders: [INBOX]}
    pipeline: {batch_size: 20, limit: 400, tier: partner}
  - name: notices
    kind: imap
    enrich: false
"""


@pytest.fixture
def config_file(tmp_path):
    def write(text: str) -> str:
        path = tmp_path / "corpus.yaml"
        path.write_text(textwrap.dedent(text))
        return str(path)

    return write


def declared(path: str) -> Registry:
    return Registry(tiers.read(path).sources)


@pytest.fixture
def process_config(monkeypatch):
    """Point the process at *path* (or no file) and reload its registries."""

    def use(path: str = "") -> None:
        monkeypatch.setattr(settings, "config_file", path)
        monkeypatch.setattr(settings, "tiers_file", "")
        sources.registry.cache_clear()
        tiers.registry.cache_clear()
        tiers._deprecated.cache_clear()  # each deprecation logs once per process

    yield use
    sources.registry.cache_clear()
    tiers.registry.cache_clear()


# --- the file -----------------------------------------------------------------


def test_a_file_declares_each_source_once(config_file):
    reg = declared(config_file(FILE))

    assert [s.id for s in reg.sources()] == ["gmail:personal", "imap:kasserar", "imap:notices"]
    personal = reg.source("gmail:personal")
    assert personal == GmailSource(
        name="personal", enrich=True, options=GmailOptions(labels=["INBOX", "Receipts"])
    )
    kasserar = reg.source("imap:kasserar")
    assert kasserar.env_prefix == "KBL_KASSERAR"
    assert kasserar.options == ImapOptions(folders=["INBOX"])
    assert kasserar.pipeline == PipelineOptions(batch_size=20, limit=400, tier="partner")
    assert reg.source("imap:notices").pipeline == PipelineOptions()


@pytest.mark.parametrize(
    ("given", "resolved"),
    [
        ({}, PipelineOptions(batch_size=20, limit=400, tier="partner")),
        ({"limit": 0}, PipelineOptions(batch_size=20, limit=0, tier="partner")),
        ({"batch_size": 5, "tier": "sanitized"}, PipelineOptions(batch_size=5, limit=400)),
    ],
)
def test_a_given_option_overrides_the_entry_and_none_keeps_it(given, resolved):
    entry = PipelineOptions(batch_size=20, limit=400, tier="partner")
    assert entry.override(**given) == resolved
    assert entry.override(batch_size=None, limit=None, tier=None) == entry


def test_a_file_with_only_sources_keeps_the_default_tiers(config_file):
    path = config_file(FILE)
    assert tiers.load(path).tiers == tiers.DEFAULT.tiers


def test_a_file_with_only_tiers_keeps_the_pre_file_sources(config_file):
    text = msgspec.yaml.encode(tiers.DEFAULT).decode()
    reg = Registry(tiers.read(config_file(text)).sources)
    assert not reg.declared
    assert reg.source("imap:anything") == ImapSource(name="anything", enrich=True)


def test_an_undeclared_source_is_refused_naming_the_declared_ones(config_file):
    with pytest.raises(ValueError, match=r"no source 'imap:other'.*gmail:personal, imap:kasserar"):
        declared(config_file(FILE)).source("imap:other")


def test_an_empty_list_declares_no_sources(config_file):
    reg = declared(config_file("sources: []\n"))
    assert reg.declared and reg.sources() == ()
    assert not reg.may_enrich("gmail:personal")
    with pytest.raises(ValueError, match="declared: none"):
        reg.source("gmail:personal")


@pytest.mark.parametrize(
    ("edit", "error"),
    [
        (lambda f: f.replace("kind: gmail", "kind: jmap"), r"Invalid value 'jmap'.*sources\[0\]\.kind"),
        (lambda f: f.replace("    kind: gmail\n", ""), r"missing required field `kind`"),
        (lambda f: f.replace("name: personal", "name: Personal"), r"sources\[0\]\.name"),
        (lambda f: f.replace("name: personal", "name: kbl.post"), r"sources\[0\]\.name"),
        (lambda f: f.replace("name: personal", "name: " + "a" * 33), r"sources\[0\]\.name"),
        (lambda f: f.replace("kind: gmail\n    enrich: true", "kind: gmail\n    colour: red\n    enrich: true"),
         r"unknown field `colour`"),
        (lambda f: f.replace("kind: gmail\n    enrich: true", "kind: gmail\n    password: x\n    enrich: true"),
         r"unknown field `password`"),
        (lambda f: f.replace("kind: gmail\n    enrich: true\n", "kind: gmail\n"),
         r"missing required field `enrich`"),
        (lambda f: f.replace("{folders: [INBOX]}", "{labels: [INBOX]}"), r"unknown field `labels`"),
        (lambda f: f.replace("{labels: [INBOX, Receipts]}", "{labels: []}"), r"length >= 1"),
        (lambda f: f.replace("KBL_KASSERAR", "kbl_kasserar"), r"credentials_env_prefix"),
        (lambda f: f.replace("limit: 400", "limit: -1"), r"pipeline\.limit"),
        (lambda f: f.replace("batch_size: 20", "batch_size: 0"), r"pipeline\.batch_size"),
    ],
)
def test_a_bad_source_is_refused_with_a_clear_error(config_file, edit, error):
    with pytest.raises(tiers.ConfigError, match=error):
        declared(config_file(edit(FILE)))


def test_a_source_declared_twice_is_refused(config_file):
    text = FILE.replace("name: notices", "name: kasserar")
    with pytest.raises(SourceConfigError, match="source 'imap:kasserar' is declared twice"):
        declared(config_file(text))


def test_the_same_name_under_two_kinds_is_two_sources(config_file):
    reg = declared(config_file(FILE.replace("name: notices", "name: personal")))
    assert {s.id for s in reg.sources()} >= {"gmail:personal", "imap:personal"}


# --- without sources: the behaviour from before the file had any ---------------


@pytest.mark.parametrize(
    ("source", "enrichable"),
    [
        ("gmail:personal", True),
        ("gmail:work", True),
        ("imap:fastmail", True),
        # By kind alone, as before: ids stored before the name grammar keep it.
        ("imap:kbl.post", True),
        ("imap", True),
        ("youtube:@chan", False),
        ("files:notes", False),
        ("", False),
        (None, False),
    ],
)
def test_without_sources_enrichment_is_by_kind_as_before(source, enrichable):
    assert Registry(None).may_enrich(source) is enrichable


@pytest.mark.parametrize("kind", ["gmail", "imap"])
@pytest.mark.parametrize("name", ["personal", "kbl_post", "kbl-post", "x1"])
def test_the_default_prefix_reproduces_the_pre_file_variable_names(kind, name):
    # The names gmail.py and imap.py composed before: CORPUS_<KIND>_<NAME.upper()>_<KEY>.
    source = Registry(None).source(f"{kind}:{name}")
    for key in ("HOST", "PASSWORD", "CLIENT_ID", "REFRESH_TOKEN", "FOLDERS", "LABELS"):
        assert source.env_name(key) == f"CORPUS_{kind.upper()}_{name.upper()}_{key}"


def test_a_synthesised_source_is_the_kinds_defaults():
    reg = Registry(None)
    assert reg.source("gmail:personal") == GmailSource(name="personal", enrich=True)
    assert reg.source("imap:example") == ImapSource(name="example", enrich=True)
    assert reg.sources() == ()


def test_without_sources_an_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="unknown fetcher source: 'youtube:@chan'"):
        Registry(None).source("youtube:@chan")


def test_without_sources_the_process_logs_the_deprecation_once(process_config, caplog):
    process_config()
    with caplog.at_level(logging.WARNING, logger="corpus.sources"):
        sources.source("imap:example")
        sources.may_enrich("gmail:personal")
    assert caplog.text.count("no sources: in the config file") == 1


# --- enrichment ---------------------------------------------------------------


def test_enrichment_follows_each_entry(config_file):
    reg = declared(config_file(FILE))
    assert reg.may_enrich("gmail:personal") and reg.may_enrich("imap:kasserar")
    assert not reg.may_enrich("imap:notices")
    assert not reg.may_enrich("imap:undeclared")
    assert not reg.may_enrich(None)
    assert reg.may_enrich(ImapSource(name="adhoc", enrich=True))


def test_an_entry_declaring_enrich_false_is_told_apart_from_a_missing_one(config_file):
    reg = declared(config_file(FILE))
    assert reg.declares("imap:notices") and reg.declares("gmail:personal")
    assert not reg.declares("imap:undeclared")
    assert not reg.declares(None)
    # Without sources:, a kind with a policy is the declaration.
    assert Registry(None).declares("imap:anything")
    assert not Registry(None).declares("youtube:@chan")


def test_a_refusal_says_how_to_allow_it(config_file):
    with pytest.raises(ValueError, match="'imap:notices' is not declared enrichable.*enrich: true"):
        declared(config_file(FILE)).require_enrichable("imap:notices")
    with pytest.raises(ValueError, match="enrichable kinds are gmail, imap"):
        Registry(None).require_enrichable("youtube:@chan")
    Registry(None).require_enrichable(ImapSource(name="x", enrich=True))


# --- build_fetcher --------------------------------------------------------------

GMAIL_ENV = {
    "CORPUS_GMAIL_PERSONAL_CLIENT_ID": "cid",
    "CORPUS_GMAIL_PERSONAL_CLIENT_SECRET": "cs",
    "CORPUS_GMAIL_PERSONAL_REFRESH_TOKEN": "rt",
}
IMAP_ENV = {
    "KBL_KASSERAR_HOST": "imap.example.com",
    "KBL_KASSERAR_USER": "kasserar",
    "KBL_KASSERAR_PASSWORD": "x",
}


def test_build_fetcher_dispatches_on_the_kind(config_file):
    reg = declared(config_file(FILE))

    gmail = build_fetcher(reg.source("gmail:personal"), environ=GMAIL_ENV)
    imap = build_fetcher(reg.source("imap:kasserar"), environ=IMAP_ENV)

    assert isinstance(gmail, GmailFetcher) and gmail.source == "gmail:personal"
    assert gmail.labels == ["INBOX", "Receipts"]
    assert isinstance(imap, ImapFetcher) and imap.source == "imap:kasserar"
    assert (imap.host, imap.user, imap.folders) == ("imap.example.com", "kasserar", ["INBOX"])


def test_build_fetcher_reads_the_pre_file_names_for_a_synthesised_source():
    env = {
        "CORPUS_IMAP_EXAMPLE_HOST": "imap.example.com",
        "CORPUS_IMAP_EXAMPLE_PORT": "1993",
        "CORPUS_IMAP_EXAMPLE_USER": "user@example.com",
        "CORPUS_IMAP_EXAMPLE_PASSWORD": "x",
        "CORPUS_IMAP_EXAMPLE_FOLDERS": "INBOX,Archive",
        "CORPUS_IMAP_EXAMPLE_SSL": "false",
    }
    fetcher = build_fetcher(Registry(None).source("imap:example"), environ=env)
    assert (fetcher.host, fetcher.port, fetcher.user) == ("imap.example.com", 1993, "user@example.com")
    assert (fetcher.folders, fetcher.ssl) == (["INBOX", "Archive"], False)


def test_build_fetcher_does_not_read_another_sources_variables(config_file):
    # The explicit prefix replaces the default: CORPUS_IMAP_KASSERAR_* is not read.
    env = {k.replace("KBL_KASSERAR", "CORPUS_IMAP_KASSERAR"): v for k, v in IMAP_ENV.items()}
    with pytest.raises(ValueError, match="KBL_KASSERAR_HOST"):
        build_fetcher(declared(config_file(FILE)).source("imap:kasserar"), environ=env)


def test_build_fetcher_refuses_a_kind_it_has_no_fetcher_for():
    class Unwired(sources.Source, tag="unwired"):
        kind: ClassVar[str] = "unwired"

    with pytest.raises(ValueError, match="no fetcher for source kind 'unwired'"):
        build_fetcher(Unwired(name="x", enrich=False), environ={})


# --- the process-level functions and the deprecated paths -----------------------


def test_the_process_reads_sources_from_the_config_file(process_config, config_file):
    process_config(config_file(FILE))
    assert [s.id for s in sources.sources()] == ["gmail:personal", "imap:kasserar", "imap:notices"]
    assert sources.source("imap:kasserar").pipeline.limit == 400
    assert not sources.may_enrich("imap:notices")
    with pytest.raises(ValueError, match="not declared enrichable"):
        sources.require_enrichable("imap:notices")


def test_the_tiers_file_variable_still_names_the_config_file(
    monkeypatch, process_config, config_file, caplog
):
    process_config()
    monkeypatch.setattr(settings, "tiers_file", config_file(FILE))
    with caplog.at_level(logging.WARNING, logger="corpus.tiers"):
        assert sources.source("imap:kasserar").env_prefix == "KBL_KASSERAR"
    assert "CORPUS_TIERS_FILE is deprecated" in caplog.text


@pytest.mark.parametrize(
    ("config_file_", "tiers_file", "path"),
    [("", "", None), ("/a.yaml", "", "/a.yaml"), ("", "/b.yaml", "/b.yaml"),
     ("/a.yaml", "/a.yaml", "/a.yaml")],
)
def test_the_config_path(config_file_, tiers_file, path):
    conf = Settings(config_file=config_file_, tiers_file=tiers_file)
    assert tiers.config_path(conf) == path


def test_two_different_config_files_are_refused():
    with pytest.raises(tiers.ConfigError, match="name different files"):
        tiers.config_path(Settings(config_file="/a.yaml", tiers_file="/b.yaml"))


def test_the_fetchers_shim_still_builds_by_id_with_a_warning(process_config, monkeypatch):
    process_config()
    for key, value in {"HOST": "imap.example.com", "USER": "u", "PASSWORD": "x"}.items():
        monkeypatch.setenv(f"CORPUS_IMAP_DEFAULT_{key}", value)
    with pytest.warns(DeprecationWarning, match="corpus.sources.build_fetcher"):
        fetcher = fetchers.build_fetcher("imap")  # no name: "default", as before
    assert fetcher.source == "imap:default"
    with pytest.warns(DeprecationWarning), pytest.raises(ValueError, match="unknown fetcher"):
        fetchers.build_fetcher("youtube:@chan")
