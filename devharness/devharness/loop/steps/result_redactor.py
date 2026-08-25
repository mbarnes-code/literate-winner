"""Redact secrets/PII in tool results before they re-enter model context.

Wire-up of the already-lifted redaction module — no upstream code copied
here. The heavy-lifting regexes live in ``devharness/redaction/rules.py``
(ported from ``vvaharness/report/redact.py``; see that file's docstring
and ``scripts/LIFT_MANIFEST.toml`` for provenance). This step calls
``redaction.rules.redact`` on the ``content`` of every ``tool``-role
message the current turn appended.
"""

from __future__ import annotations

from devharness.loop.effects import Applied, NotApplicable, StepResult
from devharness.loop.state import ThreadState, TurnContext
from devharness.redaction.rules import redact


class ResultRedactor:
    """Rewrite tool-role messages in place with redacted content."""

    name = "ResultRedactor"

    def apply(self, state: ThreadState, ctx: TurnContext) -> StepResult:
        rewritten = 0
        # Iterate the tail of state.messages: only the tool messages appended
        # this turn need scrubbing; earlier ones were already handled.
        for msg in state.messages:
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content:
                continue
            if msg.get("_redacted"):
                continue
            new_content, _counts = redact(content)
            if new_content != content:
                msg["content"] = new_content
                rewritten += 1
            msg["_redacted"] = True

        return NotApplicable if rewritten == 0 else Applied(())


__all__ = ["ResultRedactor"]
