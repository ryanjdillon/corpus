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
from corpus.enrichment import SensitivityLevel
from corpus.query import SANITIZED_POLICY, SEMANTIC_DEPTH, SENSITIVE_POLICY
from corpus.sanitize import DEFAULT_POLICY, KEEP_ALL, STUB_POLICY, Policy, Redaction
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
    # the tiers file existed; only the sanitized tier's redaction is new.
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
    assert sanitized.projection == tiers.Projection("sensitive", DEFAULT_POLICY)
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
    assert sanitized.projection.policy.sensitivity_gate is SensitivityLevel.medium
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
    assert sanitized.projection == tiers.Projection(
        "sensitive", Policy(fields=("one_line", "domain"), sensitivity_gate=SensitivityLevel.medium)
    )


def test_the_default_policy_keeps_context_and_hides_identifiers():
    # The sanitized tier's default, as the docs state it.
    policy = registry().tier("sanitized").projection.policy

    assert (policy.fields, policy.sensitivity_gate, policy.gate_action) == (
        COLS, SensitivityLevel.high, "redact"
    )
    assert msgspec.structs.asdict(policy.redact) == {
        "organization": "keep", "person_name": "role", "reference_id": "mask",
        "financial_id": "mask", "email_address": "drop", "phone": "drop", "place_name": "drop",
    }


REDACTING = FILE.replace(
    "projection: {from: sensitive, fields: [one_line, domain], sensitivity_gate: medium}",
    """projection:
      from: sensitive
      sensitivity_gate: medium
      gate_action: redact
      redact:
        organization: drop
        person_name: initial
        reference_id: drop
        financial_id: keep
        email_address: keep
        phone: drop""",
)


def test_a_file_sets_the_redaction_per_entity_class(tiers_file):
    policy = registry(tiers_file(REDACTING)).tier("sanitized").projection.policy

    assert policy.redact == Redaction(
        organization="drop", person_name="initial", reference_id="drop",
        financial_id="keep", email_address="keep", phone="drop",
    )
    assert (policy.sensitivity_gate, policy.gate_action) == (SensitivityLevel.medium, "redact")


def test_a_redaction_key_left_out_takes_its_default(tiers_file):
    text = FILE.replace("sensitivity_gate: medium}", "redact: {phone: keep}}")
    redact = registry(tiers_file(text)).tier("sanitized").projection.policy.redact

    assert redact == Redaction(phone="keep")


