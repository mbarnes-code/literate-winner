"""Approval gate — human-in-the-loop confirmation for risky actions.

Ported from hermes-agent/tools/approval.py
  License: MIT License
  Copyright (c) 2025 Nous Research
  Source: https://github.com/NousResearch/hermes-agent
  Upstream commit: f293e7206b4ddd66042329442c6afebc19a8808d
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Selective extraction (~200 LOC) of the ContextVar-scoped session cache,
sha256-keyed decision cache, and dangerous-command regex list. The
three-mode decision tree (``suggest`` / ``auto-edit`` / ``auto``) is
implemented per devharness-spec.md §10.2 — Hermes uses different mode
names (``manual`` / ``smart`` / ``off``), so this file adapts rather
than mirrors.

Skipped: Slack / Telegram / Discord / gateway integrations, YOLO env
freeze (collapsed into ``mode == "auto"``), plugin-rule escalation.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import re
import threading
from typing import Any, Callable, Literal, Optional

Decision = Literal["approved", "denied", "cached_approved"]
Mode = Literal["suggest", "auto-edit", "auto"]

# =========================================================================
# Session-scoped state (ContextVar so concurrent turns can't cross-taint)
# =========================================================================
_approval_session_key: contextvars.ContextVar[str] = contextvars.ContextVar(
    "approval_session_key",
    default="",
)
_approval_mode: contextvars.ContextVar[Mode] = contextvars.ContextVar(
    "approval_mode",
    default="suggest",
)

_lock = threading.Lock()
# session_key -> {cache_key: Decision}
_session_cache: dict[str, dict[str, Decision]] = {}
# Optional prompt handler (interactive TTY, gateway, etc.). Signature:
#   handler(tool_name, args, sandbox_class, approval_class, reason) -> bool
_prompt_handler: Optional[Callable[[str, dict, str, str, str], bool]] = None


def set_session_key(session_key: str) -> contextvars.Token:
    """Bind the approval session for the current context/task."""
    return _approval_session_key.set(session_key or "")


def reset_session_key(token: contextvars.Token) -> None:
    _approval_session_key.reset(token)


def set_mode(mode: str) -> None:
    """Set the approval mode. Accepts ``suggest`` / ``auto-edit`` / ``auto``."""
    if mode not in ("suggest", "auto-edit", "auto"):
        raise ValueError(f"invalid approval mode: {mode!r}")
    _approval_mode.set(mode)  # type: ignore[arg-type]


def set_prompt_handler(handler: Optional[Callable[[str, dict, str, str, str], bool]]) -> None:
    """Install a handler that returns True to approve, False to deny.

    When unset the gate fails closed in ``suggest``/``auto-edit`` mode for
    any tool needing prompting.
    """
    global _prompt_handler
    _prompt_handler = handler


def clear_session_cache() -> None:
    """Drop all cached decisions for the current session."""
    key = _approval_session_key.get()
    with _lock:
        _session_cache.pop(key, None)


# =========================================================================
# Canonicalization + cache key
# =========================================================================
def _canonicalize(value: Any) -> Any:
    """Recursively sort dict keys so equivalent args hash identically."""
    if isinstance(value, dict):
        return {k: _canonicalize(value[k]) for k in sorted(value.keys())}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    return value


def _cache_key(tool_name: str, args: dict) -> str:
    payload = json.dumps(
        {"tool": tool_name, "args": _canonicalize(args)},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# =========================================================================
# Dangerous-command detection
# =========================================================================
# Subset extracted from Hermes DANGEROUS_PATTERNS (approval.py lines
# ~774-1085). Kept: recursive deletion, permission wipes, disk/filesystem
# destruction, SQL blast radius, remote-content-piped-to-shell,
# obfuscated decode-and-exec, sudo privilege flags, fork bomb.
_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+(-[^\s]*\s+)*/", "delete in root path"),
    (r"\brm\s+-[^\s]*r", "recursive delete"),
    (r"\brm\s+--recursive\b", "recursive delete (long flag)"),
    (
        r"\bchmod\s+(-[^\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b",
        "world-writable permissions",
    ),
    (r"\bchown\s+(-[^\s]*)?R\s+root", "recursive chown to root"),
    (r"\bmkfs\b", "format filesystem"),
    (r"\bdd\s+.*if=", "disk copy"),
    (r">\s*/dev/sd", "write to block device"),
    (r"\bDROP\s+(TABLE|DATABASE)\b", "SQL DROP"),
    (r"\bDELETE\s+FROM\b(?![^\n]*\bWHERE\b)", "SQL DELETE without WHERE"),
    (r"\bTRUNCATE\s+(TABLE)?\s*\w", "SQL TRUNCATE"),
    (r"\bsystemctl\s+(-[^\s]+\s+)*(stop|restart|disable|mask)\b", "stop/restart service"),
    (r"\bkill\s+-9\s+-1\b", "kill all processes"),
    (r"\bpkill\s+-9\b", "force kill processes"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\b(curl|wget)\b.*\|\s*(?:[/\w]*/)?(?:ba)?sh(?:\s|$|-c)", "pipe remote content to shell"),
    (
        r"\b(base64|base32|base16)\s+(?:-[dD]|--decode)\b.*\|\s*\b(bash|sh|zsh|ksh|dash)\b",
        "pipe decoded content to shell",
    ),
    (r"\bxxd\s+-r\b.*\|\s*\b(bash|sh|zsh|ksh|dash)\b", "pipe xxd-decoded to shell"),
    (
        r"\bsudo\b[^;|&\n]*?\s+(?:-s\b|--st[a-z]*\b|-a\b|--a[a-z]*\b)",
        "sudo with privilege flag",
    ),
    (r"\bgit\s+push\s+.*--force", "git force-push"),
    (r"\bgit\s+reset\s+--hard", "git reset --hard"),
]

_RE_FLAGS = re.IGNORECASE | re.MULTILINE
_DANGEROUS_COMPILED = [(re.compile(p, _RE_FLAGS), d) for p, d in _DANGEROUS_PATTERNS]


def detect_dangerous(command: str) -> Optional[str]:
    """Return a human-readable reason if ``command`` matches a dangerous
    pattern, else ``None``."""
    if not command:
        return None
    for regex, description in _DANGEROUS_COMPILED:
        if regex.search(command):
            return description
    return None


# =========================================================================
# Decision tree — spec §10.2
# =========================================================================
# Sandbox classes: "read"     — pure reads (fs.read, code.search, ...)
#                  "edit"     — bounded workspace mutations
#                  "exec"     — subprocess / network / privileged
# Approval classes: "safe"    — never prompts
#                   "review"  — prompts in "suggest" mode only
#                   "danger"  — always prompts except in "auto"
def _needs_prompt(mode: Mode, sandbox_class: str, approval_class: str) -> bool:
    if approval_class == "safe":
        return False
    if approval_class == "danger":
        return mode != "auto"
    # approval_class == "review"
    if mode == "suggest":
        return True
    if mode == "auto-edit":
        # auto-edit prompts only when leaving the edit boundary
        return sandbox_class == "exec"
    return False  # mode == "auto"


def request_approval(
    tool_name: str,
    args: dict,
    sandbox_class: str,
    approval_class: str,
    mode: Optional[str] = None,
) -> Decision:
    """Return an approval decision for a proposed tool invocation.

    Consults the session cache first; on miss consults the mode-based
    decision tree; delegates to the installed prompt handler when the
    tree calls for confirmation. Fails closed (``denied``) when no
    handler is available and a prompt is required.
    """
    active_mode: Mode = mode or _approval_mode.get()  # type: ignore[assignment]
    if active_mode not in ("suggest", "auto-edit", "auto"):
        raise ValueError(f"invalid approval mode: {active_mode!r}")

    session_key = _approval_session_key.get()
    key = _cache_key(tool_name, args)

    with _lock:
        bucket = _session_cache.get(session_key)
        if bucket is not None and key in bucket:
            cached = bucket[key]
            # Only re-serve approvals; a cached denial is not sticky so a
            # later mode change can revisit the decision.
            if cached == "approved":
                return "cached_approved"

    # Dangerous-command override — a shell subprocess whose command string
    # matches a hardline pattern always escalates to "danger".
    if sandbox_class == "exec":
        command = str(args.get("command") or args.get("cmd") or "")
        reason = detect_dangerous(command)
        if reason:
            approval_class = "danger"

    if not _needs_prompt(active_mode, sandbox_class, approval_class):
        _store(session_key, key, "approved")
        return "approved"

    handler = _prompt_handler
    if handler is None:
        return "denied"

    reason_text = f"{sandbox_class}/{approval_class} in mode={active_mode}"
    try:
        approved = bool(handler(tool_name, args, sandbox_class, approval_class, reason_text))
    except Exception:
        return "denied"

    decision: Decision = "approved" if approved else "denied"
    _store(session_key, key, decision)
    return decision


def _store(session_key: str, key: str, decision: Decision) -> None:
    with _lock:
        bucket = _session_cache.setdefault(session_key, {})
        bucket[key] = decision
