"""The tier registry: each tier is defined once, in the tiers file or the default.

Settings and the environment are injected, so nothing reads the process's own.
Every DSN here is a placeholder.
"""

from __future__ import annotations

import textwrap

import msgspec
import pytest

from corpus import tiers
from corpus.config import Settings
from corpus.query import SANITIZED_POLICY, SEMANTIC_DEPTH, SENSITIVE_POLICY
from corpus.sanitized_store import COLS

RAW_DSN = "postgresql://raw-placeholder@db.example.com/ai"
SANITIZED_DSN = "postgresql://sync-placeholder@db.example.com/ai_sanitized"
READER_DSN = "postgresql://reader-placeholder@db.example.com/ai_sanitized"
ENV = {"CORPUS_SANITIZED_DATABASE_URL": SANITIZED_DSN, "CORPUS_INDEX_DATABASE_URL": READER_DSN}

FILE = """\
tiers:
  - name: sensitive
    store: {dsn_env: CORPUS_DATABASE_URL}
    serve: {server_name: corpus-local, access: [pi-local]}
    query:
      max_limit: 50
      filterable: [label, sent_at]
      aggregatable: [label, month]
      orders: [newest]
      text: true
      semantic: false
  - name: sanitized
    store: {dsn_env: CORPUS_SANITIZED_DATABASE_URL, reader_dsn_env: CORPUS_INDEX_DATABASE_URL, schema: kbl}
    projection: {from: sensitive, fields: [one_line, domain], sensitivity_gate: medium}
    serve: {server_name: corpus-index-example, access: [orchestrator, pi]}
    query:
      max_limit: 20
      search_depth: 100
      filterable: [domain]
      aggregatable: [domain]
      orders: [priority, newest]
      text: true
      semantic: true
"""


def settings(**values) -> Settings:
    """Settings with *values* set, as if from the environment; the rest default."""
    return Settings(**{"database_url": RAW_DSN, "db_schema": "corpus", **values})


def registry(path=None, environ=ENV, **values) -> tiers.Registry:
    conf = settings(**values)
    return tiers.Registry(tiers.load(path, conf), conf, environ)


@pytest.fixture
def tiers_file(tmp_path):
    def write(text: str) -> str:
        path = tmp_path / "tiers.yaml"
        path.write_text(textwrap.dedent(text))
        return str(path)

    return write


# --- the built-in default ----------------------------------------------------


def test_the_default_tiers_are_the_configuration_before_the_file():
    # The same names, servers, stores, policies, gate and (no) access as before
    # the tiers file existed, so a deployment without one is unchanged.
    sensitive, sanitized = registry().tiers()

    assert (sensitive.name, sensitive.server_name, sensitive.policy) == (
        "sensitive", "corpus", SENSITIVE_POLICY
    )
    assert (sensitive.dsn, sensitive.dsn_env, sensitive.schema) == (
        RAW_DSN, "CORPUS_DATABASE_URL", "corpus"
    )
    assert (sensitive.reader_dsn_env, sensitive.projection, sensitive.access) == (None, None, ())

    assert (sanitized.name, sanitized.server_name, sanitized.policy) == (
        "sanitized", "corpus-index", SANITIZED_POLICY
    )
    assert (sanitized.dsn, sanitized.reader_dsn, sanitized.schema) == (
        SANITIZED_DSN, READER_DSN, "corpus"
    )
    assert sanitized.projection == tiers.Projection("sensitive", COLS, "high")
    assert sanitized.access == ()
    assert SANITIZED_POLICY.search_depth == SENSITIVE_POLICY.search_depth == SEMANTIC_DEPTH


def test_the_default_follows_the_source_schema():
    # As before: an unset sanitized schema is CORPUS_DB_SCHEMA.
    conf = Settings(database_url=RAW_DSN, db_schema="mbx_post")
    reg = tiers.Registry(tiers.load(None, conf), conf, ENV)

    assert reg.tier("sanitized").schema == reg.tier("sensitive").schema == "mbx_post"


def test_the_process_registry_is_the_default_without_a_file():
    assert [t.name for t in tiers.tiers()] == ["sensitive", "sanitized"]
    assert tiers.tier("sanitized").server_name == tiers.registry().tier("sanitized").server_name


# --- deprecated overrides ------------------------------------------------------


def test_the_deprecated_variables_override_the_default(caplog):
    reg = registry(
        index_server_name="corpus-index-kbl", sanitized_db_schema="kbl",
        index_sensitivity_gate="medium", tier_access={"sanitized": ["orchestrator"]},
    )
    sanitized = reg.tier("sanitized")

    assert (sanitized.server_name, sanitized.schema) == ("corpus-index-kbl", "kbl")
    assert sanitized.projection.sensitivity_gate == "medium"
    assert (sanitized.access, reg.tier("sensitive").access) == (("orchestrator",), ())
    assert "deprecated" in caplog.text and "CORPUS_INDEX_SERVER_NAME" in caplog.text


def test_an_override_wins_over_the_file(tiers_file):
    reg = registry(tiers_file(FILE), index_server_name="corpus-index-kbl")

    assert reg.tier("sanitized").server_name == "corpus-index-kbl"
    assert reg.tier("sanitized").access == ("orchestrator", "pi")  # not overridden


def test_an_empty_sanitized_schema_does_not_override(tiers_file):
    assert registry(tiers_file(FILE), sanitized_db_schema="").tier("sanitized").schema == "kbl"


def test_access_is_parsed_from_json_in_the_environment(monkeypatch):
    monkeypatch.setenv("CORPUS_TIER_ACCESS", '{"sanitized": ["orchestrator", "pi"]}')
    conf = Settings()
    reg = tiers.Registry(tiers.load(None, conf), conf, ENV)

    assert reg.tier("sanitized").access == ("orchestrator", "pi")
    assert reg.tier("sensitive").access == ()


