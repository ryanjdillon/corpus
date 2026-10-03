"""Declarative trust tiers: the single source of truth for the storage pipeline.

A tier is a storage backend plus the policy around it: which MCP surface exposes
it, which principals may reach that surface, and how data is projected into it from
the tier above. Deployments compose tiers to model raw -> sanitized ->
further-downgraded trust levels. Isolation lives in the backing store (a separate
database per tier); the access boundary lives in the tool and its allow-list.

The sync, the query surfaces, and (eventually) the generated deploy all read this
registry, so a tier is defined once here rather than in several places.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import settings


@dataclass(frozen=True)
class StorageTier:
    """One trust tier in the corpus pipeline.

    Attributes:
        name: Stable tier identifier, e.g. ``"sensitive"`` or ``"sanitized"``.
        dsn: Postgres DSN for this tier's backing store.
        tool: Name of the MCP surface that exposes this tier.
        access: Principals allowed to reach the tool. These are opaque identifiers
            from the deployment (``CORPUS_TIER_ACCESS``). How a principal is
            authenticated (an API-key client id at a gateway, an OIDC claim) is the
            deployment's and the policy decision point's concern, not corpus's.
        projection: Name of the projection that derives this tier's rows from the
            tier above, or ``None`` for a source tier populated directly by ingest.
    """

    name: str
    dsn: str
    tool: str
    access: tuple[str, ...] = field(default_factory=tuple)
    projection: str | None = None


def tiers(access: dict[str, list[str]] | None = None) -> list[StorageTier]:
    """Return the configured trust tiers, most sensitive first.

    DSNs and access lists come from configuration, so the same code serves any
    isolation posture and any identity scheme: a deployment points each tier's DSN
    at the right store and names who may reach it. *access* maps a tier name to its
    principals and defaults to ``settings.tier_access``; a tier with no entry
    grants no one.
    """
    access = settings.tier_access if access is None else access

    def granted(name: str) -> tuple[str, ...]:
        return tuple(access.get(name, ()))

    return [
        StorageTier(
            name="sensitive",
            dsn=settings.database_url,
            tool="corpus-local",
            access=granted("sensitive"),
            projection=None,
        ),
        StorageTier(
            name="sanitized",
            dsn=settings.sanitized_database_url,
            tool="corpus-index",
            access=granted("sanitized"),
            projection="sanitize",
        ),
    ]


def tier(name: str, access: dict[str, list[str]] | None = None) -> StorageTier:
    """Return the tier named *name*, or raise :class:`KeyError` if undefined."""
    for t in tiers(access):
        if t.name == name:
            return t
    raise KeyError(f"no storage tier named {name!r}")
