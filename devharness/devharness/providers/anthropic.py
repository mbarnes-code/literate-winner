"""Anthropic Messages API ``Provider`` with prompt-cache marker placement.

Ported / adapted from two upstreams:

1. ``vvaharness/backends/sdk.py``
   License:  Apache License 2.0
   Copyright 2026 Visa, Inc.
   Source:   https://github.com/visa/visa-vulnerability-agentic-harness
   Upstream commit: 3d972f679d8f5e3838b394edee0b5ea9c626b0fb
   Lift:    ``_with_cache_marker`` — strip every ``cache_control`` marker
            and drop ONE ``{"type":"ephemeral"}`` onto the last dict content
            block, keeping the Anthropic 4-block cap satisfied regardless of
            turn count.  Reimplemented as ``_apply_ephemeral_marker`` here;
            the algorithmic shape is copied.

2. Provider protocol / streaming contract:
   No upstream code copied — interface conforms to devharness spec §4.3.
   Hermes' ``agent/transports/anthropic.py`` (MIT, © 2025 Nous Research,
   commit ``f293e7206b4ddd66042329442c6afebc19a8808d``) was consulted for
   the (system, messages) split and tool-use block shape but is not copied
   (it is a ``ProviderTransport`` that delegates to a separate adapter, not
   a ``Provider``).

Notable adaptations:
  - No credential pool / TLS knobs from VVAH's ``_get_client`` — the sandbox
    layer owns egress policy in devharness.  Config accepts ``api_key`` /
    ``base_url`` only.
  - System-prompt extraction is peeled off the messages list rather than
    passed via a separate arg (spec §4.3 sends a single Request object).
  - Streaming yields devharness :class:`StreamEvent` shape; VVAH's
    ``stream.get_final_message()`` blocking assemble is replaced with a
    streaming loop over ``client.messages.stream(**kw)`` events.
"""

from __future__ import annotations

import copy
from typing import Any, Iterator, Tuple

from .base import Provider, Request, StreamEvent


def _extract_system(messages: list[dict]) -> tuple[Any, list[dict]]:
    """Return ``(system_field, remaining_messages)``.

    Anthropic's Messages API accepts a top-level ``system`` field (string or
    list of blocks), separate from the ``messages`` list.  A leading
    ``role="system"`` message in the input is peeled off; anything else is
    passed through unchanged.
    """
    if not messages or messages[0].get("role") != "system":
        return None, list(messages)
    head, *rest = messages
    content = head.get("content")
    if isinstance(content, str):
        return content, list(rest)
    return content, list(rest)


def _apply_ephemeral_marker(messages: list[dict]) -> list[dict]:
    """Strip every ``cache_control`` marker and place ONE on the last block.

    Ported from ``vvaharness/backends/sdk.py::_with_cache_marker`` (Apache 2.0,
    © 2026 Visa, Inc.).  The stored history never carries markers, so the
    request cannot exceed Anthropic's 4-block ``cache_control`` cap regardless
    of turn count or block-type diversity.
    """
    out: list[dict] = []
    last_block: dict | None = None
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            new_content: list[Any] = []
            for block in content:
                if isinstance(block, dict):
                    block = {k: v for k, v in block.items() if k != "cache_control"}
                    last_block = block
                new_content.append(block)
            out.append({**m, "content": new_content})
        else:
            out.append(dict(m))
    if last_block is not None:
        last_block["cache_control"] = {"type": "ephemeral"}
    return out


