"""3-tier prompt assembly (stable / context / volatile) per spec §4.10.

Adapted (pattern only) from vvaharness/util/prompts.py
  License: Apache License 2.0
  Copyright 2026 Visa, Inc.
  Source: https://github.com/visa/visa-vulnerability-agentic-harness
  Upstream commit: 3d972f679d8f5e3838b394edee0b5ea9c626b0fb
  Strategy: extract-pattern (shared prompt-blocks composed by a builder;
    no vvaharness text is reused — devharness ships its own templates).
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable adaptations:
  - Rendering is Jinja2 over three template files
    (``prompts/system_{stable,context,volatile}.jinja2``) rather than
    Python string constants concatenated at import time.
  - The tier-1 (stable) output is SHA-256'd and pinned on
    ``ctx.prompt_hash_tier1``. If a subsequent turn produces a different
    hash, the step raises — spec §4.10's "fail-loud" contract.
  - Emits a single ``AppendMessage(role="system", ...)`` combining all three
    tiers, per the spec §4.10 tiered-prompt contract.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from devharness.loop.effects import AppendMessage, Applied, NotApplicable, StepResult
from devharness.loop.state import ThreadState, TurnContext

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
_TIER1 = "system_stable.jinja2"
_TIER2 = "system_context.jinja2"
_TIER3 = "system_volatile.jinja2"


def _render(name: str, vars_: Mapping[str, Any]) -> str:
    # Lazy import — jinja2 is an optional-at-import-time runtime dep for the
    # loop but a hard dep of the prompt builder.
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    env = Environment(
        loader=FileSystemLoader(str(_PROMPTS_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )
    return env.get_template(name).render(**dict(vars_))


class PromptBuilder:
    """Assemble stable/context/volatile tiers and freeze the tier-1 hash."""

    name = "PromptBuilder"

    def __init__(self, *, tier1_vars: Mapping[str, Any] | None = None,
                 tier2_vars: Mapping[str, Any] | None = None,
                 tier3_vars: Mapping[str, Any] | None = None) -> None:
        self._tier1_vars = dict(tier1_vars or {})
        self._tier2_vars = dict(tier2_vars or {})
        self._tier3_vars = dict(tier3_vars or {})

    def apply(self, state: ThreadState, ctx: TurnContext) -> StepResult:
        if any(m.get("role") == "system" for m in state.messages):
            return NotApplicable

        tier1 = _render(_TIER1, self._tier1_vars)
        tier1_hash = hashlib.sha256(tier1.encode("utf-8")).hexdigest()

        if state.prompt_hash_tier1 and state.prompt_hash_tier1 != tier1_hash:
            raise RuntimeError(
                f"tier-1 prompt hash drifted mid-session "
                f"(was {state.prompt_hash_tier1!r}, now {tier1_hash!r})"
            )
        state.prompt_hash_tier1 = tier1_hash
        ctx.prompt_hash_tier1 = tier1_hash

        tier2_vars = {**self._tier2_vars, "repo_hints": ctx.repo_hints}
        tier3_vars = {**self._tier3_vars, "turn_id": state.turn_id}
        tier2 = _render(_TIER2, tier2_vars)
        tier3 = _render(_TIER3, tier3_vars)

        combined = "\n\n".join(part for part in (tier1, tier2, tier3) if part.strip())
        return Applied((AppendMessage(role="system", content=combined),))


__all__ = ["PromptBuilder"]
