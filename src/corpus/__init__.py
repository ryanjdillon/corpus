"""Corpus — semantic search and structured query over content from pluggable sources."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("corpus")
except PackageNotFoundError:  # pragma: no cover - source tree, not installed
    __version__ = "0.0.0+unknown"
