"""Regex-based scanner for prompt-injection markers in untrusted text.

Ported from hermes-agent/tools/threat_patterns.py
  License: MIT License
  Copyright (c) 2025 Nous Research
  Source: https://github.com/NousResearch/hermes-agent
  Upstream commit: f293e7206b4ddd66042329442c6afebc19a8808d
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable adaptations:
  - Public surface reshaped for devharness spec §4.12: ``scan_for_injection``
    returns structured ``Finding`` records with ``kind``, ``span``, and
    ``excerpt`` (not the upstream pattern-id strings), and ``sanitize``
    replaces matched markers with ``[REDACTED: prompt-injection candidate]``.
  - Dropped upstream scope split (``all`` / ``context`` / ``strict``) — the
    devharness loop scans one class of input (repo-loaded context files and
    tool results) so a single unified pattern list is enough.
  - Dropped ``first_threat_message`` helper (upstream memory/skills-write
    convenience) — the loop's caller reports findings itself.
  - Pure function, no hermes state hooks or logger side effects.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple

# Hard cap on scanned text.  Bounds worst-case regex runtime on adversarial
# inputs while preserving detections near the beginning of injected content.
MAX_SCAN_CHARS = 65_536

# Bounded filler between key attack tokens.  Eight filler words is enough
# for the intended obfuscation bypasses without unbounded backtracking.
_FILLER = r"(?:\w+\s+){0,8}"

# Replacement token written by ``sanitize`` for every matched span.
REDACTION_MARKER = "[REDACTED: prompt-injection candidate]"

# Each entry: (regex, pattern_id).  Kinds are stable identifiers so callers
# can group / rate-limit detections.
_PATTERNS: List[Tuple[str, str]] = [
    # ── Classic prompt injection ─────────────────────────────────
    (rf"ignore\s+{_FILLER}(previous|all|above|prior)\s+{_FILLER}instructions", "prompt_injection"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (rf"disregard\s+{_FILLER}(your|all|any)\s+{_FILLER}(instructions|rules|guidelines)", "disregard_rules"),
    (rf"act\s+as\s+(if|though)\s+{_FILLER}you\s+{_FILLER}(have\s+no|don't\s+have)\s+{_FILLER}(restrictions|limits|rules)", "bypass_restrictions"),
    (r"<!--[^>]{0,512}(?:ignore|override|system|secret|hidden)[^>]{0,512}-->", "html_comment_injection"),
    (r"<\s*div\s+style\s*=\s*[\"'][^>]{0,2048}display\s*:\s*none", "hidden_div"),
    (r"translate\s+[^\n]{0,512}\s+into\s+[^\n]{0,512}\s+and\s+(execute|run|eval)", "translate_execute"),
    (rf"do\s+not\s+{_FILLER}tell\s+{_FILLER}the\s+user", "deception_hide"),
    # Bracketed / delimited system markers.  Anchored on the literal
    # tokens the injection payloads use to spoof role framing.
    (r"\[\s*SYSTEM\s*\]", "bracket_system_marker"),
    (r"<\|\s*system\s*\|>", "chatml_system_marker"),
    (r"<\|\s*im_start\s*\|>\s*system", "chatml_im_start_system"),

    # ── Role-play / identity hijack ──────────────────────────────
    (rf"you\s+are\s+{_FILLER}now\s+(?:a|an|the)\s+", "role_hijack"),
    (rf"pretend\s+{_FILLER}(you\s+are|to\s+be)\s+", "role_pretend"),
    (rf"output\s+{_FILLER}(system|initial)\s+prompt", "leak_system_prompt"),
    (rf"(respond|answer|reply)\s+without\s+{_FILLER}(restrictions|limitations|filters|safety)", "remove_filters"),
    (rf"you\s+have\s+been\s+{_FILLER}(updated|upgraded|patched)\s+to", "fake_update"),
    (r"\bname\s+yourself\s+\w+", "identity_override"),

    # ── C2 / promptware ──────────────────────────────────────────
    (r"register\s+(as\s+)?a?\s*node", "c2_node_registration"),
    (r"(heartbeat|beacon|check[\s\-]?in)\s+(to|with)\s+", "c2_heartbeat"),
    (r"pull\s+(down\s+)?(?:new\s+)?task(?:ing|s)?\b", "c2_task_pull"),
    (r"connect\s+to\s+the\s+network\b", "c2_network_connect"),
    (r"you\s+must\s+(?:\w+\s+){0,3}(register|connect|report|beacon)\b", "forced_action"),
    (r"only\s+use\s+one[\s\-]?liners?\b", "anti_forensic_oneliner"),
    (rf"never\s+{_FILLER}(?:create|write)\s+{_FILLER}(?:script|file)\s+{_FILLER}disk", "anti_forensic_disk"),
    (r"unset\s+\w*(?:CLAUDE|CODEX|HERMES|AGENT|OPENAI|ANTHROPIC|DEVHARNESS)\w*", "env_var_unset_agent"),
    (r"\b(?:cobalt\s*strike|sliver|havoc|mythic|metasploit|brainworm)\b", "known_c2_framework"),
    (r"\bc2\s+(?:server|channel|infrastructure|beacon)\b", "c2_explicit"),
    (r"\bcommand\s+and\s+control\b", "c2_explicit_long"),

    # ── Exfiltration via shell tooling ───────────────────────────
    (r"curl\s+[^\n]{0,2048}\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl"),
    (r"wget\s+[^\n]{0,2048}\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget"),
    (r"cat\s+[^\n]{0,2048}(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)", "read_secrets"),
    (r"(send|post|upload|transmit)\s+[^\n]{0,2048}\s+(to|at)\s+https?://", "send_to_url"),
    (rf"(include|output|print|share)\s+{_FILLER}(conversation|chat\s+history|previous\s+messages|full\s+context|entire\s+context)", "context_exfil"),

    # ── Persistence / SSH backdoor ───────────────────────────────
    (r"authorized_keys", "ssh_backdoor"),
    (r"\$HOME/\.ssh|~/\.ssh", "ssh_access"),
    (r"(update|modify|edit|write|change|append|add\s+to)\s+[^\n]{0,2048}(?:AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules)", "agent_config_mod"),

    # ── Hardcoded secrets ────────────────────────────────────────
    (r"(?:api[_-]?key|token|secret|password)\s*[=:]\s*[\"'][A-Za-z0-9+/=_-]{20,}", "hardcoded_secret"),
]

# Invisible / bidirectional unicode used in injection attacks.
INVISIBLE_CHARS = frozenset({
    "\u200b", "\u200c", "\u200d", "\u2060",
    "\u2062", "\u2063", "\u2064", "\ufeff",
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069",
})

_COMPILED: List[Tuple[re.Pattern, str]] = [
    (re.compile(pat, re.IGNORECASE), kind) for pat, kind in _PATTERNS
]

_EXCERPT_RADIUS = 40


@dataclass(frozen=True)
class Finding:
    """A single prompt-injection candidate detected in scanned text."""

    kind: str
    span: Tuple[int, int]
    excerpt: str


def _excerpt(text: str, start: int, end: int) -> str:
    lo = max(0, start - _EXCERPT_RADIUS)
    hi = min(len(text), end + _EXCERPT_RADIUS)
    snippet = text[lo:hi].replace("\n", " ").replace("\r", " ")
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def scan_for_injection(text: str) -> List[Finding]:
    """Return every prompt-injection candidate detected in *text*.

    Findings carry ``kind`` (stable pattern id), ``span`` (offsets into the
    NFKC-normalised scanned prefix — see ``MAX_SCAN_CHARS``), and a short
    ``excerpt`` for logging. Multiple regex matches yield multiple findings;
    the caller is responsible for deduplication / rate-limiting.
    """
    if not text:
        return []

    findings: List[Finding] = []

    # Bound scan cost and normalise Unicode before regex evaluation.
    scanned = text[:MAX_SCAN_CHARS]

    # Invisible-unicode pass runs on the pre-normalisation text so
    # codepoints NFKC strips are still reported.
    for match in re.finditer(r"[\u200b-\u200d\u2060\u2062-\u2064\ufeff\u202a-\u202e\u2066-\u2069]", scanned):
        ch = match.group(0)
        findings.append(Finding(
            kind=f"invisible_unicode_U+{ord(ch):04X}",
            span=(match.start(), match.end()),
            excerpt=_excerpt(scanned, match.start(), match.end()),
        ))

    normalised = unicodedata.normalize("NFKC", scanned)
    for pattern, kind in _COMPILED:
        for match in pattern.finditer(normalised):
            findings.append(Finding(
                kind=kind,
                span=(match.start(), match.end()),
                excerpt=_excerpt(normalised, match.start(), match.end()),
            ))

    return findings


def sanitize(text: str) -> Tuple[str, List[Finding]]:
    """Return *(cleaned_text, findings)* with detected markers replaced.

    Each matched span is rewritten to ``REDACTION_MARKER``. Overlapping
    matches are merged so redactions don't nest. The returned ``findings``
    list uses the ORIGINAL offsets so callers can correlate with the input.
    """
    findings = scan_for_injection(text)
    if not findings:
        return text, []

    # NFKC-normalise once so replacement offsets line up with the offsets
    # ``scan_for_injection`` computed against the normalised prefix.
    scanned = text[:MAX_SCAN_CHARS]
    normalised = unicodedata.normalize("NFKC", scanned)
    tail = text[MAX_SCAN_CHARS:]

    spans = sorted({f.span for f in findings})
    merged: List[Tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))

    out: List[str] = []
    cursor = 0
    for start, end in merged:
        out.append(normalised[cursor:start])
        out.append(REDACTION_MARKER)
        cursor = end
    out.append(normalised[cursor:])
    return "".join(out) + tail, findings


__all__ = [
    "Finding",
    "INVISIBLE_CHARS",
    "MAX_SCAN_CHARS",
    "REDACTION_MARKER",
    "sanitize",
    "scan_for_injection",
]
