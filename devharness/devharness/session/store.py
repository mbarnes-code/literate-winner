"""Append-only SQLite session store for the devharness rollout log.

Ported from vvaharness/orchestrator/store.py
  License: Apache License 2.0
  Copyright 2026 Visa, Inc.
  Source: https://github.com/visa/visa-vulnerability-agentic-harness
  Upstream commit: 3d972f679d8f5e3838b394edee0b5ea9c626b0fb
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable adaptations:
  - Schema replaced to match devharness spec §4.1: turns / approvals /
    snapshots / env tables. Dropped VVAH-specific runs / checkpoints /
    callgraph_snapshots / callgraph_nodes / callgraph_edges /
    callgraph_stage_refs tables and their save_callgraph/load_callgraph
    helpers (VVAH-only pipeline state).
  - Per-session DB path: ~/.devharness/sessions/{session_id}/rollout.db
    (overridable via $DEVHARNESS_STATE_DIR), replacing VVAH's single
    ~/.vvaharness/state/vvaharness.db and VVAHARNESS_STATE_DIR.
  - Payloads are plain dict[str, Any] serialised as JSON text (TEXT
    column, checked with json_valid()); pydantic contract wrappers left
    as a TODO to wire once devharness/session/models.py lands.
  - Preserved WAL / synchronous=NORMAL / foreign_keys=ON /
    busy_timeout=30s pragma set and the PRAGMA user_version migration
    guard.
"""
from __future__ import annotations
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1

# auto_vacuum MUST precede the first CREATE TABLE or it is silently ignored;
# journal_mode=WAL persists once set. foreign_keys / busy_timeout /
# synchronous are per-connection and set separately in connect().
_DDL = """
PRAGMA auto_vacuum  = INCREMENTAL;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS turns (
  turn_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       INTEGER NOT NULL,
  kind     TEXT    NOT NULL,
  payload  TEXT    NOT NULL CHECK (json_valid(payload))
);

CREATE TABLE IF NOT EXISTS approvals (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  turn_id    INTEGER NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE,
  tool       TEXT    NOT NULL,
  args_hash  TEXT    NOT NULL,
  decision   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  ts    INTEGER NOT NULL,
  kind  TEXT    NOT NULL,
  sha   TEXT    NOT NULL,
  path  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS env (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_turns_ts        ON turns(ts);
CREATE INDEX IF NOT EXISTS ix_approvals_turn  ON approvals(turn_id);
CREATE INDEX IF NOT EXISTS ix_approvals_lookup
    ON approvals(tool, args_hash);
CREATE INDEX IF NOT EXISTS ix_snapshots_kind_sha
    ON snapshots(kind, sha);
"""


def _sessions_root() -> Path:
    root = Path(os.environ.get("DEVHARNESS_STATE_DIR")
                or Path.home() / ".devharness" / "sessions")
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path(session_id: str) -> Path:
    """Resolve the per-session rollout DB path.

    Layout: ``$DEVHARNESS_STATE_DIR/{session_id}/rollout.db`` (or
    ``~/.devharness/sessions/{session_id}/rollout.db`` when unset), per
    spec §4.1.
    """
    session_dir = _sessions_root() / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir / "rollout.db"


def connect(session_id: str) -> sqlite3.Connection:
    """Open the per-session rollout DB, ensure the schema exists, and return
    a connection.

    WAL gives N readers + 1 writer with no reader/writer blocking;
    ``busy_timeout`` makes a second concurrent writer wait up to 30 s for the
    single write lock instead of failing. ``synchronous=NORMAL`` is the
    documented WAL companion: still crash-safe (the WAL is fsynced on
    checkpoint), ~10x faster than the FULL default.

    Fresh connection per call — sqlite3 connections are thread-affine.
    """
    con = sqlite3.connect(db_path(session_id))
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA synchronous  = NORMAL")
    have = con.execute("PRAGMA user_version").fetchone()[0]
    if have != _SCHEMA_VERSION:
        _migrate(con, have)
    return con


def _migrate(con: sqlite3.Connection, have: int) -> None:
    if have == 0:
        con.executescript(_DDL)
    con.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    con.commit()


# ─────────────────────────────────────────────────────────────────────────────
# turns
# ─────────────────────────────────────────────────────────────────────────────

# TODO: swap dict[str, Any] payloads for pydantic TurnPayload / ApprovalRecord
#       models once devharness/session/models.py lands. Serialising via
#       TypeAdapter.dump_json() preserves the CWE-502 no-constructor-opcodes
#       guarantee that VVAH relied on.


def append_turn(session_id: str, kind: str, payload: dict[str, Any],
                *, ts: int | None = None) -> int:
    """Append one turn row and return the assigned turn_id.

    ``kind`` is a short discriminator (``user_message`` / ``assistant_message``
    / ``tool_call`` / ``tool_result`` / ``step_result`` / …). ``payload`` is
    stored as a JSON TEXT blob validated by the CHECK constraint.
    """
    ts_val = int(ts if ts is not None else time.time() * 1000)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    con = connect(session_id)
    try:
        with con:
            cur = con.execute(
                "INSERT INTO turns(ts, kind, payload) VALUES (?, ?, ?)",
                (ts_val, kind, body),
            )
            return int(cur.lastrowid or 0)
    finally:
        con.close()


