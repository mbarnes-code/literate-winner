"""Path-jailed read/glob/grep primitives for read-only tool backends.

Ported from vvaharness/backends/localtools.py
  License: Apache License 2.0
  Copyright 2026 Visa, Inc.
  Source: https://github.com/visa/visa-vulnerability-agentic-harness
  Upstream commit: 3d972f679d8f5e3838b394edee0b5ea9c626b0fb
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable adaptations:
  - Dropped the LLM tool-schema surface (_SCHEMAS, _EXEC, schemas_for,
    anthropic_schemas_for, execute); this module is now the pure jail
    primitive, and the sandbox backends wrap it.
  - Dropped the vvaharness.report.redact import; redaction runs at a
    separate layer (devharness/redaction/rules.py) per spec §4.9.
  - Removed Anthropic/OpenAI-specific docstring paragraphs; kept the
    security rationale for the cwd jail and the per-line regex cap.
"""
from __future__ import annotations
import os
import re
from pathlib import Path

_MAX_BYTES = 200_000
_MAX_MATCHES = 200
_MAX_GLOB = 500
# Per-line ceiling for the Grep regex scan. The pattern is model-supplied, so a
# pathological line (e.g. a multi-KB minified blob) fed to a backtracking regex
# could pin a worker thread. Bounding the bytes the regex sees per line caps
# that work; the cap is high enough that normal source lines are unaffected.
_MAX_GREP_LINE = 50_000


def _jail(root: Path, p: str) -> Path | None:
    """Resolve *p* against *root* and reject any path that escapes the root.

    Returns the resolved absolute path on success, or None if the path is
    outside the jail (absolute paths escaping root, `..` traversal, symlinks
    pointing outside, or resolution errors).
    """
    try:
        cand = (root / p).resolve() if not os.path.isabs(p) else Path(p).resolve()
    except (OSError, ValueError):
        return None
    try:
        cand.relative_to(root)
    except ValueError:
        return None
    return cand


def _read(root: Path, path: str, offset: int = 0, limit: int = 2000) -> str:
    fp = _jail(root, path)
    if fp is None:
        return f"ERROR: path '{path}' is outside the repository root"
    if not fp.is_file():
        return f"ERROR: file not found: {path}"
    try:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read(_MAX_BYTES * 4).splitlines()
    except OSError as e:
        return f"ERROR: cannot read {path}: {e}"
    start = max(0, int(offset))
    end = start + max(1, int(limit))
    out = []
    for i, line in enumerate(lines[start:end], start + 1):
        out.append(f"{i}\t{line}")
    body = "\n".join(out)
    if len(body) > _MAX_BYTES:
        body = body[:_MAX_BYTES] + "\n... [truncated]"
    if not body:
        body = "(file is empty or offset past EOF)"
    return body


def _glob(root: Path, pattern: str) -> str:
    pat = pattern.lstrip("/").lstrip("\\")
    try:
        hits = sorted(
            str(p.relative_to(root)).replace("\\", "/")
            for p in root.glob(pat)
            if p.is_file() and _jail(root, str(p.relative_to(root))) is not None
        )
    except (OSError, ValueError) as e:
        return f"ERROR: invalid glob '{pattern}': {e}"
    if not hits:
        return "No files found"
    if len(hits) > _MAX_GLOB:
        return "\n".join(hits[:_MAX_GLOB]) + f"\n... ({len(hits) - _MAX_GLOB} more)"
    return "\n".join(hits)


def _grep(root: Path, pattern: str, path: str | None = None,
          glob: str | None = None, ignore_case: bool = False,
          context: int = 0) -> str:
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return f"ERROR: invalid regex '{pattern}': {e}"

    if path:
        target = _jail(root, path)
        if target is None:
            return f"ERROR: path '{path}' is outside the repository root"
        files = [target] if target.is_file() else sorted(
            p for p in target.rglob("*")
            if p.is_file() and _jail(root, str(p)) is not None)
    elif glob:
        files = sorted(
            p for p in root.glob(glob.lstrip("/"))
            if p.is_file() and _jail(root, str(p)) is not None)
    else:
        files = sorted(
            p for p in root.rglob("*")
            if p.is_file() and _jail(root, str(p)) is not None)

    out: list[str] = []
    n = 0
    ctx = max(0, min(200, int(context)))
    for fp in files:
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > _MAX_BYTES * 8:
            text = text[: _MAX_BYTES * 8]
        lines = text.splitlines()
        rel = str(fp.relative_to(root)).replace("\\", "/")

        def _clip(s: str) -> str:
            return s if len(s) <= _MAX_GREP_LINE else s[:_MAX_GREP_LINE] + " …[line clipped]"

        for i, line in enumerate(lines):
            if rx.search(line[:_MAX_GREP_LINE]):
                if ctx:
                    lo, hi = max(0, i - ctx), min(len(lines), i + ctx + 1)
                    for j in range(lo, hi):
                        mark = ":" if j == i else "-"
                        out.append(f"{rel}:{j + 1}{mark}{_clip(lines[j])}")
                    out.append("--")
                else:
                    out.append(f"{rel}:{i + 1}:{_clip(line)}")
                n += 1
                if n >= _MAX_MATCHES:
                    out.append(f"... (stopped at {_MAX_MATCHES} matches)")
                    return "\n".join(out)
    return "\n".join(out) if out else "No matches found"


__all__ = [
    "_jail",
    "_read",
    "_glob",
    "_grep",
    "_MAX_BYTES",
    "_MAX_MATCHES",
    "_MAX_GLOB",
    "_MAX_GREP_LINE",
]