def test_the_stub_projection_is_reachable_from_the_file(tiers_file):
    # gate_action stub and keep for every class is the projection before policies.
    keep = ", ".join(f"{name}: keep" for name in Redaction.__struct_fields__)
    text = FILE.replace(
        "fields: [one_line, domain], sensitivity_gate: medium}",
        f"gate_action: stub, redact: {{{keep}}}}}",
    )
    policy = registry(tiers_file(text)).tier("sanitized").projection.policy

    assert policy == STUB_POLICY
    assert policy.redact == KEEP_ALL


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
    text = FILE.replace("reader_dsn_env: CORPUS_INDEX_DATABASE_URL", "reader_dsn_env: null")
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
        (lambda f: f[:f.index("    query:\n      max_limit: 20")].replace("name: sanitized", "name: archive"),
         r"missing required field `query` - at `\$.tiers\[1\]`"),
        (lambda f: f.replace("      text: true\n      semantic: false\n", "").replace("name: sensitive", "name: raw"),
         r"missing required field `text` - at `\$.tiers\[0\].query`"),
        (lambda f: f.replace("name: sanitized", "name: archive").replace("    store: {dsn_env: CORPUS_SANITIZED_DATABASE_URL", "    other: {dsn_env: X"),
         r"unknown field `other`"),
        (lambda f: f.replace("schema: kbl}", "schema: kbl, colour: red}"),
         r"unknown field `colour` - at `\$.tiers\[1\].store`"),
        (lambda f: "- tiers\n", r"Expected `object`, got `array`"),
        (lambda f: "tiers: {name: sensitive}\n", r"Expected `array \| null`, got `object` - at `\$.tiers`"),
        (lambda f: "tiers: [sensitive]\n", r"Expected `object`, got `str` - at `\$.tiers\[0\]`"),
        (lambda f: "tiers: [{name: 5}]\n", r"Expected `str`, got `int` - at `\$.tiers\[0\].name`"),
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
        (lambda f: f.replace("sensitivity_gate: medium", "gate_action: hide"),
         r"Invalid enum value 'hide' - at `\$.tiers\[1\].projection.gate_action`"),
        (lambda f: f.replace("sensitivity_gate: medium", "redact: {person_name: mask}"),
         r"Invalid enum value 'mask' - at `\$.tiers\[1\].projection.redact.person_name`"),
        (lambda f: f.replace("sensitivity_gate: medium", "redact: {organization: mask}"),
         r"Invalid enum value 'mask' - at `\$.tiers\[1\].projection.redact.organization`"),
        (lambda f: f.replace("sensitivity_gate: medium", "redact: {email_address: initial}"),
         r"Invalid enum value 'initial'"),
        (lambda f: f.replace("sensitivity_gate: medium", "redact: {reference_id: role}"),
         r"Invalid enum value 'role'"),
        (lambda f: f.replace("sensitivity_gate: medium", "redact: {address: drop}"),
         r"unknown field `address` - at `\$.tiers\[1\].projection.redact`"),
        (lambda f: f.replace("sensitivity_gate: medium", "redact: drop"),
         r"Expected `object`, got `str` - at `\$.tiers\[1\].projection.redact`"),
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
    with pytest.raises(tiers.ConfigError, match=error):
        registry(tiers_file(edit(FILE)))


def test_an_unreadable_file_is_refused(tmp_path):
    with pytest.raises(tiers.ConfigError, match="config file .*missing.yaml"):
        registry(str(tmp_path / "missing.yaml"))


def test_the_default_round_trips_through_the_file_schema(tiers_file):
    # The default is itself a valid file: what a deployment writes to start from.
    text = msgspec.yaml.encode(tiers.DEFAULT).decode()
    assert registry(tiers_file(text)).tiers() == registry().tiers()


# --- a declared tier named like a built-in one ---------------------------------------

MINIMAL = """\
tiers:
  - name: sensitive
  - name: sanitized
    serve: {server_name: corpus-index-kbl, access: [orchestrator, pi]}
"""

#: A complete file equal to the built-in default: every key written out,
#: defaults included, as a deployment that copied the policies has it.
COMPLETE_DEFAULT = """\
tiers:
  - name: sensitive
    store: {dsn_env: CORPUS_DATABASE_URL}
    serve: {server_name: corpus, access: []}
    query:
      max_limit: 100
      search_depth: 200
      filterable: [account, label, sent_at, source]
      aggregatable: [account, label, month]
      orders: [newest, oldest]
      text: true
      semantic: true
  - name: sanitized
    store:
      dsn_env: CORPUS_SANITIZED_DATABASE_URL
      reader_dsn_env: CORPUS_INDEX_DATABASE_URL
    serve: {server_name: corpus-index, access: []}
    projection:
      from: sensitive
      fields: [id, source, account, thread_id, sent_at, sender_domain, domain, category,
               transactional_type, importance, requires_action, action_type, action_summary,
               deadline, waiting_on, time_sensitive, suggested_disposition, sensitivity_level,
               one_line, organizations, enriched_at, uri, facts]
      sensitivity_gate: high
      gate_action: redact
      redact:
        organization: keep
        person_name: role
        reference_id: mask
        financial_id: mask
        email_address: drop
        phone: drop
        place_name: drop
    query:
      max_limit: 100
      search_depth: 200
      filterable: [account, action_type, category, deadline, domain, facts.travel,
                   facts.travel.carrier, facts.travel.destination_country, facts.travel.end,
                   facts.travel.origin_country, facts.travel.start, importance,
                   max_sensitivity, organization, requires_action, sender_domain, sent_at,
                   source, suggested_disposition, time_sensitive, transactional_type,
                   waiting_on]
      aggregatable: [category, domain, facts.travel.destination_country,
                     facts.travel.origin_country, facts.travel.start_year, importance, month,
                     organization, sender_domain]
      orders: [deadline, newest, oldest, priority]
      text: true
      semantic: true
"""


