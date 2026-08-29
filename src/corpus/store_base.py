"""Base class for a Postgres-backed corpus storage tier."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Self

import psycopg


class Store(ABC):
    """A Postgres-backed storage tier.

    The base owns the connection, idempotent schema creation, and lifecycle.
    Subclasses declare their objects in :meth:`schema_ddl` and own their reads and
    writes, using :meth:`_write` and :meth:`_read`. Instances are meant to be used
    as context managers; the caller owns the lifecycle.
    """

    def __init__(self, dsn: str) -> None:
        """Connect to *dsn* and create this tier's schema if it is absent."""
        self._conn = psycopg.connect(dsn)
        self._create_schema()

    @abstractmethod
    def schema_ddl(self) -> str:
        """Return idempotent DDL that creates this tier's objects."""

    def _create_schema(self) -> None:
        """Run :meth:`schema_ddl` once, at construction."""
        with self._conn.cursor() as cur:
            cur.execute(self.schema_ddl())
        self._conn.commit()

    def _write(self, sql: str, params: object = None) -> None:
        """Execute a write statement and commit on the tier's single connection."""
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
        self._conn.commit()

    def _read(self, sql: str, params: object = None) -> list[tuple]:
        """Execute a query and return all rows."""
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def close(self) -> None:
        """Close the connection."""
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
