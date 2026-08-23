"""Regex-based secret / PII / PAN redaction with pluggable custom patterns.

Ported from vvaharness/report/redact.py
  License: Apache License 2.0
  Copyright 2026 Visa, Inc.
  Source: https://github.com/visa/visa-vulnerability-agentic-harness
  Upstream commit: 3d972f679d8f5e3838b394edee0b5ea9c626b0fb
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable adaptations:
  - Dropped ``redact_tree`` (JSON-structure walker) — the devharness
    redactor operates at the tool-result string boundary, not on report
    trees.
  - Added ``add_custom_pattern(name, regex)`` and
    ``clear_custom_patterns()`` (spec §4.9). Custom rules run AFTER the
    built-in patterns so a permissive user regex cannot swallow bytes a
    validator-gated built-in would otherwise mask precisely.
  - ``redact()`` now delegates to ``_redact_impl`` without touching the
    non-thread-safe ``last_counts`` side channel; callers that want
    counts use ``redact_counts()`` — which is the tuple return the spec
    prescribes: ``(str, dict)``.
"""
from __future__ import annotations
import re
from typing import Callable


# ─────────────────────────────────────────────────────────────────────────────
# Validators
# ─────────────────────────────────────────────────────────────────────────────


def _luhn(digits: str) -> bool:
    total, odd = 0, True
    for ch in reversed(digits):
        n = int(ch)
        if not odd:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        odd = not odd
    return total % 10 == 0


def _cc_network(digits: str) -> bool:
    """IIN/BIN gate so random Luhn-passing 16-digit ids aren't masked."""
    n = len(digits)
    if n < 12 or n > 19:
        return False
    p1, p2 = digits[0], int(digits[:2])
    p3 = int(digits[:3]) if n >= 3 else -1
    p4 = int(digits[:4]) if n >= 4 else -1
    if n == 15 and p2 in (34, 37):                       # Amex
        return True
    if p1 == "4" and 13 <= n <= 19:                      # Visa
        return True
    if n == 16 and (51 <= p2 <= 55 or 2221 <= p4 <= 2720):  # Mastercard
        return True
    if 16 <= n <= 19 and (p4 == 6011 or p2 == 65 or 644 <= p3 <= 649):  # Discover
        return True
    if 16 <= n <= 19 and 3528 <= p4 <= 3589:             # JCB
        return True
    if 16 <= n <= 19 and p2 == 62:                       # UnionPay
        return True
    if 14 <= n <= 19 and p2 == 36:                       # Diners
        return True
    if 12 <= n <= 19 and p4 in (5018, 5020, 5038, 5893,
                                6304, 6759, 6761, 6762, 6763):  # Maestro
        return True
    if n == 16 and (p3 == 508 or p2 in (81, 82)):        # RuPay
        return True
    return False


def _ssn_valid(d: str) -> bool:
    a, g, s = int(d[:3]), int(d[3:5]), int(d[5:9])
    # Reject only structurally-impossible groupings (area 000/666, group 00,
    # serial 0000). Area 900-999 is intentionally ALLOWED: it is the ITIN range.
    return not (a == 0 or a == 666 or g == 0 or s == 0)


def _bearer_credential(m: re.Match) -> bool:
    """True only when the post-scheme value looks like a real token.

    A genuine bearer/basic credential is base64 or otherwise carries at least
    one non-alphabetic character; requiring one rejects prose matches like
    ``HTTP Basic Authentication``.
    """
    return any(not c.isalpha() for c in m.group("bv"))


_SECRET_CODE_SHAPE = re.compile(
    r"^\("                 # leading paren / cast:   (sasl_secret_t
    r"|^[A-Za-z_]\w*\("    # function call:          parse(
    r"|^[A-Za-z_]\w*\.\w"  # member access:          obj.field / this.field
)


# ─────────────────────────────────────────────────────────────────────────────
# Patterns  (label, compiled-regex, optional validator(match)->bool)
# ─────────────────────────────────────────────────────────────────────────────

_b64u = r"[A-Za-z0-9_-]"