def test_a_bad_gate_override_is_refused():
    with pytest.raises(tiers.TierConfigError, match="CORPUS_INDEX_SENSITIVITY_GATE"):
        registry(index_sensitivity_gate="extreme")


# --- the file -----------------------------------------------------------------------


def test_a_file_defines_every_part_of_a_tier(tiers_file):
    reg = registry(tiers_file(FILE))
    sensitive, sanitized = reg.tiers()

    assert (sensitive.server_name, sensitive.access) == ("corpus-local", ("pi-local",))
    assert sensitive.policy.orders == {"newest"} and sensitive.policy.semantic is False
    assert (sanitized.server_name, sanitized.schema) == ("corpus-index-example", "kbl")
    assert (sanitized.policy.max_limit, sanitized.policy.search_depth) == (20, 100)
    assert sanitized.projection == tiers.Projection("sensitive", ("one_line", "domain"), "medium")


def test_dsns_resolve_lazily_and_fail_naming_their_variable():
    # Each pod holds only its own tier's DSN: a missing one fails when the store
    # is opened, not when the registry is built.
    reg = tiers.Registry(tiers.DEFAULT, settings(database_url=""), {})
    sensitive, sanitized = reg.tiers()

    assert (sensitive.dsn, sanitized.dsn, sanitized.reader_dsn) == (None, None, None)
    with pytest.raises(RuntimeError, match="CORPUS_DATABASE_URL"):
        sensitive.serving_dsn()
    with pytest.raises(RuntimeError, match="CORPUS_SANITIZED_DATABASE_URL"):
        sanitized.store_dsn()
    with pytest.raises(RuntimeError, match="CORPUS_INDEX_DATABASE_URL"):
        sanitized.serving_dsn()


def test_a_tiers_repr_holds_no_dsn():
    shown = repr(registry().tier("sanitized"))
    assert "CORPUS_INDEX_DATABASE_URL" in shown
    assert SANITIZED_DSN not in shown and READER_DSN not in shown


def test_a_tier_without_a_reader_serves_with_its_store_dsn(tiers_file):
    text = FILE.replace(", reader_dsn_env: CORPUS_INDEX_DATABASE_URL", "")
    assert registry(tiers_file(text)).tier("sanitized").serving_dsn() == SANITIZED_DSN


def test_an_unknown_tier_is_refused_naming_the_defined_ones():
    with pytest.raises(ValueError, match="no tier named 'nope'; defined: sensitive, sanitized"):
        registry().tier("nope")


@pytest.mark.parametrize(
    ("edit", "error"),
    [
        (lambda f: f.replace("serve: {server_name: corpus-local", "colour: red\n    serve: {server_name: corpus-local"),
         r"unknown field `colour`"),
        (lambda f: f.replace("max_limit: 20", "max_limit: 20\n      max_rows: 5"),
         r"unknown field `max_rows`"),
        (lambda f: f[:f.index("    query:\n      max_limit: 20")],
         r"missing required field `query`"),
        (lambda f: f.replace("      text: true\n      semantic: false\n", ""),
         r"missing required field `text`"),
        (lambda f: f.replace("from: sensitive", "from: raw"),
         r"projection.from names unknown tier 'raw'"),
        (lambda f: f.replace("from: sensitive", "from: sanitized"),
         r"projection.from must name a source tier"),
        (lambda f: f.replace("filterable: [domain]", "filterable: [domain, colour]"),
         r"unknown filter\(s\) 'colour'"),
        (lambda f: f.replace("orders: [priority, newest]", "orders: [random]"),
         r"random"),
        (lambda f: f.replace("search_depth: 100", "search_depth: 500"),
         r"search_depth"),
        (lambda f: f.replace("fields: [one_line, domain]", "fields: [one_line, subject]"),
         r"unknown column\(s\) subject"),
        (lambda f: f.replace("sensitivity_gate: medium", "sensitivity_gate: extreme"),
         r"sensitivity_gate"),
        (lambda f: f.replace("server_name: corpus-index-example", "server_name: corpus-local"),
         r"server_name 'corpus-local' is used by two tiers"),
        (lambda f: f.replace("name: sanitized", "name: sensitive"),
         r"tier 'sensitive' is defined twice"),
        (lambda f: f.replace("{dsn_env: CORPUS_DATABASE_URL}", "{dsn_env: CORPUS_OTHER_URL}"),
         r"source tier's store.dsn_env must be CORPUS_DATABASE_URL"),
        (lambda f: f.replace("{dsn_env: CORPUS_DATABASE_URL}", "{dsn_env: CORPUS_DATABASE_URL, schema: other}"),
         r"source tier's store.schema must be CORPUS_DB_SCHEMA"),
        (lambda f: f.replace("dsn_env: CORPUS_DATABASE_URL", "dsn: postgresql://u@db/x"),
         r"unknown field `dsn`"),
        (lambda f: "tiers: []\n", r"defines no tiers"),
        (lambda f: "tiers: [\n", r"not valid YAML"),
    ],
)
def test_a_bad_file_is_refused_with_a_clear_error(tiers_file, edit, error):
    with pytest.raises(tiers.TierConfigError, match=error):
        registry(tiers_file(edit(FILE)))


def test_an_unreadable_file_is_refused(tmp_path):
    with pytest.raises(tiers.TierConfigError, match="tiers file .*missing.yaml"):
        registry(str(tmp_path / "missing.yaml"))


def test_the_default_round_trips_through_the_file_schema(tiers_file):
    # The default is itself a valid file: what a deployment writes to start from.
    text = msgspec.yaml.encode(tiers.DEFAULT).decode()
    assert registry(tiers_file(text)).tiers() == registry().tiers()