class AnthropicProvider(Provider):
    """Anthropic Messages API provider with automatic prompt-cache marker."""

    name = "anthropic"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._api_key = cfg.get("api_key")
        self._base_url = cfg.get("base_url")
        self._timeout = cfg.get("timeout", 600.0)
        self._max_retries = int(cfg.get("max_retries", 4))
        self._pricing: Tuple[float, float, float, float] = tuple(  # type: ignore[assignment]
            cfg.get("price_per_1k", (0.0, 0.0, 0.0, 0.0))
        )

    def _client(self):
        import anthropic  # lazy

        kwargs: dict[str, Any] = {"max_retries": self._max_retries, "timeout": self._timeout}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return anthropic.Anthropic(**kwargs)

    def _build_kwargs(self, req: Request) -> dict[str, Any]:
        system, conv = _extract_system(req.messages)
        conv = _apply_ephemeral_marker(conv)
        kwargs: dict[str, Any] = {
            "model": req.model,
            "messages": conv,
            "max_tokens": req.max_tokens or 4096,
            "temperature": req.temperature,
            "top_p": req.top_p,
        }
        if system is not None:
            # Mark the system block ephemeral too — same rationale as
            # vvaharness/backends/sdk.py::prompt() (Apache-2.0): the system
            # prompt is stable across turns so it earns a cache-read bucket.
            if isinstance(system, str):
                kwargs["system"] = [
                    {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
                ]
            else:
                blocks = [copy.deepcopy(b) for b in system]
                if blocks and isinstance(blocks[-1], dict):
                    blocks[-1]["cache_control"] = {"type": "ephemeral"}
                kwargs["system"] = blocks
        if req.tools:
            kwargs["tools"] = req.tools
        if req.tool_choice is not None:
            kwargs["tool_choice"] = (
                req.tool_choice if isinstance(req.tool_choice, dict) else {"type": req.tool_choice}
            )
        if req.stop:
            kwargs["stop_sequences"] = req.stop
        return kwargs

    def stream(self, req: Request) -> Iterator[StreamEvent]:
        try:
            client = self._client()
            kwargs = self._build_kwargs(req)
        except Exception as exc:  # pragma: no cover
            yield StreamEvent(kind="error", error=f"{type(exc).__name__}: {exc}")
            return

        finish: str | None = None
        tool_partials: dict[int, tuple[str | None, str | None]] = {}
        try:
            with client.messages.stream(**kwargs) as stream:
                for event in stream:
                    etype = getattr(event, "type", "")
                    if etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        if block is not None and getattr(block, "type", "") == "tool_use":
                            idx = int(getattr(event, "index", 0) or 0)
                            tool_partials[idx] = (
                                getattr(block, "id", None),
                                getattr(block, "name", None),
                            )
                    elif etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        dtype = getattr(delta, "type", "") if delta is not None else ""
                        if dtype == "text_delta":
                            text = getattr(delta, "text", "") or ""
                            if text:
                                yield StreamEvent(kind="text_delta", text=text)
                        elif dtype == "input_json_delta":
                            idx = int(getattr(event, "index", 0) or 0)
                            call_id, call_name = tool_partials.get(idx, (None, None))
                            yield StreamEvent(
                                kind="tool_call_delta",
                                tool_call_id=call_id,
                                tool_name=call_name,
                                arguments_delta=getattr(delta, "partial_json", "") or "",
                            )
                    elif etype == "message_delta":
                        delta = getattr(event, "delta", None)
                        if delta is not None:
                            sr = getattr(delta, "stop_reason", None)
                            if sr:
                                finish = sr
                final = stream.get_final_message()
                usage = getattr(final, "usage", None)
                if usage is not None:
                    dump = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
                    yield StreamEvent(
                        kind="usage",
                        usage={
                            "input": int(dump.get("input_tokens") or 0),
                            "output": int(dump.get("output_tokens") or 0),
                            "cache_read": int(dump.get("cache_read_input_tokens") or 0),
                            "cache_write": int(dump.get("cache_creation_input_tokens") or 0),
                        },
                    )
        except Exception as exc:  # pragma: no cover
            yield StreamEvent(kind="error", error=f"{type(exc).__name__}: {exc}")
            return
        yield StreamEvent(kind="finish", finish_reason=finish or "stop")

    def supports_prompt_cache(self) -> bool:
        return True

    def supports_reasoning(self) -> bool:
        return True

    def price_per_1k(self) -> Tuple[float, float, float, float]:
        return self._pricing


__all__ = ["AnthropicProvider", "_apply_ephemeral_marker"]
