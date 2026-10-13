"""Deterministic PII and credential detection, and redaction built on it.

The public interface is this module's ``__all__``; the submodules ``pii`` and
``leaks`` stay importable for a caller that needs one detector alone.

Importing Presidio's recognizers takes over a second, so the names that need
them load on first use: a caller that only handles :class:`Span` values (a
config loader, a policy type) does not pay for detection it never runs. The
submodules are named apart from the functions they hold (``redaction``, not
``redact``): importing a submodule binds its name on this package, which would
otherwise shadow the lazily loaded function of the same name.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .leaks import LeaksSettings
from .span import Span, resolve_overlaps, validate_spans

if TYPE_CHECKING:
    from .detectors import ENTITY_TYPES, detect, detect_spans, fingerprint_parts
    from .redaction import RedactResult, redact

_LAZY = {
    "ENTITY_TYPES": ".detectors",
    "detect": ".detectors",
    "detect_spans": ".detectors",
    "fingerprint_parts": ".detectors",
    "RedactResult": ".redaction",
    "redact": ".redaction",
}

__all__ = [
    "ENTITY_TYPES",
    "LeaksSettings",
    "RedactResult",
    "Span",
    "detect",
    "detect_spans",
    "fingerprint_parts",
    "redact",
    "resolve_overlaps",
    "validate_spans",
]


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
