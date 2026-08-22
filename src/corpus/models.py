"""Normalized record types shared across fetchers, classification, and storage."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Record(BaseModel):
    """A single ingestable item (an email or a document) before storage."""

    source: str  # fetcher id, e.g. "imap:example"
    source_uid: str  # stable per-source id; (source, source_uid) is unique
    kind: str  # "email" | "file"

    account: str | None = None
    folder: str | None = None
    thread_id: str | None = None

    from_addr: str | None = None
    to_addrs: list[str] = Field(default_factory=list)
    subject: str | None = None
    sent_at: datetime | None = None

    # Provider labels/categories (e.g. Gmail labels). Empty for sources without
    # them (plain IMAP conveys only the folder).
    labels: list[str] = Field(default_factory=list)

    headers: dict[str, str] = Field(default_factory=dict)
    uri: str | None = None
    body_text: str = ""

    def key(self) -> str:
        return f"{self.source}::{self.source_uid}"


class Classification(BaseModel):
    label: str
    confidence: float
    signals: dict[str, object] = Field(default_factory=dict)
