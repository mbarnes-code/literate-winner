"""OpenAI Responses API ``Provider`` implementation.

No upstream code copied — interface conforms to spec §4.3.
Hermes' ``agent/transports/chat_completions.py`` (MIT, © 2025 Nous Research,
commit ``f293e7206b4ddd66042329442c6afebc19a8808d``) was consulted for the
Responses-vs-Chat-Completions field split (``input`` instead of ``messages``,
top-level ``instructions``, structured reasoning), but no code is copied — the
upstream file is a ``ProviderTransport`` (format conversion only), not a
``Provider``.

The Responses API preserves reasoning traces across turns, so
:meth:`supports_reasoning` returns True.  Prompt-cache directives are managed
server-side and honoured via a stable ``prompt_cache_key`` when the caller
supplies one in ``Request.cache_control``.
"""

from __future__ import annotations

from typing import Any, Iterator, Tuple

from .base import Provider, Request, StreamEvent


def _split_instructions(messages: list[dict]) -> tuple[str, list[dict]]:
    """Peel a leading system/developer message off, returning ``(instructions, rest)``.

    The Responses API carries system-prompt content in a top-level
    ``instructions`` field rather than as a message, so a caller who passed a
    Chat-Completions-shaped payload still routes correctly.
    """
    if not messages or messages[0].get("role") not in {"system", "developer"}:
        return "", list(messages)
    head, *rest = messages
    content = head.get("content")
    if isinstance(content, str):
        return content, list(rest)
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") in {"text", "input_text"}:
            parts.append(str(block.get("text", "")))
    return "\n".join(parts), list(rest)


class OpenAIResponsesProvider(Provider):
    """OpenAI Responses API provider."""

    name = "openai_responses"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._api_key = cfg.get("api_key")
        self._base_url = cfg.get("base_url")
        self._timeout = cfg.get("timeout", 600.0)
        self._pricing: Tuple[float, float, float, float] = tuple(  # type: ignore[assignment]
            cfg.get("price_per_1k", (0.0, 0.0, 0.0, 0.0))
        )

    def _client(self):
        from openai import OpenAI  # lazy

        kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return OpenAI(**kwargs)

    def _build_kwargs(self, req: Request) -> dict[str, Any]:
        instructions, conv = _split_instructions(req.messages)
        kwargs: dict[str, Any] = {
            "model": req.model,
            "input": conv,
            "temperature": req.temperature,
            "top_p": req.top_p,
            "stream": True,
        }
        if instructions:
            kwargs["instructions"] = instructions
        if req.tools:
            kwargs["tools"] = req.tools
        if req.tool_choice is not None:
            kwargs["tool_choice"] = req.tool_choice
        if req.max_tokens is not None:
            kwargs["max_output_tokens"] = req.max_tokens
        if req.response_format is not None:
            kwargs["response_format"] = req.response_format
        if isinstance(req.cache_control, dict) and req.cache_control.get("key"):
            kwargs["prompt_cache_key"] = req.cache_control["key"]
        return kwargs

    def stream(self, req: Request) -> Iterator[StreamEvent]:
        try:
            client = self._client()
            stream = client.responses.create(**self._build_kwargs(req))
        except Exception as exc:  # pragma: no cover
            yield StreamEvent(kind="error", error=f"{type(exc).__name__}: {exc}")
            return

        finish: str | None = None
        try:
            for event in stream:
                etype = getattr(event, "type", "")
                if etype == "response.output_text.delta":
                    delta = getattr(event, "delta", "") or ""
                    if delta:
                        yield StreamEvent(kind="text_delta", text=delta)
                elif etype == "response.function_call_arguments.delta":
                    yield StreamEvent(
                        kind="tool_call_delta",
                        tool_call_id=getattr(event, "item_id", None),
                        tool_name=getattr(event, "name", None),
                        arguments_delta=getattr(event, "delta", None),
                    )
                elif etype == "response.completed":
                    resp = getattr(event, "response", None)
                    usage = getattr(resp, "usage", None) if resp else None
                    if usage is not None:
                        yield StreamEvent(
                            kind="usage",
                            usage={
                                "input": getattr(usage, "input_tokens", 0) or 0,
                                "output": getattr(usage, "output_tokens", 0) or 0,
                                "cache_read": (
                                    getattr(
                                        getattr(usage, "input_tokens_details", None),
                                        "cached_tokens",
                                        0,
                                    )
                                    or 0
                                ),
                                "cache_write": 0,
                            },
                        )
                    finish = "stop"
                elif etype == "response.error":
                    err = getattr(event, "error", None)
                    yield StreamEvent(kind="error", error=str(err) if err else "response.error")
                    return
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


__all__ = ["OpenAIResponsesProvider"]