_PATTERNS: list[tuple[str, re.Pattern, Callable[[re.Match], bool] | None]] = [
    # ── Card / PAN ───────────────────────────────────────────────────────
    ("PAN",
     re.compile(r"(?<![0-9A-Za-z./_-])"
                r"(?:\d[\s\-]?){12,18}\d"
                r"(?![0-9A-Za-z./_-])"),
     lambda m: (lambda d: _cc_network(d) and _luhn(d))(re.sub(r"\D", "", m.group(0)))),
    ("CVV",
     re.compile(r"(?i)\b(cvv2?|cvc2?|cid|csc)\b\s*[:=]?\s*\"?(\d{3,4})\"?"),
     None),
    ("TRACK",
     re.compile(r"%B\d{12,19}\^[^?]{2,90}\?"),
     None),

    # ── PII ──────────────────────────────────────────────────────────────
    ("SSN",
     re.compile(r"(?<!\d)(\d{3})[-.\t \u00a0\u2009\u202f\u2007]"
                r"(\d{2})[-.\t \u00a0\u2009\u202f\u2007](\d{4})(?!\d)"),
     lambda m: _ssn_valid(m.group(1) + m.group(2) + m.group(3))),
    ("SSN-CTX",
     re.compile(
         r"(?i)\b(ssn|social[\s_-]*sec(?:urity)?(?:[\s_-]*(?:no|num|number))?"
         r"|itin|tin|taxpayer[\s_-]*id)\b['\"]?\s*[:=#-]?\s*['\"]?"
         r"(?<!\d)(\d{9})(?!\d)"),
     lambda m: _ssn_valid(m.group(2))),

    # ── Cloud / SaaS credentials ────────────────────────────────────────
    ("AWS-KEY",
     re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[0-9A-Z]{16}\b"),
     None),
    ("GITHUB-TOKEN",
     re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}"
                r"|github_pat_[A-Za-z0-9_]{22}_[A-Za-z0-9]{59})\b"),
     None),
    ("SLACK-TOKEN",
     re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,72}\b"),
     None),
    ("STRIPE-KEY",
     re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{24,99}\b"),
     None),
    ("GOOGLE-API-KEY",
     re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
     None),
    ("AZURE-SAS",
     re.compile(r"(?i)\bsig=[0-9A-Za-z%+/=]{20,}\b"),
     None),
    ("TWILIO-KEY",
     re.compile(r"\bSK[0-9a-fA-F]{32}\b"),
     None),

    # ── Bearer / Basic / JWT ────────────────────────────────────────────
    ("JWT",
     re.compile(r"\beyJ" + _b64u + r"{10,}\." + _b64u + r"{10,}\." + _b64u + r"{10,}\b"),
     None),
    ("BEARER",
     re.compile(r"(?i)\b(?:Bearer|Basic)\s+(?P<bv>[A-Za-z0-9+/=._-]{8,})\b"),
     _bearer_credential),

    # ── URL userinfo credential (scheme://user:secret@host) ─────────────
    ("URL-CRED",
     re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s/@]{1,256})@"),
     None),

    # ── Private-key material ────────────────────────────────────────────
    ("PRIVATE-KEY",
     re.compile(r"-{5}BEGIN [A-Z ]*PRIVATE KEY-{5}[\s\S]*?-{5}END [A-Z ]*PRIVATE KEY-{5}"),
     None),

    # ── Keyword-gated generic secret assignment ─────────────────────────
    ("SECRET",
     re.compile(
         r"(?i)(?:\b|(?<=[a-z]))(pass(?:word|wd)?|pwd|secret|api[_-]?key|access[_-]?key"
         r"|client[_-]?secret|auth[_-]?token|token|credential)s?\b"
         r"['\"`]?\s*[:=]\s*"
         r"(?P<q>['\"`]?)(?P<v>[^\s'\"`,;\x00]{6,256})(?P=q)"),
     lambda m: not re.fullmatch(
         r"(?i)\$\{?[A-Z0-9_.]+}?|%[A-Z0-9_]+%|<[^>]+>|\*{3,}|x{3,}"
         r"|\[?redacted]?|\[redacted-[a-z0-9-]+]"
         r"|null|none|true|false|changeme|your[_-]?\w+|placeholder|example",
         m.group("v"))),
]


# ─────────────────────────────────────────────────────────────────────────────
# Custom pattern registry (spec §4.9 config.toml [redaction.custom].patterns)
# ─────────────────────────────────────────────────────────────────────────────

