"""Deterministic credential/secret-leak scan over document content.

Two layers, both returning secret *types* and counts — never the values:

1. A small set of local, high-precision regexes for unmistakable credentials
   (private-key blocks, provider-prefixed API keys/tokens, JWTs). These have
   distinctive prefixes/structure, so they barely false-positive on prose and run
   everywhere — no external dependency, no model.
2. Optionally, Betterleaks (a maintained gitleaks fork) when
   :attr:`LeaksSettings.bin` (``CORPUS_LEAKS_BIN``) names the binary: its full
   curated ruleset is unioned in for rule types the local regexes don't cover.
   Only the rule id is read from its report, never the matched secret.

If Betterleaks is configured but cannot be run (missing, failed, timed out), the
scan quietly narrows to the local regexes. That must not go unnoticed, so
:func:`scan` reports which scanner actually ran through its ``on_scanner``
callback — ``"betterleaks"`` or ``"local"`` — for the caller to turn into a
metric, and the first fallback in a process is logged once as a warning naming
the reason (never content).

This complements ``pii`` (identity/financial numbers): a leaked API key is a
different, higher-severity class than an SSN, and detectable with far better
precision than any hand-rolled recovery-code heuristic — which is why recovery
codes are left to the LLM confirmation stage instead.

:func:`scan` returns counts (the archive audit). :func:`scan_spans` returns match
offsets for redaction, from the local regexes only: Betterleaks reports counts
over stdin without reliable character offsets, and the local rules already cover
the high-severity credential types the egress gate redacts and blocks on.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from .span import Span

log = logging.getLogger(__name__)

# Local rules: (secret_name, pattern). Chosen for distinctive, low-false-positive
# structure — provider prefixes and key markers, not generic entropy.
_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[porsu]_[A-Za-z0-9]{36,251}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_+-]{40,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe_secret_key", re.compile(r"\bsk_live_[0-9A-Za-z]{24,}\b")),
    # Newer OpenAI keys carry this fixed marker; the bare `sk-...` form is too
    # false-positive-prone on prose to include.
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
]

# Redaction needs the secret's whole extent, not just its marker. The count rule
# above matches a private key by its BEGIN line alone (enough to flag it); for
# redaction we mask the entire PEM block so no key material survives. The header
# rule still runs as a fallback for a truncated block missing its END line.
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----",
    re.DOTALL,
)

#: Which scanner a :func:`scan` ran: Betterleaks (its report read, unioned with the
#: local rules) or the local rules alone (unconfigured, or a fallback).
Scanner = Literal["betterleaks", "local"]

#: Why a configured Betterleaks did not run: the binary is missing, it failed
#: (error, non-zero exit, unreadable report) or it ran past its timeout.
FallbackReason = Literal["missing", "failed", "timeout"]

#: Every type name a local rule can carry. Betterleaks maps onto these where it
#: can and reports any other rule as ``leak_<ruleid>``, so counts are open-ended.
ENTITY_TYPES = frozenset(name for name, _ in _RULES)

# Betterleaks/gitleaks rule id -> our stable name (best-effort; unmapped rule ids
# are kept verbatim as ``leak_<ruleid>`` so nothing is silently dropped).
_RULE_MAP = {
    "private-key": "private_key",
    "aws-access-token": "aws_access_key",
    "github-pat": "github_token",
    "github-fine-grained-pat": "github_pat",
    "jwt": "jwt",
    "stripe-access-token": "stripe_secret_key",
    "slack-bot-token": "slack_token",
    "gcp-api-key": "google_api_key",
    "openai-api-key": "openai_key",
}


@dataclass(frozen=True)
class LeaksSettings:
    """Where the external scanner is, and how long it may take.

    Read from the environment rather than from a host application's config, so
    the detectors install alone. The names carry the ``CORPUS_`` prefix because
    the corpus service and anything deployed beside it share them.

    Attributes:
        bin: The Betterleaks binary name or path; empty runs the local regexes only.
        timeout: Seconds one Betterleaks run may take.
    """

    bin: str = ""
    timeout: float = 30.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> LeaksSettings:
        """Build the settings from ``CORPUS_LEAKS_BIN`` and ``CORPUS_LEAKS_TIMEOUT``."""
        timeout = environ.get("CORPUS_LEAKS_TIMEOUT", "")
        try:
            seconds = float(timeout) if timeout else cls.timeout
        except ValueError:
            raise ValueError(
                f"CORPUS_LEAKS_TIMEOUT must be a number of seconds, got {timeout!r}"
            ) from None
        return cls(bin=environ.get("CORPUS_LEAKS_BIN", ""), timeout=seconds)


#: The process's settings, read once at import like the rest of its configuration.
settings = LeaksSettings.from_env()


# One warning per process: the enrich audit scans every document, so a broken
# binary would otherwise log once per message. The per-call signal is the
# ``on_scanner`` callback.
_fallback_lock = threading.Lock()
_fallback_warned = threading.Event()


def _note_fallback(reason: FallbackReason) -> None:
    """Log a fallback to the local rules: a warning the first time, debug after."""
    with _fallback_lock:
        first = not _fallback_warned.is_set()
        _fallback_warned.set()
    if first:
        log.warning(
            "betterleaks is configured but unavailable (%s); credential scans fall back "
            "to the local rules (logged once per process; later fallbacks at debug)",
            reason,
        )
    else:
        log.debug("betterleaks %s; local rules only", reason)


def _run_betterleaks(
    text: str,
    config: LeaksSettings,
    *,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, int] | FallbackReason:
    """Scan ``text`` with the external Betterleaks binary via stdin.

    Returns ``{rule_name: count}``, or why it could not: the local regexes remain
    the guaranteed baseline, so the caller degrades rather than fails.
    """
    try:
        proc = run(
            [
                config.bin,
                "stdin",
                "--report-format",
                "json",
                "--report-path",
                "/dev/stdout",
                "--no-banner",
                "--exit-code",
                "0",
            ],
            input=text.encode(),
            capture_output=True,
            timeout=config.timeout,
            check=False,
        )
    except FileNotFoundError:
        return "missing"
    except subprocess.TimeoutExpired:
        return "timeout"
    except (OSError, subprocess.SubprocessError):
        return "failed"
    # ``--exit-code 0`` makes a run with findings exit 0, so any other exit is an
    # error: whatever report it left must not read as a clean scan.
    if proc.returncode != 0:
        return "failed"
    try:
        findings = json.loads(proc.stdout or b"[]")
    except json.JSONDecodeError:
        return "failed"
    if not isinstance(findings, list):
        return "failed"
    counts: dict[str, int] = {}
    for finding in findings:
        rule = str(finding.get("RuleID") or "generic").lower()
        name = _RULE_MAP.get(rule, f"leak_{rule}")  # rule id only — never the value
        counts[name] = counts.get(name, 0) + 1
    return counts


def scan(
    text: str | None,
    *,
    config: LeaksSettings | None = None,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    on_scanner: Callable[[Scanner], None] | None = None,
) -> dict[str, int]:
    """Detect leaked credentials in ``text`` — {secret_name: count}, no values.

    ``config`` defaults to the environment's :data:`settings`. ``on_scanner``, if
    given, is called once per non-empty ``text`` with the :data:`Scanner` that
    ran: ``"local"`` when Betterleaks is unconfigured or fell back.
    """
    if config is None:
        config = settings
    if not text:
        return {}
    counts: dict[str, int] = {}
    for name, pattern in _RULES:
        n = len(pattern.findall(text))
        if n:
            counts[name] = counts.get(name, 0) + n
    scanner: Scanner = "local"
    if config.bin:
        external = _run_betterleaks(text, config, run=run)
        if isinstance(external, str):
            _note_fallback(external)
        else:
            scanner = "betterleaks"
            for name, n in external.items():
                # Local regexes take precedence; the external scanner only adds rule
                # types the local layer doesn't already cover (avoids double counting).
                counts.setdefault(name, n)
    if on_scanner is not None:
        on_scanner(scanner)
    return counts


def scan_spans(text: str | None) -> list[Span]:
    """Detect leaked credentials in ``text``, returning match offsets for redaction.

    Local regexes only (see the module docstring). Private keys are matched as the
    whole PEM block where possible so redaction removes the key material, not just
    the BEGIN marker; the header rule adds a fallback span for a truncated block.
    Overlapping spans are left for the caller's overlap resolution to dedupe.
    """
    if not text:
        return []
    spans: list[Span] = [
        Span(m.start(), m.end(), "private_key", "secret")
        for m in _PRIVATE_KEY_BLOCK.finditer(text)
    ]
    for name, pattern in _RULES:
        for m in pattern.finditer(text):
            spans.append(Span(m.start(), m.end(), name, "secret"))
    return spans
