"""Semantic writer over the append-only rollout log.

No upstream code copied — this is a devharness design layer on top of
``devharness/session/store.py`` (itself lifted from VVAH). Everything
below is a thin, spec-driven facade so callers speak in loop-vocabulary
(``record_turn`` / ``record_approval`` / ``record_snapshot``) rather
than SQL. See docs/devharness-spec.md §4.1 for the durability contract.

Design notes:
  - Every mutation flows through a single ``sqlite3`` connection opened
    per Rollout instance; ``BEGIN IMMEDIATE`` acquires the writer lock
    up-front so concurrent recorders queue on the SQLite busy-timeout
    (30 s, inherited from ``store.connect``) instead of racing.
  - Timestamps are always ``int(time.time_ns())`` and enforced to be
    monotonically non-decreasing across a single Rollout — the underlying
    ``turns.ts`` column stores the raw ns value (widening from the
    existing millisecond convention is caller-driven, no schema change
    needed).
  - ``flush()`` commits any open transaction and is a no-op when the
    connection is idle; it never closes the connection.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from typing import Any

from . import store

_MONO_LOCK = threading.Lock()


class Rollout:
    """Semantic wrapper around one session's rollout SQLite DB."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._con: sqlite3.Connection = store.connect(session_id)
        self._in_tx = False
        self._last_ts_ns = 0

    # ── ts helper ────────────────────────────────────────────────────────
    def _next_ts(self) -> int:
        """Monotonic ns timestamp; clamps clock skew to prev+1."""
        with _MONO_LOCK:
            now = int(time.time_ns())
            if now <= self._last_ts_ns:
                now = self._last_ts_ns + 1
            self._last_ts_ns = now
            return now

    # ── tx helpers ───────────────────────────────────────────────────────
    def _begin(self) -> None:
        if not self._in_tx:
            self._con.execute("BEGIN IMMEDIATE")
            self._in_tx = True

    def flush(self) -> None:
        """Commit any pending writes; safe to call when idle."""
        if self._in_tx:
            self._con.execute("COMMIT")
            self._in_tx = False

    def close(self) -> None:
        self.flush()
        self._con.close()

    # ── public API ───────────────────────────────────────────────────────
    def record_turn(self, turn_id: int | None, kind: str,
                    payload: dict[str, Any]) -> int:
        """Append one turn row.

        ``turn_id`` is accepted for symmetry with the spec's naming but is
        assigned by SQLite (AUTOINCREMENT); when the caller passes a
        non-None value it is stored inside the payload under
        ``__caller_turn_id`` for auditing. Returns the SQLite-assigned
        turn_id.
        """
        body = dict(payload)
        if turn_id is not None:
            body.setdefault("__caller_turn_id", int(turn_id))
        ts_ns = self._next_ts()
        text = json.dumps(body, sort_keys=True, separators=(",", ":"))
        self._begin()
        cur = self._con.execute(
            "INSERT INTO turns(ts, kind, payload) VALUES (?, ?, ?)",
            (ts_ns, kind, text),
        )
        return int(cur.lastrowid or 0)

    def record_approval(self, *, turn_id: int, tool: str,
                        args: dict[str, Any] | None = None,
                        args_hash: str | None = None,
                        decision: str) -> int:
        """Cache one approval decision (spec §4.7).

        Either pre-computed ``args_hash`` or raw ``args`` must be given.
        When ``args`` is provided its sha256 over canonical JSON is used
        (same shape as ApprovalGate's cache key).
        """
        if args_hash is None:
            if args is None:
                raise ValueError("record_approval requires args or args_hash")
            canon = json.dumps({"tool": tool, "args": args},
                               sort_keys=True, separators=(",", ":"))
            args_hash = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        self._begin()
        cur = self._con.execute(
            "INSERT INTO approvals(turn_id, tool, args_hash, decision) "
            "VALUES (?, ?, ?, ?)",
            (int(turn_id), tool, args_hash, decision),
        )
        return int(cur.lastrowid or 0)

    def record_snapshot(self, *, kind: str, sha: str, path: str) -> int:
        """Record a content-addressed snapshot pointer (spec §4.1)."""
        ts_ns = self._next_ts()
        self._begin()
        cur = self._con.execute(
            "INSERT INTO snapshots(ts, kind, sha, path) VALUES (?, ?, ?, ?)",
            (ts_ns, kind, sha, path),
        )
        return int(cur.lastrowid or 0)

    # ── context manager ──────────────────────────────────────────────────
    def __enter__(self) -> "Rollout":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None and self._in_tx:
            self._con.execute("ROLLBACK")
            self._in_tx = False
        self.close()
