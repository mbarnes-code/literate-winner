"""Stop-hook driver — run ``state.stop_hooks`` validators; any veto stops end.

No upstream code copied — design from spec §4.2.
Each hook in ``state.stop_hooks`` is called with the current ``ThreadState``.
The hook contract:
  - Return ``None`` / falsy → hook is satisfied; turn end may proceed.
  - Return a ``str`` → hook vetoed the turn end; the string becomes the
    ``StopSession`` reason. The engine will stop the current turn and
    surface the reason to the caller.
Hooks are run in order; the first veto short-circuits the rest.
"""

from __future__ import annotations

import logging

from devharness.loop.effects import Applied, NotApplicable, StepResult, StopSession
from devharness.loop.state import ThreadState, TurnContext

logger = logging.getLogger(__name__)


class StopHooks:
    """Iterate ``state.stop_hooks`` and let any non-empty return veto."""

    name = "StopHooks"

    def apply(self, state: ThreadState, ctx: TurnContext) -> StepResult:
        for hook in state.stop_hooks:
            try:
                result = hook(state)
            except Exception:
                logger.exception("stop hook %r raised; treating as veto", hook)
                return Applied((StopSession(reason="stop_hook_raised"),))
            if not result:
                continue
            reason = str(result) if not isinstance(result, str) else result
            return Applied((StopSession(reason=f"stop_hook:{reason}"),))
        return NotApplicable


__all__ = ["StopHooks"]
