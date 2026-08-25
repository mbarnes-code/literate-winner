"""Budget enforcement — turn / token / USD caps (spec §4.2).

No upstream code copied — design from spec §4.2. The step reads
``state.budget`` (a ``BudgetConfig``) and compares each configured
threshold against the accumulated counters; the first threshold reached
returns ``Applied([StopSession(reason=...)])`` with a stable reason
string. When nothing is configured or nothing is breached, returns
``NotApplicable``.
"""

from __future__ import annotations

from devharness.loop.effects import Applied, NotApplicable, StepResult, StopSession
from devharness.loop.state import ThreadState, TurnContext


class BudgetCheck:
    """Enforce turn / token / cost budgets per ``BudgetConfig``."""

    name = "BudgetCheck"

    def apply(self, state: ThreadState, ctx: TurnContext) -> StepResult:
        b = state.budget
        if b is None:
            return NotApplicable

        if b.max_turns is not None and state.turn_id >= b.max_turns:
            return Applied((StopSession(reason="budget_exhausted:max_turns"),))
        if b.max_input_tokens is not None and state.tokens_input >= b.max_input_tokens:
            return Applied((StopSession(reason="budget_exhausted:max_input_tokens"),))
        if b.max_output_tokens is not None and state.tokens_output >= b.max_output_tokens:
            return Applied((StopSession(reason="budget_exhausted:max_output_tokens"),))
        if b.max_total_tokens is not None:
            total = state.tokens_input + state.tokens_output
            if total >= b.max_total_tokens:
                return Applied((StopSession(reason="budget_exhausted:max_total_tokens"),))
        if b.max_cost_usd is not None and state.cost_usd_running >= b.max_cost_usd:
            return Applied((StopSession(reason="budget_exhausted:max_cost_usd"),))

        return NotApplicable


__all__ = ["BudgetCheck"]
