"""Walk cwd→git-root and load repo hint files (AGENTS.md, etc.) — spec §4.2.

No upstream code copied — design from spec §4.2.
The step is a pure disk walker; it does not import from any reference
project. When no hint files are found the step returns ``NotApplicable``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from devharness.loop.effects import Applied, NotApplicable, StepResult
from devharness.loop.state import ThreadState, TurnContext

HINT_FILES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", ".cursorrules", ".goosehints")
_MAX_HINT_BYTES = 64_000


def _find_git_root(start: Path) -> Path | None:
    for parent in (start, *start.parents):
        if (parent / ".git").exists():
            return parent
    return None


def _iter_dirs(start: Path, stop: Path | None) -> Iterable[Path]:
    yield start
    for parent in start.parents:
        yield parent
        if stop is not None and parent == stop:
            return


def _collect_hints(cwd: Path) -> str:
    git_root = _find_git_root(cwd)
    seen: set[Path] = set()
    chunks: list[str] = []
    remaining = _MAX_HINT_BYTES
    for d in _iter_dirs(cwd, git_root):
        for name in HINT_FILES:
            p = d / name
            if p in seen or not p.is_file():
                continue
            seen.add(p)
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not text.strip():
                continue
            header = f"# --- {p.name} @ {p.parent} ---\n"
            chunk = header + text
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            chunks.append(chunk)
            remaining -= len(chunk)
            if remaining <= 0:
                break
        if remaining <= 0:
            break
    return "\n\n".join(chunks)


class LoadRepoHints:
    """Load ``AGENTS.md`` / ``CLAUDE.md`` / ``.cursorrules`` / ``.goosehints``."""

    name = "LoadRepoHints"

    def apply(self, state: ThreadState, ctx: TurnContext) -> StepResult:
        if ctx.repo_hints:
            return NotApplicable
        cwd = Path(ctx.cwd).resolve() if ctx.cwd else Path.cwd()
        hints = _collect_hints(cwd)
        if not hints:
            return NotApplicable
        ctx.repo_hints = hints
        return Applied(())


__all__ = ["HINT_FILES", "LoadRepoHints"]
