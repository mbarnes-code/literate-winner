"""JSONL local logging for turns, tool calls, and events.

No upstream code copied — devharness ships a small in-house JSONL
writer (spec §4.14 explicitly says DIY on top of the plain filesystem
so we do not couple to a heavyweight logging framework). One JSON
object per line, thread-safe via a per-instance lock, size-based
rotation at 100 MB producing ``events.jsonl.1`` / ``.2`` / ... siblings.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_MAX_BYTES = 100 * 1024 * 1024  # 100 MB


def _sessions_root() -> Path:
    return Path(
        os.environ.get("DEVHARNESS_STATE_DIR")
        or Path.home() / ".devharness" / "sessions"
    )


class JsonlLogger:
    """One log per session — write events with ``log(event_dict)``."""

    def __init__(self, session_id: str, *,
                 path: str | Path | None = None,
                 max_bytes: int = _MAX_BYTES) -> None:
        self.session_id = session_id
        self.max_bytes = int(max_bytes)
        if path is not None:
            self.path = Path(path)
        else:
            self.path = _sessions_root() / session_id / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = open(self.path, "a", encoding="utf-8")

    def log(self, event: dict[str, Any]) -> None:
        """Write one event as a single JSON line. Non-blocking best-effort:
        on IO error the exception propagates — callers can catch and
        continue (observability must not crash the loop)."""
        body = dict(event)
        body.setdefault("ts_ns", int(time.time_ns()))
        line = json.dumps(body, sort_keys=True, separators=(",", ":"),
                          default=str) + "\n"
        with self._lock:
            self._rotate_if_needed(len(line))
            self._fh.write(line)
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    # ── rotation ─────────────────────────────────────────────────────────
    def _rotate_if_needed(self, next_write_len: int) -> None:
        try:
            size = self._fh.tell()
        except (OSError, ValueError):
            size = 0
        if size + next_write_len < self.max_bytes:
            return
        self._fh.close()
        # Shift .N -> .N+1, up to a small cap; the most recent rotated
        # file becomes .1. We only keep 9 to bound disk usage.
        for i in range(9, 0, -1):
            src = self.path.with_suffix(self.path.suffix + f".{i}")
            dst = self.path.with_suffix(self.path.suffix + f".{i + 1}")
            if src.exists():
                if i == 9:
                    src.unlink()
                else:
                    src.rename(dst)
        rotated = self.path.with_suffix(self.path.suffix + ".1")
        if self.path.exists():
            self.path.rename(rotated)
        self._fh = open(self.path, "a", encoding="utf-8")

    def __enter__(self) -> "JsonlLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