_CUSTOM: list[tuple[str, re.Pattern, Callable[[re.Match], bool] | None]] = []


def add_custom_pattern(name: str, regex: str) -> None:
    """Register an operator-supplied regex under ``name``.

    Matches are masked as ``[REDACTED-{name}]``. Custom rules run AFTER the
    built-in patterns so a permissive user regex cannot swallow bytes that a
    validator-gated built-in would otherwise mask precisely. Duplicate names
    replace the prior entry.
    """
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"custom redaction name must be alnum/_/-: {name!r}")
    label = name.upper()
    compiled = re.compile(regex)
    global _CUSTOM
    _CUSTOM = [(lbl, rx, v) for (lbl, rx, v) in _CUSTOM if lbl != label]
    _CUSTOM.append((label, compiled, None))


def clear_custom_patterns() -> None:
    """Drop every custom pattern registered via ``add_custom_pattern``."""
    _CUSTOM.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def _redact_impl(text: str) -> tuple[str, dict[str, int]]:
    """Core masking pass. Returns (masked_text, counts). Pure / no globals."""
    # NUL is reserved as the in-band placeholder sentinel below.
    if "\x00" in text:
        text = text.replace("\x00", "")
    counts: dict[str, int] = {}
    placeholders: list[str] = []
    sentinel = "\x00{}\x00"

    def _mask(label: str, m: re.Match) -> str:
        counts[label] = counts.get(label, 0) + 1
        placeholders.append(f"[REDACTED-{label}]")
        return sentinel.format(len(placeholders) - 1)

    out = text
    for label, rx, validator in _PATTERNS + _CUSTOM:
        def _sub(m: re.Match, _label=label, _ok=validator) -> str:
            if _ok is not None and not _ok(m):
                return m.group(0)
            if _label == "SECRET":
                v = m.group("v")
                quoted = bool(m.group("q"))
                v_core = v if quoted else (v.rstrip(").}]!?>") or v)
                if len(v_core) < 6:
                    v_core = v
                keyword = re.sub(r"[^a-z]", "", m.group(1).lower())
                strong = {"password", "passwd", "pwd", "apikey",
                          "accesskey", "clientsecret", "authtoken"}
                generic_unquoted = keyword not in strong and not quoted
                plain_word = (generic_unquoted
                              and v_core.isalpha() and v_core.islower()
                              and len(v_core) < 20)
                code_shape = generic_unquoted and bool(_SECRET_CODE_SHAPE.match(v_core))
                if plain_word or code_shape:
                    return m.group(0)
                head = m.group(0)[: m.start("v") - m.start(0)]
                tail = v[len(v_core):] + m.group(0)[m.end("v") - m.start(0):]
                return head + _mask(_label, m) + tail
            if _label == "CVV":
                return m.group(0)[: m.start(2) - m.start(0)] + _mask(_label, m)
            if _label == "SSN-CTX":
                # Emit canonical "SSN" label so placeholder + counts match the
                # separated-SSN rule; keep the keyword/prefix.
                return m.group(0)[: m.start(2) - m.start(0)] + _mask("SSN", m)
            if _label == "URL-CRED":
                return m.group(1) + _mask(_label, m) + "@"
            return _mask(_label, m)
        out = rx.sub(_sub, out)

    if placeholders:
        def _reinsert(m: re.Match) -> str:
            idx = int(m.group(1))
            return placeholders[idx] if 0 <= idx < len(placeholders) else m.group(0)
        out = re.sub(r"\x00(\d+)\x00", _reinsert, out)
    return out, counts


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Return ``(masked_text, counts)``. The devharness redactor signature is
    always a tuple — no shared ``last_counts`` side channel (spec §4.9)."""
    if not text:
        return text, {}
    return _redact_impl(text)


def redact_counts(text: str) -> tuple[str, dict[str, int]]:
    """Alias for :func:`redact` kept for callers that want the explicit name."""
    return redact(text)


__all__ = [
    "redact",
    "redact_counts",
    "add_custom_pattern",
    "clear_custom_patterns",
    "_luhn",
    "_cc_network",
    "_ssn_valid",
    "_bearer_credential",
]
