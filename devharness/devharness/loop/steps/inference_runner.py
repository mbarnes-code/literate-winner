"""Inference dispatcher — invoke ``Provider.stream()`` for one turn (spec §4.3).

No upstream code copied — design from spec §4.3.
The step consumes streaming ``StreamEvent``s, materializes an assistant
message (text + optional ``tool_calls``), and emits an ``IncrementCost``
effect derived from the provider's ``usage`` event and ``price_per_1k``.
"""

from __future__ import annotations

import json
from typing import Any

from devharness.loop.effects import AppendMessage, Applied, IncrementCost, NotApplicable, StepResult
from devharness.loop.state import ThreadState, TurnContext


def _price(provider: Any, tokens_in: int, tokens_out: int,
           cache_read: int = 0, cache_write: int = 0) -> float:
    try:
        p_in, p_out, p_cr, p_cw = provider.price_per_1k()
    except Exception:
        return 0.0
    return (
        (tokens_in * p_in)
        + (tokens_out * p_out)
        + (cache_read * p_cr)
        + (cache_write * p_cw)
    ) / 1000.0


class InferenceRunner:
    """Send ``Request`` via a ``Provider`` and materialize the assistant reply."""

    name = "InferenceRunner"

    def __init__(self, provider: Any, request_factory: Any) -> None:
        # ``request_factory(state, ctx) -> Request`` — kept abstract so this
        # module doesn't couple to Request construction details.
        self._provider = provider
        self._request_factory = request_factory

    def apply(self, state: ThreadState, ctx: TurnContext) -> StepResult:
        req = self._request_factory(state, ctx)

        text_parts: list[str] = []
        tool_calls: dict[str, dict[str, Any]] = {}
        tool_call_order: list[str] = []
        usage: dict[str, int] = {}

        for event in self._provider.stream(req):
            kind = getattr(event, "kind", None)
            if kind == "text_delta" and event.text:
                text_parts.append(event.text)
            elif kind == "tool_call_delta":
                cid = event.tool_call_id or ""
                slot = tool_calls.get(cid)
                if slot is None:
                    slot = {
                        "id": cid,
                        "type": "function",
                        "function": {"name": event.tool_name or "", "arguments": ""},
                    }
                    tool_calls[cid] = slot
                    tool_call_order.append(cid)
                if event.tool_name:
                    slot["function"]["name"] = event.tool_name
                if event.arguments_delta:
                    slot["function"]["arguments"] += event.arguments_delta
            elif kind == "usage" and event.usage:
                usage = dict(event.usage)
            elif kind == "finish":
                # ``finish_reason`` is consumed by observability spans elsewhere;
                # here we just terminate the stream loop cleanly.
                pass
            elif kind == "error":
                raise RuntimeError(f"provider error: {event.error!r}")

        if not text_parts and not tool_call_order:
            return NotApplicable

        # Validate that any tool-call argument JSON parses; a malformed
        # arguments string is a provider bug we want caught here, not by
        # the tool dispatcher.
        for cid in tool_call_order:
            args_raw = tool_calls[cid]["function"]["arguments"]
            if args_raw:
                try:
                    json.loads(args_raw)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"tool_call {cid!r} has non-JSON arguments: {exc}"
                    ) from exc

        tokens_in = int(usage.get("input", 0) or usage.get("input_tokens", 0))
        tokens_out = int(usage.get("output", 0) or usage.get("output_tokens", 0))
        cache_read = int(usage.get("cache_read", 0))
        cache_write = int(usage.get("cache_write", 0))
        usd = _price(self._provider, tokens_in, tokens_out, cache_read, cache_write)

        msg = AppendMessage(
            role="assistant",
            content="".join(text_parts),
            tool_calls=tuple(tool_calls[c] for c in tool_call_order) or None,
        )
        return Applied((
            msg,
            IncrementCost(tokens_input=tokens_in, tokens_output=tokens_out, usd=usd),
        ))


__all__ = ["InferenceRunner"]
