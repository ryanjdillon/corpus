"""Guards on the shape of the msgspec types the package declares."""

from __future__ import annotations

import importlib
import pkgutil

import msgspec
import msgspec.inspect as mi
import pytest

import corpus
import corpus.query

_CONTAINERS = (mi.ListType, mi.SetType, mi.FrozenSetType, mi.VarTupleType, mi.DictType)


def _modules() -> list[str]:
    """Every module in the package, imported so its structs are declared."""
    names = [m.name for m in pkgutil.walk_packages(corpus.__path__, "corpus.")]
    for name in names:
        importlib.import_module(name)
    return names


def _structs() -> list[type[msgspec.Struct]]:
    names = set(_modules())
    found, stack = [], list(msgspec.Struct.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if cls.__module__ in names:
            found.append(cls)
    return sorted(found, key=lambda c: c.__qualname__)


def _unions(t: mi.Type, seen: set[type]) -> list[mi.UnionType]:
    """Every union in *t*, descending into containers and nested structs."""
    if isinstance(t, mi.UnionType):
        return [t, *(u for member in t.types for u in _unions(member, seen))]
    if isinstance(t, mi.StructType):
        if t.cls in seen:
            return []
        seen.add(t.cls)
        return [u for f in t.fields for u in _unions(f.type, seen)]
    if isinstance(t, mi.DictType):
        return [*_unions(t.key_type, seen), *_unions(t.value_type, seen)]
    if isinstance(t, mi.CollectionType):
        return _unions(t.item_type, seen)
    return []


def test_the_package_declares_structs():
    assert corpus.query.Filters in _structs()


@pytest.mark.parametrize("struct", _structs(), ids=lambda c: c.__qualname__)
def test_no_union_puts_a_pattern_beside_a_container(struct):
    # msgspec <= 0.22.0 miscounts a str `pattern` beside a container in one union
    # when it frees the type, freeing the compiled regex as if it were the
    # container's node. The heap corruption surfaces later, typically as a
    # segfault in garbage collection at interpreter exit.
    bad = [
        u for u in _unions(mi.type_info(struct), set())
        if any(isinstance(m, mi.StrType) and m.pattern for m in u.types)
        and any(isinstance(m, _CONTAINERS) for m in u.types)
    ]
    assert bad == []
