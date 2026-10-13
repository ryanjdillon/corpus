"""Declarative trust tiers: each tier is defined once, in the config file.

A tier is a storage backend plus the policy around it: the MCP server that
exposes it, the principals that may reach that server, the query policy the
server enforces, and, for a downgraded tier, the projection that derives its rows
from a source tier. Isolation lives in the backing store (a separate database per
tier); the access boundary lives in the gateway in front of each server.

The config file (``CORPUS_CONFIG_FILE``, YAML; ``CORPUS_TIERS_FILE`` is its
deprecated name) holds the layer's configuration: ``tiers:`` here and
``sources:`` (see :mod:`corpus.sources`). This module reads it, since it held the
tiers first. It is validated at startup into typed structs: an unknown key, a
missing query policy or a projection from an unknown tier is an error, never
ignored. Without a file, or a file without ``tiers:``, the built-in
:data:`DEFAULT` applies, which is the configuration corpus had before the file
existed. The file never holds a DSN: each store names the environment variable
that does, so secrets keep their injection path.

``serve`` and ``sync`` need nothing but :func:`tier`. A few older environment
variables still override the file for one release (see :data:`OVERRIDES`).
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import msgspec

from .config import Settings, settings
from .enrichment import SensitivityLevel
from .query import SANITIZED_POLICY, SENSITIVE_POLICY, QueryPolicy
from .sanitized_store import COLS
from .sources import Registry as SourceRegistry
from .sources import SourceEntry

log = logging.getLogger("corpus.tiers")

_Name = Annotated[str, msgspec.Meta(pattern=r"^[a-z][a-z0-9_-]*$", max_length=63)]
_EnvVar = Annotated[str, msgspec.Meta(pattern=r"^[A-Z_][A-Z0-9_]*$")]
_Ident = Annotated[str, msgspec.Meta(pattern=r"^[a-z_][a-z0-9_]*$", max_length=63)]
_Principal = Annotated[str, msgspec.Meta(min_length=1)]

#: The store ingest writes. A source tier must name it until ingest and enrich
#: read their store from the tier too; otherwise serve and ingest could silently
#: use different databases.
SOURCE_DSN_ENV = "CORPUS_DATABASE_URL"


class _Strict(
    msgspec.Struct, forbid_unknown_fields=True, frozen=True, kw_only=True, omit_defaults=True
):
    pass


class StoreConfig(_Strict):
    """Where a tier's rows live. DSNs are named by environment variable, never written.

    Attributes:
        dsn_env: The variable holding the DSN the tier is written with.
        reader_dsn_env: The variable holding a read-only DSN the server reads
            with; unset means the server reads with ``dsn_env``.
        schema: The table's schema. Unset means ``CORPUS_DB_SCHEMA`` for a source
            tier, and the source tier's schema for a projected one.
    """

    dsn_env: _EnvVar
    reader_dsn_env: _EnvVar | None = None
    schema: _Ident | None = None


class ProjectionConfig(_Strict):
    """How a downgraded tier is derived from a source tier (``corpus sync``).

    Attributes:
        source: The source tier (key ``from``).
        fields: The projected columns; any other column is stored as NULL.
        sensitivity_gate: The sensitivity at/above which free text is withheld.
    """

    source: _Name = msgspec.field(name="from")
    fields: tuple[str, ...] = COLS
    sensitivity_gate: SensitivityLevel = SensitivityLevel.high


class ServeConfig(_Strict):
    """How a tier is served (``corpus serve <tier>``).

    Attributes:
        server_name: The MCP server name, also reported in every result envelope.
        access: Principals the gateway lets reach the server. Declarative: corpus
            does not enforce it in-process.
    """

    server_name: _Name
    access: tuple[_Principal, ...] = ()


class TierConfig(_Strict):
    """One tier as written in the config file. ``query`` is required: no default policy."""

    name: _Name
    store: StoreConfig
    serve: ServeConfig
    query: QueryPolicy
    projection: ProjectionConfig | None = None


class ConfigFile(_Strict):
    """The config file.

    Attributes:
        tiers: The tiers, most sensitive first; unset means :data:`DEFAULT`'s.
        sources: The sources (see :mod:`corpus.sources`); unset means the
            deprecated pre-file convention, and an empty list means none.
    """

    tiers: list[TierConfig] | None = None
    sources: list[SourceEntry] | None = None


#: The tiers without a file: exactly the configuration before the file existed.
DEFAULT = ConfigFile(tiers=[
    TierConfig(
        name="sensitive",
        store=StoreConfig(dsn_env=SOURCE_DSN_ENV),
        serve=ServeConfig(server_name="corpus"),
        query=SENSITIVE_POLICY,
    ),
    TierConfig(
        name="sanitized",
        store=StoreConfig(
            dsn_env="CORPUS_SANITIZED_DATABASE_URL", reader_dsn_env="CORPUS_INDEX_DATABASE_URL"
        ),
        serve=ServeConfig(server_name="corpus-index"),
        query=SANITIZED_POLICY,
        projection=ProjectionConfig(source="sensitive"),
    ),
])

#: Deprecated environment overrides, applied over the file (or the default) when
#: set: setting -> what it overrides. They go after one release.
OVERRIDES = {
    "index_server_name": "serve.server_name of the 'sanitized' tier",
    "sanitized_db_schema": "store.schema of the 'sanitized' tier",
    "index_sensitivity_gate": "projection.sensitivity_gate of the 'sanitized' tier",
    "tier_access": "serve.access of each tier it names",
}


class ConfigError(ValueError):
    """The config file cannot be read or does not match its schema."""


class TierConfigError(ConfigError):
    """The tiers (in the file, or an override) do not fit together."""


@dataclass(frozen=True)
class Projection:
    """A projected tier's derivation: its source tier, columns and gate."""

    source: str
    fields: tuple[str, ...]
    sensitivity_gate: str


