"""Conversation compaction step — token-triggered summarize + keep-N (spec §4.8).

Design ported from deepagents/libs/deepagents/deepagents/middleware/summarization.py
  License: MIT License
  Copyright (c) LangChain, Inc.
  Source: https://github.com/langchain-ai/deepagents
  Upstream commit: 23b83ad50f63d241d0069a3dc426d43b211adf2e
  Strategy: extract-pattern (trigger + keep-N + summary persistence).
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable adaptations:
  - LangChain is not a runtime dep. No ``SummarizationMiddleware``,
    ``BaseChatModel``, or ``ContextOverflowError`` imports — the step
    reads ``ThreadState.messages`` (provider-neutral dicts) and calls a
    caller-supplied ``summarize`` function whose only contract is
    ``(list[dict]) -> str``.
  - ``trigger_tokens`` and ``keep_recent_messages`` come from
    ``compaction`` config in TOML (spec §4.8), not the deepagents
    ``ContextSize`` tuple.
  - Summary text is persisted to
    ``~/.devharness/sessions/{session_id}/summaries/{turn_id}.md`` per
    spec §4.8, not the deepagents ``/conversation_history/`` backend.
  - Anti-thrash cooldown (spec §4.8) is enforced in-process by tracking
    the last-compact turn on the step instance.
  - Media-offloading (``_MEDIA_REFERENCE_SUMMARY_PROMPT``) is skipped —
    devharness tool results are text; oversized results are handled by
    the separate ``ResultSpillover`` step.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from devharness.loop.effects import Applied, Compact, NotApplicable, StepResult
from devharness.loop.state import ThreadState, TurnContext

logger = logging.getLogger(__name__)

_DEFAULT_TRIGGER_TOKENS = 100_000
_DEFAULT_KEEP_RECENT = 20
_DEFAULT_COOLDOWN_S = 600
# Rough per-token char count for the built-in approximator; the caller can
# always inject a real tokenizer via ``token_counter``.
_CHARS_PER_TOKEN = 4


def _devharness_home() -> Path:
    override = os.environ.get("DEVHARNESS_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".devharness"


def _approx_tokens(messages: list[dict[str, Any]]) -> int:
    total_chars = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total_chars += len(str(block.get("text", "")))
        for tc in m.get("tool_calls") or ():
            fn = tc.get("function") or {}
            total_chars += len(str(fn.get("arguments", "")))
    return total_chars // _CHARS_PER_TOKEN


class Compaction:
    """Compact the message history when it exceeds the token trigger."""

    name = "Compaction"

    def __init__(
        self,
        *,
        summarize: Callable[[list[dict[str, Any]]], str],
        trigger_tokens: int = _DEFAULT_TRIGGER_TOKENS,
        keep_recent_messages: int = _DEFAULT_KEEP_RECENT,
        anti_thrash_cooldown_s: int = _DEFAULT_COOLDOWN_S,
        token_counter: Callable[[list[dict[str, Any]]], int] | None = None,
    ) -> None:
        if keep_recent_messages < 1:
            raise ValueError("keep_recent_messages must be >= 1")
        self._summarize = summarize
        self._trigger_tokens = trigger_tokens
        self._keep = keep_recent_messages
        self._cooldown_s = anti_thrash_cooldown_s
        self._counter = token_counter or _approx_tokens
        self._last_compact_at: float = 0.0

    def _should_compact(self, messages: list[dict[str, Any]]) -> bool:
        # Anti-thrash: cooldown between compactions.
        if self._last_compact_at and (time.time() - self._last_compact_at) < self._cooldown_s:
            return False
        # Need enough non-system messages to actually compact anything.
        non_system = sum(1 for m in messages if m.get("role") != "system")
        if non_system <= self._keep:
            return False
        return self._counter(messages) >= self._trigger_tokens

    def _partition(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
        # System messages are always preserved at the head.
        head_system: list[dict[str, Any]] = []
        conv: list[dict[str, Any]] = []
        for m in messages:
            (head_system if m.get("role") == "system" else conv).append(m)
        cutoff = max(0, len(conv) - self._keep)
        return head_system, cutoff, conv

    def _summary_path(self, session_id: str, turn_id: int) -> Path:
        base = _devharness_home() / "sessions" / session_id / "summaries"
        base.mkdir(parents=True, exist_ok=True)
        return base / f"{turn_id}.md"

    def apply(self, state: ThreadState, ctx: TurnContext) -> StepResult:
        if not self._should_compact(state.messages):
            return NotApplicable

        head_system, cutoff, conv = self._partition(state.messages)
        to_summarize = conv[:cutoff]
        to_keep = conv[cutoff:]
        if not to_summarize:
            return NotApplicable

        try:
            summary = self._summarize(to_summarize)
        except Exception:
            logger.exception("summarize() raised; skipping compaction this turn")
            return NotApplicable

        summary_path = self._summary_path(state.session_id, state.turn_id)
        try:
            summary_path.write_text(summary, encoding="utf-8")
        except OSError:
            logger.exception("could not persist summary to %s", summary_path)

        # Rewrite the state messages: [system...] + [summary marker] + kept.
        summary_marker = {
            "role": "user",
            "content": f"[compacted-history]\n{summary}",
            "name": "compaction",
        }
        state.messages[:] = [*head_system, summary_marker, *to_keep]
        self._last_compact_at = time.time()

        return Applied((
            Compact(
                cutoff_index=cutoff,
                summary=summary,
                summary_path=str(summary_path),
            ),
        ))


__all__ = ["Compaction"]
