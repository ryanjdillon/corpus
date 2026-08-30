"""Deterministic credential/secret-leak scan over document content.

Two layers, both returning secret *types* and counts — never the values:

1. A small set of local, high-precision regexes for unmistakable credentials
   (private-key blocks, provider-prefixed API keys/tokens, JWTs). These have
   distinctive prefixes/structure, so they barely false-positive on prose and run
   everywhere — no external dependency, no model.
2. Optionally, Betterleaks (a maintained gitleaks fork) when ``settings.leaks_bin``
   points at the binary: its full curated ruleset is unioned in for rule types the
   local regexes don't cover. Only the rule id is read from its report, never the
   matched secret.

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
import re
import subprocess
from collections.abc import Callable

from .config import settings
from .redact import Span

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


def _run_betterleaks(
    text: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, int]:
    """Scan ``text`` with the external Betterleaks binary via stdin.

    Returns ``{rule_name: count}``. Degrades to ``{}`` (with a warning) if the
    binary is missing or its report can't be read — the local regexes remain the
    guaranteed baseline.
    """
    try:
        proc = run(
            [
                settings.leaks_bin,
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
            timeout=settings.leaks_timeout,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        log.warning("betterleaks unavailable (%s); credential scan limited to local rules", exc)
        return {}
    try:
        findings = json.loads(proc.stdout or b"[]")
    except json.JSONDecodeError:
        log.warning("betterleaks returned no parseable report; skipping external scan")
        return {}
    counts: dict[str, int] = {}
    for finding in findings or []:
        rule = str(finding.get("RuleID") or "generic").lower()
        name = _RULE_MAP.get(rule, f"leak_{rule}")  # rule id only — never the value
        counts[name] = counts.get(name, 0) + 1
    return counts


def scan(
    text: str | None,
    *,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, int]:
    """Detect leaked credentials in ``text`` — {secret_name: count}, no values."""
    if not text:
        return {}
    counts: dict[str, int] = {}
    for name, pattern in _RULES:
        n = len(pattern.findall(text))
        if n:
            counts[name] = counts.get(name, 0) + n
    if settings.leaks_bin:
        for name, n in _run_betterleaks(text, run=run).items():
            # Local regexes take precedence; the external scanner only adds rule
            # types the local layer doesn't already cover (avoids double counting).
            counts.setdefault(name, n)
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
