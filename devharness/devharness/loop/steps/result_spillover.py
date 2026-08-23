"""Spill oversized tool results to disk and return a pointer marker.

Ported from hermes-agent/tools/tool_result_storage.py
  License: MIT License
  Copyright (c) 2025 Nous Research
  Source: https://github.com/NousResearch/hermes-agent
  Upstream commit: f293e7206b4ddd66042329442c6afebc19a8808d
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable adaptations:
  - Storage root changed from ``$HERMES_HOME/cache/spillover`` to
    ``~/.devharness/sessions/{session_id}/spillover/`` per devharness spec
    §4.11, keyed by ``session_id`` so a session's spilled outputs are
    self-contained and disposable with the session.
  - Filenames are ``{turn_id}_{tool_name}_{call_id}.txt`` so paths are
    deterministic per the spec's ``(turn_id, tool_name, call_id)`` contract.
  - Dropped the hermes remote-sandbox path translation
    (``_sandbox_visible_spillover_path``, ``_write_to_sandbox``, ssh/modal
    /daytona ``env.execute`` fallbacks); devharness runs its own
    ``SandboxRunner`` layer and doesn't route tool results back through a
    remote shell.
  - Dropped the hermes ``BudgetConfig`` module dependency; per-tool
    thresholds are passed in explicitly by ``maybe_spillover`` and the
    turn-aggregate budget helper takes a plain int.
  - Kept the three-layer pattern: per-tool cap (caller's responsibility),
    per-result spillover (``maybe_spillover``), per-turn aggregate
    (``enforce_turn_budget``).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Iterable, List, Mapping, MutableMapping

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────
PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"

DEFAULT_SPILLOVER_THRESHOLD = 100_000
DEFAULT_TURN_BUDGET = 200_000
DEFAULT_PREVIEW_SIZE = 2_000
SPILLOVER_MAX_AGE_HOURS = 24

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_FILENAME_STEM = 120

# Once-per-process prune guard so long-running sessions self-clean without
# needing an external housekeeping loop.
_prune_lock = threading.Lock()
_pruned_sessions: set[str] = set()


# ── Path helpers ─────────────────────────────────────────────


def _devharness_home() -> Path:
    """Return the devharness state root (``$DEVHARNESS_HOME`` or ``~/.devharness``)."""
    override = os.environ.get("DEVHARNESS_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".devharness"


def get_spillover_dir(session_id: str) -> Path:
    """Return the spillover directory for *session_id* (not created)."""
    return _devharness_home() / "sessions" / _sanitize_component(session_id) / "spillover"


def _sanitize_component(raw: str) -> str:
    """Turn *raw* into a filesystem-safe path component."""
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", str(raw or "")).strip("._-")
    if not cleaned:
        cleaned = "unknown"
    if len(cleaned) > _MAX_FILENAME_STEM:
        digest = hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:12]
        cleaned = f"{cleaned[:_MAX_FILENAME_STEM].rstrip('._-')}_{digest}"
    return cleaned


def _spillover_filename(turn_id: int, tool_name: str, call_id: str) -> str:
    """Deterministic filename per spec §4.11: ``{turn_id}_{tool_name}_{call_id}.txt``."""
    return (
        f"{int(turn_id)}_"
        f"{_sanitize_component(tool_name)}_"
        f"{_sanitize_component(call_id)}.txt"
    )


# ── Cleanup ──────────────────────────────────────────────────


def cleanup_spillover_cache(
    session_id: str, max_age_hours: int = SPILLOVER_MAX_AGE_HOURS
) -> int:
    """Delete spillover files older than *max_age_hours*. Returns removed count."""
    spill_dir = get_spillover_dir(session_id)
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    try:
        entries = list(spill_dir.iterdir())
    except OSError:
        return 0
    for f in entries:
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _prune_once(session_id: str) -> None:
    """Best-effort prune, at most once per session per process."""
    with _prune_lock:
        if session_id in _pruned_sessions:
            return
        _pruned_sessions.add(session_id)
    try:
        removed = cleanup_spillover_cache(session_id)
        if removed:
            logger.debug("Pruned %d expired spillover file(s) in %s", removed, session_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Spillover prune failed: %s", exc)


# ── Preview + marker ─────────────────────────────────────────


def generate_preview(content: str, max_chars: int = DEFAULT_PREVIEW_SIZE) -> tuple[str, bool]:
    """Return ``(preview, has_more)``. Truncates at the last newline in bounds."""
    if len(content) <= max_chars:
        return content, False
    truncated = content[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars // 2:
        truncated = truncated[: last_nl + 1]
    return truncated, True


def _format_size(n_chars: int) -> str:
    kb = n_chars / 1024
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb:.1f} KB"


def _build_persisted_message(
    *, preview: str, has_more: bool, original_size: int, file_path: str
) -> str:
    """Build the ``<persisted-output>`` replacement block."""
    msg = f"{PERSISTED_OUTPUT_TAG}\n"
    msg += (
        f"This tool result was too large "
        f"({original_size:,} characters, {_format_size(original_size)}).\n"
    )
    msg += f"Full output saved to: {file_path}\n"
    msg += (
        "Use a file-read tool with offset/limit to access specific sections; "
        "do NOT re-request the same data from the remote API — the full "
        "result is already on disk.\n\n"
    )
    msg += f"Preview (first {len(preview)} chars):\n"
    msg += preview
    if has_more:
        msg += "\n..."
    msg += f"\n{PERSISTED_OUTPUT_CLOSING_TAG}"
    return msg


_PERSISTED_PATH_RE = re.compile(r"^Full output saved to: (.+)$", re.MULTILINE)


def extract_persisted_path(content: str) -> str | None:
    """Return the file path recorded in a ``<persisted-output>`` block, or None."""
    if not isinstance(content, str) or PERSISTED_OUTPUT_TAG not in content:
        return None
    match = _PERSISTED_PATH_RE.search(content)
    return match.group(1).strip() if match else None


# ── Public API ───────────────────────────────────────────────


def maybe_spillover(
    result: str,
    *,
    session_id: str,
    turn_id: int,
    tool_name: str,
    call_id: str,
    threshold: int = DEFAULT_SPILLOVER_THRESHOLD,
    preview_size: int = DEFAULT_PREVIEW_SIZE,
) -> str:
    """Persist *result* to disk when it exceeds *threshold* and return a marker.

    Returns the original ``result`` unchanged when it fits under ``threshold``
    or when the persist write fails.  Otherwise returns a
    ``<persisted-output>...</persisted-output>`` block that carries the
    on-disk path plus a bounded preview — safe to inject back into the
    model's context.

    Path convention (spec §4.11):
        ``~/.devharness/sessions/{session_id}/spillover/{turn_id}_{tool_name}_{call_id}.txt``
    """
    if threshold <= 0 or len(result) <= threshold:
        return result

    spill_dir = get_spillover_dir(session_id)
    filename = _spillover_filename(turn_id, tool_name, call_id)
    path = spill_dir / filename

    try:
        spill_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(result, encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning(
            "Spillover write failed for %s (session=%s, turn=%s): %s",
            filename, session_id, turn_id, exc,
        )
        preview, has_more = generate_preview(result, max_chars=preview_size)
        suffix = "\n..." if has_more else ""
        return (
            f"{preview}{suffix}\n\n"
            f"[Truncated: tool response was {len(result):,} chars. "
            f"Full output could not be persisted.]"
        )

    _prune_once(session_id)

    preview, has_more = generate_preview(result, max_chars=preview_size)
    logger.info(
        "Persisted large tool result: tool=%s call_id=%s size=%d path=%s",
        tool_name, call_id, len(result), path,
    )
    return _build_persisted_message(
        preview=preview,
        has_more=has_more,
        original_size=len(result),
        file_path=str(path),
    )


def enforce_turn_budget(
    tool_messages: List[MutableMapping[str, object]],
    *,
    session_id: str,
    turn_id: int,
    budget: int = DEFAULT_TURN_BUDGET,
    threshold: int = DEFAULT_SPILLOVER_THRESHOLD,
    preview_size: int = DEFAULT_PREVIEW_SIZE,
) -> List[MutableMapping[str, object]]:
    """Layer 3: spill the largest non-persisted results until under *budget*.

    Each entry in ``tool_messages`` is expected to have ``content`` (str) and
    optionally ``tool_call_id`` / ``name`` fields.  The list is mutated in
    place and returned; already-persisted entries are skipped.
    """
    if budget <= 0:
        return tool_messages

    total_size = 0
    candidates: list[tuple[int, int]] = []
    for i, msg in enumerate(tool_messages):
        content = msg.get("content") or ""
        if not isinstance(content, str):
            continue
        size = len(content)
        total_size += size
        if PERSISTED_OUTPUT_TAG not in content:
            candidates.append((i, size))

    if total_size <= budget:
        return tool_messages

    candidates.sort(key=lambda x: x[1], reverse=True)

    for idx, size in candidates:
        if total_size <= budget:
            break
        msg = tool_messages[idx]
        content = msg.get("content") or ""
        if not isinstance(content, str):
            continue
        tool_name = str(msg.get("name") or "__turn_budget__")
        call_id = str(msg.get("tool_call_id") or f"budget_{idx}")
        replacement = maybe_spillover(
            content,
            session_id=session_id,
            turn_id=turn_id,
            tool_name=tool_name,
            call_id=call_id,
            # Force a spill regardless of per-tool threshold.
            threshold=1,
            preview_size=preview_size,
        )
        if replacement != content:
            total_size -= size
            total_size += len(replacement)
            msg["content"] = replacement
            logger.info(
                "Turn-budget enforcement: persisted tool result "
                "call_id=%s size=%d",
                call_id, size,
            )

    return tool_messages


def iter_persisted_paths(messages: Iterable[Mapping[str, object]]) -> Iterable[str]:
    """Yield persisted-output paths referenced by *messages*."""
    for msg in messages:
        content = msg.get("content") if isinstance(msg, Mapping) else None
        if not isinstance(content, str):
            continue
        path = extract_persisted_path(content)
        if path:
            yield path


__all__ = [
    "DEFAULT_PREVIEW_SIZE",
    "DEFAULT_SPILLOVER_THRESHOLD",
    "DEFAULT_TURN_BUDGET",
    "PERSISTED_OUTPUT_CLOSING_TAG",
    "PERSISTED_OUTPUT_TAG",
    "SPILLOVER_MAX_AGE_HOURS",
    "cleanup_spillover_cache",
    "enforce_turn_budget",
    "extract_persisted_path",
    "generate_preview",
    "get_spillover_dir",
    "iter_persisted_paths",
    "maybe_spillover",
]