@dataclass(frozen=True)
class Tier:
    """One tier, resolved: everything ``serve`` and ``sync`` need.

    Attributes:
        name: The tier's name, reported in every result envelope.
        dsn: The DSN the tier is written with, or ``None`` if its variable is unset.
        dsn_env: The variable ``dsn`` came from.
        schema: The schema of the tier's table.
        reader_dsn: The read-only DSN the server reads with, if one is configured.
        reader_dsn_env: The variable ``reader_dsn`` came from.
        server_name: The MCP server name.
        access: Principals the gateway admits; declarative, not enforced here.
        policy: The query policy the server enforces.
        projection: How the tier is derived, or ``None`` for a source tier.
    """

    name: str
    # A DSN may carry a password, so it stays out of the repr (and any traceback).
    dsn: str | None = field(repr=False)
    dsn_env: str
    schema: str
    reader_dsn: str | None = field(repr=False)
    reader_dsn_env: str | None
    server_name: str
    access: tuple[str, ...]
    policy: QueryPolicy
    projection: Projection | None

    def store_dsn(self) -> str:
        """The DSN to write the tier with; raises ``RuntimeError`` if unset."""
        if not self.dsn:
            raise RuntimeError(f"{self.dsn_env} not set (the {self.name!r} tier's store)")
        return self.dsn

    def serving_dsn(self) -> str:
        """The DSN the server reads with: the reader DSN if configured, else the store's."""
        if self.reader_dsn_env is None:
            return self.store_dsn()
        if not self.reader_dsn:
            raise RuntimeError(
                f"{self.reader_dsn_env} not set (the {self.name!r} tier's read-only reader)"
            )
        return self.reader_dsn


@functools.cache
def _deprecated(message: str) -> None:
    log.warning(message)


def config_path(conf: Settings = settings) -> str | None:
    """The config file's path: ``CORPUS_CONFIG_FILE``, else ``CORPUS_TIERS_FILE``.

    Raises:
        ConfigError: Both are set, to different files.
    """
    new, old = conf.config_file, conf.tiers_file
    if new and old and new != old:
        raise ConfigError(
            f"CORPUS_CONFIG_FILE ({new}) and the deprecated CORPUS_TIERS_FILE ({old}) name "
            "different files; set only CORPUS_CONFIG_FILE"
        )
    if old and not new:
        _deprecated("CORPUS_TIERS_FILE is deprecated; set CORPUS_CONFIG_FILE to the same path")
    return new or old or None


