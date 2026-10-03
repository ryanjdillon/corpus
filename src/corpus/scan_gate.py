"""scan-gate: run the egress policy behind a gateway protocol adapter.

The policy (:mod:`corpus.egress.policy`) is gateway-neutral; an adapter speaks one
gateway's protocol. ``CORPUS_SCAN_GATE_ADAPTER`` selects the adapter; adding a
gateway means adding an adapter module and an entry in :data:`ADAPTERS`.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module

from .config import settings

# Adapter name -> "module:function" serving it. Imported lazily, so an adapter's
# protocol dependencies load only when it is selected.
ADAPTERS: dict[str, str] = {
    "envoy-ext-proc": "corpus.egress.envoy:serve",
}


def adapter(name: str | None = None) -> Callable[[], None]:
    """Return the serve function for adapter *name* (default: the configured one)."""
    name = name or settings.scan_gate_adapter
    try:
        target = ADAPTERS[name]
    except KeyError:
        known = ", ".join(sorted(ADAPTERS))
        raise ValueError(f"unknown scan-gate adapter {name!r}; known: {known}") from None
    module, _, func = target.partition(":")
    return getattr(import_module(module), func)


def serve() -> None:  # pragma: no cover - binds a port and blocks
    """Run the configured adapter until terminated."""
    adapter()()
