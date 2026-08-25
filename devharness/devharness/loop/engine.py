"""``LoopEngine`` — ordered ``Step[]`` driver for one turn (spec §4.2).

Design reference (no code copied): goose ``crates/goose-agent/machine.rs``
(Apache-2.0) — the explicit-step state machine pattern. Reimplemented in
Python from scratch; goose is Rust and cross-language.

The engine holds a frozen list of ``Step`` instances, records
``sha256(step-list)`` on init (written to the env manifest per spec §4.2),
and drives one turn by iterating steps in order. Each step returns
``NotApplicable`` (engine advances) or ``Applied(effects)`` (engine applies
effects via ``kind`` dispatch, then advances). A ``StopSession`` effect
sets ``turn_ctx.stop_reason`` and terminates the turn early.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from typing import Protocol, runtime_checkable

from devharness.loop.effects import (
    AppendMessage,
    Applied,
    Compact,
    EmitEvent,
    IncrementCost,
    NotApplicable,
    RequestApproval,
    RunTools,
    Spillover,
    StepResult,
    StopSession,
)
from devharness.loop.state import ThreadState, TurnContext

logger = logging.getLogger(__name__)


@runtime_checkable
class Step(Protocol):
    """One deterministic step in the loop (spec §4.2)."""

    name: str

    def apply(self, state: ThreadState, ctx: TurnContext) -> StepResult:
        """Return ``NotApplicable`` or ``Applied(effects=(...))``."""
        ...


class LoopEngine:
    """Drive one turn = one pass through the ordered ``Step[]`` (spec §4.2)."""

    def __init__(self, steps: list[Step]) -> None:
        self.steps: tuple[Step, ...] = tuple(steps)
        self.steps_hash: str = self._compute_steps_hash(self.steps)

    @staticmethod
    def _compute_steps_hash(steps: tuple[Step, ...]) -> str:
        payload = json.dumps(
            [getattr(s, "name", type(s).__name__) for s in steps],
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def run_turn(self, state: ThreadState, ctx: TurnContext) -> ThreadState:
        """Run one turn; return the mutated ``state``.

        The engine mutates ``state`` in place for list-typed fields
        (``messages``) and via ``dataclasses.replace`` for immutable
        counters. Steps observe the accumulated state — a later step
        sees effects from all prior steps in the same turn.
        """
        if not state.steps_hash:
            state.steps_hash = self.steps_hash
        elif state.steps_hash != self.steps_hash:
            raise RuntimeError(
                f"steps_hash changed mid-session "
                f"(was {state.steps_hash!r}, now {self.steps_hash!r})"
            )

        for step in self.steps:
            try:
                result = step.apply(state, ctx)
            except Exception:
                logger.exception("step %r raised", getattr(step, "name", step))
                raise
            if result is NotApplicable:
                continue
            if not isinstance(result, Applied):
                raise TypeError(
                    f"{getattr(step, 'name', step)!r}: apply() returned {type(result)!r}"
                )
            state = self._apply_effects(state, ctx, result.effects)
            if ctx.stop_reason is not None:
                break
        return state

    def _apply_effects(
        self,
        state: ThreadState,
        ctx: TurnContext,
        effects: tuple,
    ) -> ThreadState:
        for effect in effects:
            kind = getattr(effect, "kind", None)
            handler = _EFFECT_HANDLERS.get(kind)
            if handler is None:
                raise ValueError(f"unhandled effect kind: {kind!r}")
            state = handler(state, ctx, effect)
        return state


# ---- effect handlers ---------------------------------------------------------
# Each handler takes (state, ctx, effect) and returns state (possibly rebuilt).


def _h_append_message(state: ThreadState, ctx: TurnContext, e: AppendMessage) -> ThreadState:
    msg: dict = {"role": e.role, "content": e.content}
    if e.name is not None:
        msg["name"] = e.name
    if e.tool_call_id is not None:
        msg["tool_call_id"] = e.tool_call_id
    if e.tool_calls is not None:
        msg["tool_calls"] = list(e.tool_calls)
    state.messages.append(msg)
    return state


def _h_run_tools(state: ThreadState, ctx: TurnContext, e: RunTools) -> ThreadState:
    ctx.planned_batches = tuple(e.tool_calls)
    return state


def _h_request_approval(state: ThreadState, ctx: TurnContext, e: RequestApproval) -> ThreadState:
    # ApprovalGate step owns the actual prompt; this effect is a marker for
    # observers.
    return state


def _h_emit_event(state: ThreadState, ctx: TurnContext, e: EmitEvent) -> ThreadState:
    logger.info("event %s payload=%s", e.event, e.payload)
    return state


def _h_spillover(state: ThreadState, ctx: TurnContext, e: Spillover) -> ThreadState:
    # The ResultSpillover step performs the disk write and rewrites the
    # tool-result message; this effect is a marker/record for observers.
    return state


def _h_compact(state: ThreadState, ctx: TurnContext, e: Compact) -> ThreadState:
    # The Compaction step performs the eviction+summary write; here we only
    # append a HumanMessage-style summary marker if not already present.
    return state


def _h_stop_session(state: ThreadState, ctx: TurnContext, e: StopSession) -> ThreadState:
    ctx.stop_reason = e.reason
    return state


def _h_increment_cost(state: ThreadState, ctx: TurnContext, e: IncrementCost) -> ThreadState:
    return dataclasses.replace(
        state,
        tokens_input=state.tokens_input + e.tokens_input,
        tokens_output=state.tokens_output + e.tokens_output,
        cost_usd_running=state.cost_usd_running + e.usd,
        messages=state.messages,
        stop_hooks=state.stop_hooks,
    )


_EFFECT_HANDLERS = {
    "append_message": _h_append_message,
    "run_tools": _h_run_tools,
    "request_approval": _h_request_approval,
    "emit_event": _h_emit_event,
    "spillover": _h_spillover,
    "compact": _h_compact,
    "stop_session": _h_stop_session,
    "increment_cost": _h_increment_cost,
}


__all__ = ["LoopEngine", "Step"]
