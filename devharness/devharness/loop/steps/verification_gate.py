"""Verification gate — post-mutation ``assert_no_diff`` + affected-test rerun.

No upstream code copied — design from spec §4.2.
The gate runs after tool execution to catch two failure modes cheaply:
  1. Silent mutations: a tool declared as ``read-only`` that actually
     modified the working tree. The ``assert_no_diff`` callable is
     expected to raise / return a truthy reason when the working tree
     diverged from the last known snapshot.
  2. Broken tests: if any file in the mutated set is covered by tests
     the tool registry can enumerate, those tests are re-run.

Both callables are injected — this module has no opinion on how ``git
diff`` is computed or which test runner is used, only on the sequencing
and veto semantics. A failure surfaces as ``StopSession(reason=...)``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Sequence

from devharness.loop.effects import Applied, NotApplicable, StepResult, StopSession
from devharness.loop.state import ThreadState, TurnContext

logger = logging.getLogger(__name__)


class VerificationGate:
    """Post-mutation invariant + regression check."""

    name = "VerificationGate"

    def __init__(
        self,
        *,
        assert_no_diff: Callable[[ThreadState, TurnContext], Any] | None = None,
        rerun_affected_tests: Callable[[ThreadState, TurnContext, Sequence[str]], Any] | None = None,
        mutated_paths: Callable[[ThreadState, TurnContext], Sequence[str]] | None = None,
    ) -> None:
        self._assert_no_diff = assert_no_diff
        self._rerun = rerun_affected_tests
        self._mutated_paths = mutated_paths

    def apply(self, state: ThreadState, ctx: TurnContext) -> StepResult:
        # Only run when the turn produced at least one tool result.
        if not ctx.tool_results and self._assert_no_diff is None:
            return NotApplicable

        if self._assert_no_diff is not None:
            try:
                diff_result = self._assert_no_diff(state, ctx)
            except Exception as exc:
                logger.exception("assert_no_diff raised")
                return Applied((StopSession(reason=f"verification:assert_no_diff:{exc}"),))
            if diff_result:
                return Applied((StopSession(reason=f"verification:diff:{diff_result}"),))

        if self._rerun is not None and self._mutated_paths is not None:
            try:
                paths = list(self._mutated_paths(state, ctx))
            except Exception:
                logger.exception("mutated_paths raised; skipping rerun")
                return NotApplicable
            if paths:
                try:
                    fail = self._rerun(state, ctx, paths)
                except Exception as exc:
                    logger.exception("rerun_affected_tests raised")
                    return Applied((StopSession(reason=f"verification:tests:{exc}"),))
                if fail:
                    return Applied((StopSession(reason=f"verification:tests_failed:{fail}"),))

        return NotApplicable


__all__ = ["VerificationGate"]
