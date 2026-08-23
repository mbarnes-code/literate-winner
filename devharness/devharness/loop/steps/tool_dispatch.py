"""Tool dispatch — parallelization gating and path-overlap conflict detection.

Ported from hermes-agent/agent/tool_dispatch_helpers.py
  License: MIT License
  Copyright (c) 2025 Nous Research
  Source: https://github.com/NousResearch/hermes-agent
  Upstream commit: f293e7206b4ddd66042329442c6afebc19a8808d
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable adaptations:
  - Kept only the pure planning logic: _is_destructive_command, _paths_overlap,
    _extract_parallel_scope_paths, _plan_tool_batch_segments,
    _should_parallelize_tool_batch. Dropped multimodal envelopes, threat-scan
    wrapping, trajectory normalization, MCP parallel-safe probe, and result
    verifier plumbing (~700 LOC).
  - _PARALLEL_SAFE_TOOLS and _PATH_SCOPED_TOOLS now derive from the devharness
    ToolSpec.parallel_safe / category flags at plan time rather than hard-coded
    frozensets. Sane defaults are still provided for offline use.
  - Added plan_batches(tool_calls, tool_specs) public entry point that
    enforces MAX_PARALLEL=8 (spec §4.5) by chunking parallel segments.
  - Tool-call arg parsing accepts either an object with .function.name /
    .function.arguments (OpenAI-style) or a plain dict {"name", "arguments"}.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# Spec §4.5: hard cap on concurrent tool executions per batch.
MAX_PARALLEL: int = 8


# ---- default parallel-safety hints (fallbacks when no ToolSpec available) ----

_DEFAULT_NEVER_PARALLEL: frozenset = frozenset({"clarify", "ask_user", "wait_user"})

_DEFAULT_PARALLEL_SAFE: frozenset = frozenset({
    "read_file",
    "search_files",
    "web_search",
    "web_extract",
    "session_search",
})

_DEFAULT_PATH_SCOPED_READERS: frozenset = frozenset({"read_file", "search_files"})
_DEFAULT_PATH_SCOPED_WRITERS: frozenset = frozenset({
    "write_file", "patch", "apply_patch",
})


# Terminal commands that may modify or destroy files. Batches containing a
# destructive shell call are always sequential.
_DESTRUCTIVE_PATTERNS = re.compile(
    r"""(?:^|\s|&&|\|\||;|`)(?:
        rm\s|rmdir\s|
        cp\s|install\s|
        mv\s|
        sed\s+-i|
        truncate\s|
        dd\s|
        shred\s|
        git\s+(?:reset|clean|checkout|push)\s
    )""",
    re.VERBOSE,
)
_REDIRECT_OVERWRITE = re.compile(r'[^>]>[^>]|^>[^>]')


def _is_destructive_command(cmd: str) -> bool:
    """Heuristic: does this terminal command look like it modifies/deletes files?"""
    if not cmd:
        return False
    if _DESTRUCTIVE_PATTERNS.search(cmd):
        return True
    if _REDIRECT_OVERWRITE.search(cmd):
        return True
    return False


# ---------------------------------------------------------------------------
# ToolCall normalization
# ---------------------------------------------------------------------------

def _normalize_call(tool_call: Any) -> Tuple[str, Any]:
    """Return (name, raw_arguments) from either OpenAI-style or plain-dict calls."""
    # OpenAI-style object: tool_call.function.name / .arguments
    fn = getattr(tool_call, "function", None)
    if fn is not None:
        return getattr(fn, "name", "") or "", getattr(fn, "arguments", None)
    # Mapping form.
    if isinstance(tool_call, Mapping):
        name = tool_call.get("name") or ""
        args = tool_call.get("arguments")
        if args is None:
            args = tool_call.get("args")
        return str(name), args
    return "", None


def _parse_args(raw: Any) -> Optional[Dict]:
    """Return dict args, or None when unparseable / not a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


# ---------------------------------------------------------------------------
# Path scope extraction
# ---------------------------------------------------------------------------

def _canonical_path(raw_path: str, execution_cwd: Optional[Path] = None) -> Path:
    """Return a canonical, symlink-resolved path for overlap checks."""
    expanded = Path(raw_path).expanduser()
    base = execution_cwd if execution_cwd is not None else Path.cwd()
    candidate = expanded if expanded.is_absolute() else base / expanded
    resolved = os.path.normcase(os.path.realpath(os.path.abspath(str(candidate))))
    return Path(resolved)


_V4A_FILE_HEADER = re.compile(
    r'^\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*(.+)$',
    re.MULTILINE,
)
_V4A_MOVE_HEADER = re.compile(
    r'^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)$',
    re.MULTILINE,
)


def _extract_v4a_scope_paths(patch_body: str) -> List[str]:
    """Return the file paths referenced by a V4A patch body."""
    if not isinstance(patch_body, str) or not patch_body:
        return []
    paths: List[str] = []
    for m in _V4A_FILE_HEADER.finditer(patch_body):
        p = m.group(1).strip()
        if p:
            paths.append(p)
    for m in _V4A_MOVE_HEADER.finditer(patch_body):
        src = m.group(1).strip()
        dst = m.group(2).strip()
        if src:
            paths.append(src)
        if dst:
            paths.append(dst)
    return paths


def _extract_parallel_scope_paths(
    tool_name: str,
    function_args: Dict,
    *,
    path_scoped_tools: frozenset,
    writers: frozenset,
    execution_cwd: Optional[Path] = None,
) -> List[Path]:
    """Return every canonical path this call reserves for overlap checks.

    Empty result → planner treats the call as a sequential barrier.
    """
    if tool_name not in path_scoped_tools:
        return []

    raw_paths: List[str] = []
    # V4A patch: scope from patch-body headers, not a decoy path= arg.
    if tool_name in {"patch", "apply_patch"} and (function_args.get("mode") or "").lower() == "patch":
        raw_paths.extend(_extract_v4a_scope_paths(function_args.get("patch") or ""))
    elif tool_name == "apply_patch" and "patch" in function_args:
        raw_paths.extend(_extract_v4a_scope_paths(function_args.get("patch") or ""))
    else:
        raw = function_args.get("path")
        if isinstance(raw, str) and raw.strip():
            raw_paths.append(raw)
        elif tool_name == "search_files":
            raw_paths.append(".")

    scoped: List[Path] = []
    seen: set = set()
    for raw in raw_paths:
        if not isinstance(raw, str) or not raw.strip():
            continue
        canonical = _canonical_path(raw, execution_cwd)
        key = str(canonical)
        if key in seen:
            continue
        seen.add(key)
        scoped.append(canonical)
    return scoped


def _paths_overlap(left: Path, right: Path) -> bool:
    """True when two canonical paths refer to the same subtree."""
    lp = left.parts
    rp = right.parts
    if not lp or not rp:
        return bool(lp) == bool(rp) and bool(lp)
    common = min(len(lp), len(rp))
    return lp[:common] == rp[:common]


# ---------------------------------------------------------------------------
# Batch segmentation
# ---------------------------------------------------------------------------

def _resolve_tool_flags(
    tool_specs: Optional[Mapping[str, Any]],
) -> Tuple[frozenset, frozenset, frozenset, frozenset]:
    """Derive the four gating sets from ToolSpec metadata (with defaults).

    Returns (never_parallel, parallel_safe, path_readers, path_writers).
    """
    never_parallel = set(_DEFAULT_NEVER_PARALLEL)
    parallel_safe = set(_DEFAULT_PARALLEL_SAFE)
    readers = set(_DEFAULT_PATH_SCOPED_READERS)
    writers = set(_DEFAULT_PATH_SCOPED_WRITERS)

    if tool_specs:
        for name, spec in tool_specs.items():
            parallel_flag = getattr(spec, "parallel_safe", None)
            category = getattr(spec, "category", "") or ""
            sandbox = getattr(spec, "sandbox_class", "") or ""

            if parallel_flag is False and sandbox in {"MF", "SF"}:
                # Path-scoped writer (must-fence or soft-fence file mutator).
                writers.add(name)
            elif parallel_flag is True and category in {"fs", "search"}:
                readers.add(name)

            if parallel_flag is True:
                parallel_safe.add(name)
            elif parallel_flag is False and name not in writers:
                # Non-file-scoped serial tool: force sequential.
                never_parallel.add(name)

    return frozenset(never_parallel), frozenset(parallel_safe), frozenset(readers), frozenset(writers)


def _plan_tool_batch_segments(
    tool_calls: Sequence[Any],
    *,
    tool_specs: Optional[Mapping[str, Any]] = None,
    execution_cwd: Optional[Path] = None,
) -> List[Tuple[str, List[Any]]]:
    """Split a tool-call batch into ordered ("parallel"|"sequential", calls) segments.

    Segments preserve the model's original call order exactly. Any path-scoped
    writer conflicting with an already-reserved path closes the current
    parallel run so the conflict lands sequentially. Reader↔reader overlaps
    stay parallel.
    """
    never_parallel, safe, readers, writers = _resolve_tool_flags(tool_specs)
    path_scoped = readers | writers

    segments: List[List[Any]] = []  # [kind, calls]
    current: List[Any] = []
    reserved: List[Tuple[Path, bool]] = []  # (path, is_writer)

    def _close_parallel() -> None:
        nonlocal current, reserved
        if current:
            segments.append(["parallel", current])
            current = []
            reserved = []

    def _add_sequential(tc: Any) -> None:
        _close_parallel()
        if segments and segments[-1][0] == "sequential":
            segments[-1][1].append(tc)
        else:
            segments.append(["sequential", [tc]])

    for tc in tool_calls:
        name, raw_args = _normalize_call(tc)

        if name in never_parallel or not name:
            _add_sequential(tc)
            continue

        args = _parse_args(raw_args)
        if args is None:
            logger.debug(
                "Non-dict args for %s (%s) — treating as sequential barrier",
                name, type(raw_args).__name__,
            )
            _add_sequential(tc)
            continue

        # Destructive shell command anywhere in the batch → sequential.
        if name in {"shell", "bash", "terminal"}:
            cmd = args.get("command") or args.get("cmd") or ""
            if _is_destructive_command(str(cmd)):
                _add_sequential(tc)
                continue

        if name in path_scoped:
            scoped_paths = _extract_parallel_scope_paths(
                name, args,
                path_scoped_tools=path_scoped,
                writers=writers,
                execution_cwd=execution_cwd,
            )
            if not scoped_paths:
                _add_sequential(tc)
                continue
            is_writer = name in writers
            conflict = any(
                (is_writer or existing_is_writer) and _paths_overlap(sp, ep)
                for sp in scoped_paths
                for ep, existing_is_writer in reserved
            )
            if conflict:
                _close_parallel()
            reserved.extend((p, is_writer) for p in scoped_paths)
            current.append(tc)
            continue

        if name in safe:
            current.append(tc)
            continue

        _add_sequential(tc)

    _close_parallel()

    # Demote short parallel runs; merge adjacent sequential segments.
    normalized: List[List[Any]] = []
    for kind, calls in segments:
        if kind == "parallel" and len(calls) < 2:
            kind = "sequential"
        if normalized and normalized[-1][0] == "sequential" and kind == "sequential":
            normalized[-1][1].extend(calls)
        else:
            normalized.append([kind, calls])
    return [(kind, calls) for kind, calls in normalized]


def _should_parallelize_tool_batch(
    tool_calls: Sequence[Any],
    *,
    tool_specs: Optional[Mapping[str, Any]] = None,
) -> bool:
    """True iff the WHOLE batch fits into a single parallel segment."""
    if len(tool_calls) <= 1:
        return False
    segs = _plan_tool_batch_segments(tool_calls, tool_specs=tool_specs)
    return len(segs) == 1 and segs[0][0] == "parallel"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def plan_batches(
    tool_calls: Sequence[Any],
    tool_specs: Optional[Mapping[str, Any]] = None,
    *,
    execution_cwd: Optional[Path] = None,
    max_parallel: int = MAX_PARALLEL,
) -> List[List[Any]]:
    """Return an ordered list of batches; each batch runs in parallel.

    * A sequential segment is emitted as a series of single-call batches
      (each of size 1) so the caller can await them one at a time.
    * A parallel segment larger than *max_parallel* is chunked into
      max_parallel-sized batches to enforce the spec §4.5 cap.
    """
    if not tool_calls:
        return []
    segments = _plan_tool_batch_segments(
        tool_calls, tool_specs=tool_specs, execution_cwd=execution_cwd
    )
    batches: List[List[Any]] = []
    for kind, calls in segments:
        if kind == "sequential":
            for c in calls:
                batches.append([c])
            continue
        # parallel: chunk to max_parallel
        for i in range(0, len(calls), max_parallel):
            batches.append(list(calls[i:i + max_parallel]))
    return batches


__all__ = [
    "MAX_PARALLEL",
    "plan_batches",
    "_is_destructive_command",
    "_paths_overlap",
    "_extract_parallel_scope_paths",
    "_plan_tool_batch_segments",
    "_should_parallelize_tool_batch",
]
