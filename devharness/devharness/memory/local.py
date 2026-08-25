"""Local memory provider backed by ~/.devharness/memory.md.

No upstream code copied — the markdown-per-``## H2`` layout is a
devharness convention chosen so users can hand-edit the file and see
git-friendly diffs. Locking prefers ``filelock`` when installed and
falls back to stdlib ``fcntl.flock`` (POSIX only) so a single-user
laptop workflow needs zero extra deps.
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# ── Optional filelock ────────────────────────────────────────────────────
try:  # pragma: no cover - trivial import guard
    from filelock import FileLock as _FileLock  # type: ignore[import-not-found]
    _HAVE_FILELOCK = True
except ImportError:
    _FileLock = None  # type: ignore[assignment]
    _HAVE_FILELOCK = False

try:  # pragma: no cover - POSIX only
    import fcntl as _fcntl
except ImportError:
    _fcntl = None  # type: ignore[assignment]


_H2 = re.compile(r"^##\s+(?P<key>.+?)\s*$", re.MULTILINE)


def _default_path() -> Path:
    override = os.environ.get("DEVHARNESS_MEMORY_PATH")
    if override:
        return Path(override)
    return Path.home() / ".devharness" / "memory.md"


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Cross-platform exclusive lock. filelock > fcntl > best-effort."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    if _HAVE_FILELOCK and _FileLock is not None:
        with _FileLock(str(lock_path)):
            yield
        return
    if _fcntl is None:  # non-POSIX and no filelock — best effort, warn quietly
        yield
        return
    with open(lock_path, "w") as fh:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)


def _parse(text: str) -> dict[str, str]:
    """Split markdown into ``## Key`` sections. First section without a header
    is dropped (treated as preamble)."""
    out: dict[str, str] = {}
    matches = list(_H2.finditer(text))
    for i, m in enumerate(matches):
        key = m.group("key").strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[key] = text[start:end].strip("\n")
    return out


def _serialize(entries: dict[str, str]) -> str:
    parts = []
    for key in sorted(entries):
        body = entries[key].rstrip()
        parts.append(f"## {key}\n{body}\n")
    return "\n".join(parts)


class LocalMemory:
    """Markdown-per-H2 memory backend (implements ``MemoryProvider``)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else _default_path()

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return _parse(self.path.read_text(encoding="utf-8"))

    def read(self, key: str) -> str | None:
        with _locked(self.path):
            return self._load().get(key)

    def write(self, key: str, value: str) -> None:
        with _locked(self.path):
            entries = self._load()
            entries[key] = value
            self.path.write_text(_serialize(entries), encoding="utf-8")

    def list_keys(self) -> list[str]:
        with _locked(self.path):
            return sorted(self._load().keys())

    def snapshot(self) -> dict[str, Any]:
        with _locked(self.path):
            return dict(self._load())
