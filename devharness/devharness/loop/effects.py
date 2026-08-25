"""Effect ADT for the loop ``Step[]`` engine.

No upstream code copied — design from spec §4.2.
Each ``Step.apply`` returns either ``NotApplicable`` (a sentinel — the engine
skips) or ``Applied(effects=(...))`` where each effect is a tagged
dataclass discriminated by ``kind``. The engine's effect handler dispatches
on ``kind`` and applies the mutation to ``ThreadState`` in the order the
step declared. Effects are inert values; only the engine applies them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union


@dataclass(slots=True, frozen=True)
class AppendMessage:
    """Append a message dict to ``state.messages``."""

    role: str
    content: str
    kind: Literal["append_message"] = "append_message"
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[dict[str, Any], ...] | None = None


@dataclass(slots=True, frozen=True)
class RunTools:
    """Ask the engine to dispatch the given tool-call batch."""

    tool_calls: tuple[dict[str, Any], ...]
    kind: Literal["run_tools"] = "run_tools"


@dataclass(slots=True, frozen=True)
class RequestApproval:
    """Ask the operator to approve a tool invocation before it runs."""

    tool_name: str
    args_hash: str
    kind: Literal["request_approval"] = "request_approval"


@dataclass(slots=True, frozen=True)
class EmitEvent:
    """Emit a structured observability event."""

    event: str
    payload: dict[str, Any] = field(default_factory=dict)
    kind: Literal["emit_event"] = "emit_event"


@dataclass(slots=True, frozen=True)
class Spillover:
    """Spill an oversized tool result to disk and rewrite it with a pointer."""

    turn_id: int
    tool_name: str
    call_id: str
    content: str
    kind: Literal["spillover"] = "spillover"


@dataclass(slots=True, frozen=True)
class Compact:
    """Trigger conversation compaction — the compaction step already ran."""

    cutoff_index: int
    summary: str
    summary_path: str
    kind: Literal["compact"] = "compact"


@dataclass(slots=True, frozen=True)
class StopSession:
    """Signal the engine to stop the session with a reason string."""

    reason: str
    kind: Literal["stop_session"] = "stop_session"


@dataclass(slots=True, frozen=True)
class IncrementCost:
    """Add to the running token / USD counters."""

    tokens_input: int = 0
    tokens_output: int = 0
    usd: float = 0.0
    kind: Literal["increment_cost"] = "increment_cost"


Effect = Union[
    AppendMessage,
    RunTools,
    RequestApproval,
    EmitEvent,
    Spillover,
    Compact,
    StopSession,
    IncrementCost,
]


@dataclass(slots=True, frozen=True)
class Applied:
    """A step successfully applied; ``effects`` is the ordered tuple to run."""

    effects: tuple[Effect, ...] = ()


class _NotApplicableSentinel:
    """Sentinel type for the ``NotApplicable`` singleton."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "NotApplicable"

    def __bool__(self) -> bool:
        return False


NotApplicable: _NotApplicableSentinel = _NotApplicableSentinel()
StepResult = Union[Applied, _NotApplicableSentinel]


__all__ = [
    "AppendMessage",
    "Applied",
    "Compact",
    "Effect",
    "EmitEvent",
    "IncrementCost",
    "NotApplicable",
    "RequestApproval",
    "RunTools",
    "Spillover",
    "StepResult",
    "StopSession",
]
