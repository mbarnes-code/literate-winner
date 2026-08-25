"""Loop state dataclasses — ``ThreadState``, ``TurnContext``, ``BudgetConfig``.

No upstream code copied — design from spec §4.2.
The dataclasses are the closed set of values that flow through the ``Step[]``
engine: ``ThreadState`` is the accumulator each ``Step.apply`` reads and each
``Effect`` mutates; ``TurnContext`` is the per-turn scratch space that never
survives a turn boundary; ``BudgetConfig`` is the frozen policy the
``BudgetCheck`` step consults.

Design notes:
  - All classes use ``dataclass(slots=True)`` so hot-path attribute access
    avoids ``__dict__`` and mismatched attribute writes fail loud.
  - ``ThreadState.messages`` is a plain list because the effect handler
    appends in-place; the rest of the state — ids, hashes, budgets — is
    treated as immutable and rebuilt via ``dataclasses.replace``.
  - ``BudgetConfig`` is frozen; the operator sets it once per session.
  - ``TurnContext`` is *not* frozen because steps stash intermediate values
    on it (``prompt_hash_tier1``, injection findings, tool-plan batches).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(slots=True, frozen=True)
class BudgetConfig:
    """Session-scoped budget policy (spec §4.2).

    ``None`` on any field disables that particular check.
    """

    max_turns: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None


@dataclass(slots=True)
class ThreadState:
    """Accumulator for one logical thread of the loop (spec §4.2).

    Fields:
      - ``session_id``: rollout key; used to derive per-session paths.
      - ``turn_id``: monotonically increasing turn counter.
      - ``messages``: chat history in provider-neutral dict form.
      - ``cost_usd_running``: cumulative USD across the session.
      - ``tokens_input`` / ``tokens_output``: cumulative token counters.
      - ``budget``: the policy ``BudgetCheck`` consults.
      - ``stop_hooks``: callables run by ``StopHooks``; any that returns a
        truthy value vetoes the turn end.
      - ``steps_hash``: SHA-256 of the loop's step-list, written to the env
        manifest at session start.
      - ``prompt_hash_tier1``: SHA-256 of the frozen tier-1 system prompt.
    """

    session_id: str
    turn_id: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    cost_usd_running: float = 0.0
    tokens_input: int = 0
    tokens_output: int = 0
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    stop_hooks: tuple[Callable[["ThreadState"], Any], ...] = ()
    steps_hash: str = ""
    prompt_hash_tier1: str = ""


@dataclass(slots=True)
class TurnContext:
    """Per-turn scratch space — discarded at turn boundary (spec §4.2).

    Steps write here to communicate downstream: ``PromptBuilder`` stores the
    frozen tier-1 hash for ``VerificationGate`` to check, ``PromptInjectionScan``
    stashes findings, ``ToolDispatch`` writes the planned batches.
    """

    cwd: str
    tools_hash: str = ""
    prompt_hash_tier1: str = ""
    repo_hints: str = ""
    injection_findings: tuple[Any, ...] = ()
    planned_batches: tuple[Any, ...] = ()
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None


__all__ = ["BudgetConfig", "ThreadState", "TurnContext"]
