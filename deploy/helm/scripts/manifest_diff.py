"""Compare two rendered Kubernetes manifest streams object by object.

Usage:
    python deploy/helm/scripts/manifest_diff.py A.yaml B.yaml [options]

Typically A is the current render (e.g. `kustomize build`) and B the chart's
(`helm template`). Objects are matched by kind and name and normalised before
comparison:

- tool bookkeeping is removed: metadata.namespace (the release namespace
  applies), the labels and annotations Helm and kustomize add, and the
  annotations under the prefixes in --drop-annotation-prefix;
- Secret payloads (data, stringData, sops) are dropped; only names and the
  references to them are compared;
- container env lists are sorted by name (order carries no meaning unless a
  value references another variable with $(VAR));
- a ConfigMap value holding a multi-line YAML document is parsed, so the
  comparison is of its content, not its formatting or comments.

--add-labels-a k=v adds metadata labels to every A object, to model what an
applier adds on top of the stream (a Flux Kustomization's commonMetadata).

Exit status: 0 if every object matched, 1 otherwise.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import sys
from collections.abc import Iterable
from typing import Any

import yaml

DROP_LABELS = (
    "helm.sh/chart",
    "app.kubernetes.io/managed-by",
    "app.kubernetes.io/instance",
    "app.kubernetes.io/version",
    "app.kubernetes.io/component",
)
DROP_ANNOTATION_PREFIXES = (
    "meta.helm.sh/",
    "config.kubernetes.io/",
    "internal.config.kubernetes.io/",
    "kustomize.config.k8s.io/",
    "kubectl.kubernetes.io/last-applied-configuration",
)


def load(path: str) -> dict[tuple[str, str], dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        docs = [d for d in yaml.safe_load_all(fh) if d]
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for doc in docs:
        out[(doc["kind"], doc["metadata"]["name"])] = doc
    return out


def _strip_meta(
    meta: dict[str, Any], drop_labels: Iterable[str], drop_prefixes: tuple[str, ...]
) -> None:
    meta.pop("namespace", None)
    labels = meta.get("labels") or {}
    for key in list(labels):
        if key in drop_labels:
            del labels[key]
    if not labels:
        meta.pop("labels", None)
    annotations = meta.get("annotations") or {}
    for key in list(annotations):
        if key.startswith(drop_prefixes):
            del annotations[key]
    if not annotations:
        meta.pop("annotations", None)


def _walk(node: Any, drop_labels: Iterable[str], drop_prefixes: tuple[str, ...]) -> None:
    """Normalise nested pod templates and container env lists in place."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "env" and isinstance(value, list):
                value.sort(key=lambda e: e.get("name", ""))
            if (
                key == "template"
                and isinstance(value, dict)
                and isinstance(value.get("metadata"), dict)
            ):
                _strip_meta(value["metadata"], drop_labels, drop_prefixes)
                if not value["metadata"]:
                    del value["metadata"]
            _walk(value, drop_labels, drop_prefixes)
    elif isinstance(node, list):
        for item in node:
            _walk(item, drop_labels, drop_prefixes)


def normalise(
    obj: dict[str, Any],
    drop_labels: Iterable[str],
    drop_prefixes: tuple[str, ...],
    add_labels: dict[str, str],
) -> dict[str, Any]:
    obj = copy.deepcopy(obj)
    meta = obj.setdefault("metadata", {})
    if add_labels:
        meta.setdefault("labels", {}).update(add_labels)
    _strip_meta(meta, drop_labels, drop_prefixes)
    if obj.get("kind") == "Secret":
        for key in ("data", "stringData", "sops"):
            obj.pop(key, None)
    if obj.get("kind") == "ConfigMap":
        data = obj.get("data") or {}
        for key, value in data.items():
            if isinstance(value, str) and "\n" in value:
                try:
                    data[key] = yaml.safe_load(value)
                except yaml.YAMLError:
                    pass
    _walk(obj.get("spec"), drop_labels, drop_prefixes)
    return obj


def dump(obj: dict[str, Any]) -> list[str]:
    return yaml.safe_dump(obj, sort_keys=True, default_flow_style=False).splitlines(keepends=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("a", help="first manifest stream (e.g. kustomize build output)")
    parser.add_argument("b", help="second manifest stream (e.g. helm template output)")
    parser.add_argument("--add-labels-a", action="append", default=[], metavar="K=V")
    parser.add_argument("--drop-label", action="append", default=[], metavar="KEY")
    parser.add_argument("--drop-annotation-prefix", action="append", default=[], metavar="PREFIX")
    parser.add_argument("--ignore-kind", action="append", default=[], metavar="KIND")
    parser.add_argument("--quiet", action="store_true", help="summary only, no per-object diffs")
    args = parser.parse_args(argv)

    add_labels = dict(item.split("=", 1) for item in args.add_labels_a)
    drop_labels = (*DROP_LABELS, *args.drop_label)
    drop_prefixes = (*DROP_ANNOTATION_PREFIXES, *args.drop_annotation_prefix)
    a, b = load(args.a), load(args.b)
    ignored = set(args.ignore_kind)

    rows: list[tuple[str, str, str]] = []
    for key in sorted(set(a) | set(b)):
        kind, name = key
        if kind in ignored:
            continue
        if key not in b:
            rows.append((kind, name, "only-in-a"))
            continue
        if key not in a:
            rows.append((kind, name, "only-in-b"))
            continue
        na = normalise(a[key], drop_labels, drop_prefixes, add_labels)
        nb = normalise(b[key], drop_labels, drop_prefixes, {})
        if na == nb:
            rows.append((kind, name, "identical"))
            continue
        rows.append((kind, name, "differs"))
        if not args.quiet:
            sys.stdout.writelines(
                difflib.unified_diff(dump(na), dump(nb), f"a/{kind}/{name}", f"b/{kind}/{name}")
            )

    print()
    for kind, name, status in rows:
        print(f"{status:10} {kind}/{name}")
    counts: dict[str, int] = {}
    for _, _, status in rows:
        counts[status] = counts.get(status, 0) + 1
    print("summary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0 if set(counts) <= {"identical"} else 1


if __name__ == "__main__":
    sys.exit(main())