def read(path: str) -> ConfigFile:
    """Decode the config file at *path* against its schema (each part's own checks aside).

    Raises:
        ConfigError: The file cannot be read, is not YAML, or does not match.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ConfigError(f"config file {path}: {exc.strerror or exc}") from None
    try:
        return msgspec.yaml.decode(raw, type=ConfigFile)
    except msgspec.ValidationError as exc:
        raise ConfigError(f"config file {path}: {exc}") from None
    except msgspec.DecodeError as exc:
        raise ConfigError(f"config file {path}: not valid YAML: {exc}") from None


def _override(cfg: ConfigFile, conf: Settings) -> ConfigFile:
    """*cfg* with each deprecated override that is set in *conf* applied."""
    set_ = conf.model_fields_set
    out = []
    for t in cfg.tiers or ():
        if t.name == "sanitized":
            if "index_server_name" in set_:
                t = msgspec.structs.replace(
                    t, serve=msgspec.structs.replace(t.serve, server_name=conf.index_server_name)
                )
            if "sanitized_db_schema" in set_ and conf.sanitized_db_schema:
                t = msgspec.structs.replace(
                    t, store=msgspec.structs.replace(t.store, schema=conf.sanitized_db_schema)
                )
            if "index_sensitivity_gate" in set_ and t.projection is not None:
                try:
                    gate = msgspec.convert(conf.index_sensitivity_gate, SensitivityLevel)
                except msgspec.ValidationError:
                    raise TierConfigError(
                        f"CORPUS_INDEX_SENSITIVITY_GATE: {conf.index_sensitivity_gate!r} is not "
                        f"a sensitivity level ({', '.join(s.value for s in SensitivityLevel)})"
                    ) from None
                t = msgspec.structs.replace(
                    t, projection=msgspec.structs.replace(t.projection, sensitivity_gate=gate)
                )
        if "tier_access" in set_ and t.name in conf.tier_access:
            access = tuple(conf.tier_access[t.name])
            t = msgspec.structs.replace(t, serve=msgspec.structs.replace(t.serve, access=access))
        out.append(t)
    used = sorted(f"CORPUS_{name.upper()}" for name in OVERRIDES if name in set_)
    if used:
        log.warning(
            "deprecated tier overrides set: %s; define them in the config file (CORPUS_CONFIG_FILE)",
            ", ".join(used),
        )
    return msgspec.structs.replace(cfg, tiers=out)


def _validate(cfg: ConfigFile, conf: Settings) -> None:
    """Refuse a configuration whose tiers do not fit together."""
    if not cfg.tiers:
        raise TierConfigError("config file defines no tiers")
    by_name: dict[str, TierConfig] = {}
    servers: set[str] = set()
    for t in cfg.tiers:
        if t.name in by_name:
            raise TierConfigError(f"tier {t.name!r} is defined twice")
        if t.serve.server_name in servers:
            raise TierConfigError(f"server_name {t.serve.server_name!r} is used by two tiers")
        by_name[t.name] = t
        servers.add(t.serve.server_name)
    for t in cfg.tiers:
        if t.projection is None:
            if t.store.dsn_env != SOURCE_DSN_ENV:
                raise TierConfigError(
                    f"tier {t.name!r}: a source tier's store.dsn_env must be {SOURCE_DSN_ENV}, "
                    "the store ingest writes"
                )
            if t.store.schema not in (None, conf.db_schema):
                raise TierConfigError(
                    f"tier {t.name!r}: a source tier's store.schema must be CORPUS_DB_SCHEMA "
                    f"({conf.db_schema!r}), the schema ingest writes; omit it"
                )
            continue
        source = t.projection.source
        if source not in by_name:
            raise TierConfigError(
                f"tier {t.name!r}: projection.from names unknown tier {source!r} "
                f"(defined: {', '.join(by_name)})"
            )
        if by_name[source].projection is not None:
            raise TierConfigError(
                f"tier {t.name!r}: projection.from must name a source tier (one without a "
                f"projection), not {source!r}"
            )
        unknown = sorted(set(t.projection.fields) - set(COLS))
        if unknown:
            raise TierConfigError(
                f"tier {t.name!r}: projection.fields has unknown column(s) {', '.join(unknown)}"
            )


def load(path: str | None, conf: Settings = settings) -> ConfigFile:
    """Load and validate the file at *path* (``None``: :data:`DEFAULT`), overrides applied.

    The whole file is validated, its sources included, so a command that reads
    only the tiers (``serve``, ``sync``) still refuses a file whose sources do
    not fit together. The result's ``tiers`` is always set: a file without
    ``tiers:`` gets the default's.

    Raises:
        ConfigError: The file cannot be read or does not match the schema.
        TierConfigError: Its tiers do not fit together.
        SourceConfigError: Its sources do not fit together.
    """
    cfg = read(path) if path else DEFAULT
    if cfg.tiers is None:
        cfg = msgspec.structs.replace(cfg, tiers=DEFAULT.tiers)
    cfg = _override(cfg, conf)
    _validate(cfg, conf)
    if cfg.sources is not None:
        SourceRegistry(cfg.sources)  # raises on what the schema cannot check
    return cfg


class Registry:
    """Validated tiers, resolved against the environment on each lookup.

    Resolving late keeps a DSN out of any cache and lets a DSN variable change
    between lookups (as tests do), while the file is parsed only once.
    """

    def __init__(
        self,
        cfg: ConfigFile,
        conf: Settings = settings,
        environ: Mapping[str, str] = os.environ,
    ) -> None:
        self._cfg = {t.name: t for t in cfg.tiers or ()}
        self._conf = conf
        self._environ = environ

    def _env(self, name: str | None) -> str | None:
        if name is None:
            return None
        # A variable that is also a setting (CORPUS_DATABASE_URL) resolves through
        # the settings, so it keeps the setting's default.
        field = name.removeprefix("CORPUS_").lower()
        if name.startswith("CORPUS_") and field in type(self._conf).model_fields:
            value = getattr(self._conf, field)
        else:
            value = self._environ.get(name)
        return value or None

    def _schema(self, t: TierConfig) -> str:
        if t.store.schema:
            return t.store.schema
        if t.projection is None:
            return self._conf.db_schema
        return self._schema(self._cfg[t.projection.source])

    def names(self) -> tuple[str, ...]:
        """The tier names, most sensitive first."""
        return tuple(self._cfg)

    def tier(self, name: str) -> Tier:
        """The tier named *name*; raises ``ValueError`` if it is not defined."""
        try:
            t = self._cfg[name]
        except KeyError:
            raise ValueError(
                f"no tier named {name!r}; defined: {', '.join(self._cfg)}"
            ) from None
        p = t.projection
        return Tier(
            name=t.name,
            dsn=self._env(t.store.dsn_env),
            dsn_env=t.store.dsn_env,
            schema=self._schema(t),
            reader_dsn=self._env(t.store.reader_dsn_env),
            reader_dsn_env=t.store.reader_dsn_env,
            server_name=t.serve.server_name,
            access=tuple(t.serve.access),
            policy=t.query,
            projection=None if p is None else Projection(
                source=p.source, fields=tuple(p.fields), sensitivity_gate=p.sensitivity_gate.value
            ),
        )

    def tiers(self) -> tuple[Tier, ...]:
        """Every tier, most sensitive first."""
        return tuple(self.tier(name) for name in self._cfg)


@functools.cache
def registry() -> Registry:
    """The process's tiers: the config file's, or :data:`DEFAULT`, loaded once."""
    return Registry(load(config_path()))


def tier(name: str) -> Tier:
    """The configured tier named *name*; raises ``ValueError`` if it is not defined."""
    return registry().tier(name)


def tiers() -> Sequence[Tier]:
    """Every configured tier, most sensitive first."""
    return registry().tiers()


def projected(name: str) -> tuple[Tier, Tier]:
    """The projected tier *name* and its source tier: what ``sync`` reads and writes.

    Raises:
        ValueError: *name* is not defined, or is a source tier (nothing to sync into).
        RuntimeError: Either tier's store DSN variable is unset.
    """
    dst = tier(name)
    if dst.projection is None:
        raise ValueError(f"tier {name!r} is a source tier, with no projection to sync into")
    src = tier(dst.projection.source)
    for t in (dst, src):
        t.store_dsn()  # raises if its DSN variable is unset
    return dst, src
