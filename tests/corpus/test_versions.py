"""The stored version stamps, pinned.

Each is stored beside the rows it produced, and a changed value re-audits or
re-projects every row. Moving code must leave them unchanged; changing one is a
deliberate act that updates the pin here in the same change.
"""

from __future__ import annotations

from corpus import sanitize, scan


def test_scan_version_is_pinned():
    assert scan.SCAN_VERSION == "f6c73c1d9bcd"


def test_redaction_version_is_pinned():
    assert sanitize.REDACTION_VERSION == 2
    assert sanitize.Policy().digest() == "fab6fa4570f21e69"
