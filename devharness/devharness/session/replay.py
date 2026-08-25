"""Replay-from-rollout — reconstruct session state from the rollout log.

No upstream code copied — designed against docs/devharness-spec.md §4.1
(rollout schema, determinism contract) and §11 (end-to-end replay flow).
The replay reader is intentionally read-only: it opens the per-session
DB via ``store.connect`` and yields ``TurnRecord`` rows in insertion
order, plus recorded approvals keyed for cache lookup.

Semantics:
  - ``replay(session_id)`` streams every turn row in ``turn_id`` order.
  - When the driver processes a ``RunTools`` effect (``kind ==
    "tool_call"``), the next matching ``tool_result`` row is the recorded
    result — the caller can pair them with ``lookup_tool_result``.
  - When the driver hits an approval prompt, ``lookup_approval`` returns
    the cached decision (delegated to ``store.lookup_approval``); if no
    decision was recorded, replay raises ``ReplayGapError`` so the
    determinism contract fails loud.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator

from . import store


class ReplayGapError(RuntimeError):
    """Raised when replay reaches a decision that was not recorded."""


@dataclass(frozen=True)
class TurnRecord:
    turn_id: int
    ts: int
    kind: str
    payload: dict[str, Any]


def replay(session_id: str) -> Iterator[TurnRecord]:
    """Yield every turn row for a session in insertion order."""
    con = store.connect(session_id)
    try:
        cur = con.execute(
            "SELECT turn_id, ts, kind, payload FROM turns ORDER BY turn_id ASC"
        )
        for tid, ts, kind, payload in cur:
            yield TurnRecord(
                turn_id=int(tid),
                ts=int(ts),
                kind=str(kind),
                payload=json.loads(payload),
            )
    finally:
        con.close()


def lookup_tool_result(session_id: str, *, tool_name: str,
                       call_id: str) -> dict[str, Any] | None:
    """Return the recorded ``tool_result`` payload matching one call.

    Match key is ``(payload.tool, payload.call_id)`` — kept out of the
    schema so the store stays generic; the loop populates both fields
    when writing tool_result rows. Returns None on miss so the driver
    can decide whether the miss is a bug (replay) or fine (live).
    """
    con = store.connect(session_id)
    try:
        cur = con.execute(
            "SELECT payload FROM turns WHERE kind = 'tool_result' "
            "ORDER BY turn_id ASC"
        )
        for (payload,) in cur:
            row = json.loads(payload)
            if row.get("tool") == tool_name and row.get("call_id") == call_id:
                return row
    finally:
        con.close()
    return None


def lookup_approval(session_id: str, *, tool: str,
                    args_hash: str) -> str:
    """Return the recorded approval decision or raise ``ReplayGapError``.

    Thin wrapper over ``store.lookup_approval`` that enforces the
    determinism-contract requirement from spec §4.7: replay mode reads
    decisions from the rollout instead of prompting.
    """
    decision = store.lookup_approval(
        session_id, tool=tool, args_hash=args_hash
    )
    if decision is None:
        raise ReplayGapError(
            f"no recorded approval for tool={tool!r} args_hash={args_hash!r}"
        )
    return decision
