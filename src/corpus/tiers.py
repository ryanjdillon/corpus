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
        tool: Name of the MCP surface that exposes this tier (a gateway route).
        access: Principals (gateway ``x-client-id`` values) allowed to reach the tool.
        projection: Name of the projection that derives this tier's rows from the
            tier above, or ``None`` for a source tier populated directly by ingest.
    """

    name: str
    dsn: str
    tool: str
    access: tuple[str, ...] = field(default_factory=tuple)
    projection: str | None = None


def tiers() -> list[StorageTier]:
    """Return the configured trust tiers, most sensitive first.

    DSNs come from configuration so the same code serves any isolation posture
    (same instance / separate instance / managed Postgres); a deployment need only
    point each tier's DSN at the right store.
    """
    return [
        StorageTier(
            name="sensitive",
            dsn=settings.database_url,
            tool="corpus-local",
            access=("pi-local", "corpus-summary"),
            projection=None,
        ),
        StorageTier(
            name="sanitized",
            dsn=settings.sanitized_database_url,
            tool="corpus-index",
            access=("orchestrator", "pi"),
            projection="sanitize",
        ),
    ]


def tier(name: str) -> StorageTier:
    """Return the tier named *name*, or raise :class:`KeyError` if undefined."""
    for t in tiers():
        if t.name == name:
            return t
    raise KeyError(f"no storage tier named {name!r}")