def read_turns(session_id: str, *, since_turn_id: int = 0,
               kinds: list[str] | None = None,
               limit: int | None = None) -> list[dict[str, Any]]:
    """Read turns in insertion order. ``since_turn_id`` is exclusive.

    Each row is returned as ``{"turn_id", "ts", "kind", "payload"}`` with
    ``payload`` parsed back into a dict.
    """
    sql = "SELECT turn_id, ts, kind, payload FROM turns WHERE turn_id > ?"
    params: list[Any] = [int(since_turn_id)]
    if kinds:
        sql += " AND kind IN (" + ",".join("?" * len(kinds)) + ")"
        params.extend(kinds)
    sql += " ORDER BY turn_id ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    con = connect(session_id)
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    return [
        {"turn_id": tid, "ts": ts, "kind": kind, "payload": json.loads(payload)}
        for (tid, ts, kind, payload) in rows
    ]


def last_turn_id(session_id: str) -> int:
    """Return the highest turn_id in the store, or 0 if empty."""
    con = connect(session_id)
    try:
        row = con.execute("SELECT MAX(turn_id) FROM turns").fetchone()
    finally:
        con.close()
    return int(row[0] or 0)


# ─────────────────────────────────────────────────────────────────────────────
# approvals — cache of per-(tool, args_hash) decisions for replay
# ─────────────────────────────────────────────────────────────────────────────


def record_approval(session_id: str, *, turn_id: int, tool: str,
                    args_hash: str, decision: str) -> int:
    """Persist one approval decision. ``decision`` is caller-defined
    (typically ``allow`` / ``deny`` / ``allow_once``)."""
    con = connect(session_id)
    try:
        with con:
            cur = con.execute(
                "INSERT INTO approvals(turn_id, tool, args_hash, decision) "
                "VALUES (?, ?, ?, ?)",
                (int(turn_id), tool, args_hash, decision),
            )
            return int(cur.lastrowid or 0)
    finally:
        con.close()


def lookup_approval(session_id: str, *, tool: str,
                    args_hash: str) -> str | None:
    """Return the most recent decision for ``(tool, args_hash)`` or None.

    Replay mode reads decisions from here instead of prompting (spec §4.6).
    """
    con = connect(session_id)
    try:
        row = con.execute(
            "SELECT decision FROM approvals "
            "WHERE tool = ? AND args_hash = ? "
            "ORDER BY id DESC LIMIT 1",
            (tool, args_hash),
        ).fetchone()
    finally:
        con.close()
    return None if row is None else str(row[0])


# ─────────────────────────────────────────────────────────────────────────────
# snapshots — content-addressed pointers to captured files/blobs
# ─────────────────────────────────────────────────────────────────────────────


def record_snapshot(session_id: str, *, kind: str, sha: str, path: str,
                    ts: int | None = None) -> int:
    """Record a snapshot pointer (kind, sha, path). ``path`` is the location
    on disk of the captured artefact; ``sha`` is its content hash for
    deduplication / integrity checks."""
    ts_val = int(ts if ts is not None else time.time() * 1000)
    con = connect(session_id)
    try:
        with con:
            cur = con.execute(
                "INSERT INTO snapshots(ts, kind, sha, path) VALUES (?, ?, ?, ?)",
                (ts_val, kind, sha, path),
            )
            return int(cur.lastrowid or 0)
    finally:
        con.close()


def find_snapshot(session_id: str, *, kind: str,
                  sha: str) -> dict[str, Any] | None:
    """Return the most recent snapshot row matching ``(kind, sha)`` or None."""
    con = connect(session_id)
    try:
        row = con.execute(
            "SELECT id, ts, kind, sha, path FROM snapshots "
            "WHERE kind = ? AND sha = ? ORDER BY id DESC LIMIT 1",
            (kind, sha),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return {"id": row[0], "ts": row[1], "kind": row[2], "sha": row[3], "path": row[4]}


# ─────────────────────────────────────────────────────────────────────────────
# env — key/value manifest (model, provider, cwd hash, tool digest, …)
# ─────────────────────────────────────────────────────────────────────────────


def put_env(session_id: str, key: str, value: str) -> None:
    con = connect(session_id)
    try:
        with con:
            con.execute(
                "INSERT INTO env(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
    finally:
        con.close()


def get_env(session_id: str, key: str) -> str | None:
    con = connect(session_id)
    try:
        row = con.execute("SELECT value FROM env WHERE key = ?", (key,)).fetchone()
    finally:
        con.close()
    return None if row is None else str(row[0])


def all_env(session_id: str) -> dict[str, str]:
    con = connect(session_id)
    try:
        rows = con.execute("SELECT key, value FROM env").fetchall()
    finally:
        con.close()
    return {str(k): str(v) for (k, v) in rows}


# ─────────────────────────────────────────────────────────────────────────────
# lifecycle
# ─────────────────────────────────────────────────────────────────────────────


def reset_session(session_id: str) -> int:
    """Delete every row for one session (turns cascade to approvals). Returns
    the number of rows deleted across all tables."""
    con = connect(session_id)
    try:
        with con:
            cleared = 0
            for table in ("approvals", "turns", "snapshots", "env"):
                cur = con.execute(f"DELETE FROM {table}")
                cleared += cur.rowcount or 0
            return cleared
    finally:
        con.close()


def delete_session(session_id: str) -> bool:
    """Remove the on-disk rollout DB for one session. Returns True if a file
    was deleted."""
    path = db_path(session_id)
    if not path.exists():
        return False
    # WAL sidecar files must also be removed to fully evict the session.
    for suffix in ("", "-wal", "-shm"):
        p = path.with_name(path.name + suffix)
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    return True