def test_a_minimal_built_in_tier_keeps_its_policies_and_digest(tiers_file):
    # Only what the deployment changes is written; the policies, store and
    # projection are the built-in ones, so the synced rows stay current.
    default = registry().tier("sanitized")
    sensitive, sanitized = registry(tiers_file(MINIMAL)).tiers()

    assert (sanitized.server_name, sanitized.access) == ("corpus-index-kbl", ("orchestrator", "pi"))
    assert sanitized.policy == SANITIZED_POLICY
    assert sanitized.projection == default.projection
    assert sanitized.projection.policy.digest() == DEFAULT_POLICY.digest()
    assert (sanitized.dsn_env, sanitized.reader_dsn_env, sanitized.schema) == (
        default.dsn_env, default.reader_dsn_env, default.schema
    )
    assert sensitive == registry().tier("sensitive")


def test_a_complete_file_equal_to_the_default_loads_as_the_default(tiers_file):
    path = tiers_file(COMPLETE_DEFAULT)

    assert tiers.load(path, settings()) == tiers.load(None, settings())
    assert registry(path).tiers() == registry().tiers()
    assert (
        registry(path).tier("sanitized").projection.policy.digest()
        == registry().tier("sanitized").projection.policy.digest()
    )


def test_a_complete_file_loads_as_written(tiers_file):
    # Nothing a complete file sets is changed by the built-in tiers.
    assert tiers.read(tiers_file(FILE)) == msgspec.yaml.decode(FILE, type=tiers.ConfigFile)


def test_a_built_in_tier_merges_nested_keys_and_replaces_lists(tiers_file):
    text = MINIMAL + "    query: {max_limit: 10, orders: [newest], aggregatable: []}\n"
    policy = registry(tiers_file(text)).tier("sanitized").policy

    assert (policy.max_limit, policy.orders, policy.aggregatable) == (10, {"newest"}, frozenset())
    assert policy.filterable == SANITIZED_POLICY.filterable
    assert (policy.text, policy.semantic, policy.search_depth) == (True, True, SEMANTIC_DEPTH)


def test_a_built_in_projection_merges_its_redaction(tiers_file):
    text = MINIMAL + "    projection: {sensitivity_gate: medium, redact: {phone: keep}}\n"
    projection = registry(tiers_file(text)).tier("sanitized").projection

    assert projection.source == "sensitive"
    assert projection.policy == Policy(
        sensitivity_gate=SensitivityLevel.medium, redact=Redaction(phone="keep")
    )


def test_an_explicit_empty_value_is_not_filled_in(tiers_file):
    text = MINIMAL.replace("access: [orchestrator, pi]", "access: []")
    assert registry(tiers_file(text)).tier("sanitized").access == ()


def test_an_explicit_null_drops_a_built_in_value(tiers_file):
    # projection: null makes the built-in sanitized tier a source tier, which
    # then fails the source tier's store rule rather than keeping the projection.
    with pytest.raises(tiers.TierConfigError, match="source tier's store.dsn_env"):
        registry(tiers_file(MINIMAL + "    projection: null\n"))


def test_a_built_in_tier_left_out_of_the_file_does_not_exist(tiers_file):
    # A declared tiers: block lists exactly the tiers there are.
    reg = registry(tiers_file("tiers:\n  - name: sensitive\n"))

    assert reg.names() == ("sensitive",)
    with pytest.raises(ValueError, match="no tier named 'sanitized'"):
        reg.tier("sanitized")


def test_a_new_tier_named_like_no_built_in_must_be_complete(tiers_file):
    text = MINIMAL + "  - name: archive\n    serve: {server_name: corpus-archive}\n"
    with pytest.raises(tiers.ConfigError, match=r"missing required field `store` - at `\$.tiers\[2\]`"):
        registry(tiers_file(text))
