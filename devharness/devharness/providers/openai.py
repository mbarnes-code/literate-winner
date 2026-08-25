"""OpenAI Chat Completions ``Provider`` implementation.

No upstream code copied — interface conforms to spec §4.3.
Format handling was cross-checked against
``hermes-agent/agent/transports/chat_completions.py`` (MIT, © 2025 Nous
Research, commit ``f293e7206b4ddd66042329442c6afebc19a8808d``) for the
tool-call streaming shape; that transport is a ``ProviderTransport`` (format
conversion only), not a ``Provider``, so its streaming loop is not copied.

Notable design choices:
  - Lazy import of the ``openai`` SDK inside ``stream`` so importing this
    module never requires the SDK to be installed.
  - Determinism-critical fields (``temperature``, ``top_p``, ``seed``) are
    passed straight through from :class:`~devharness.providers.base.Request`.
  - Prompt caching on OpenAI is server-managed; :meth:`supports_prompt_cache`
    returns True but ``cache_control`` is a no-op (routed via
    ``prompt_cache.attach``, which is a no-op for OpenAI).
"""

from __future__ import annotations

from typing import Any, Iterator, Tuple

from .base import Provider, Request, StreamEvent


class OpenAIProvider(Provider):
    """Chat Completions provider (OpenAI SDK)."""

    name = "openai"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._api_key = cfg.get("api_key")
        self._base_url = cfg.get("base_url")
        self._organization = cfg.get("organization")
        self._timeout = cfg.get("timeout", 600.0)
        self._pricing: Tuple[float, float, float, float] = tuple(  # type: ignore[assignment]
            cfg.get("price_per_1k", (0.0, 0.0, 0.0, 0.0))
        )

    def _client(self):
        from openai import OpenAI  # lazy — SDK optional at import time

        kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._base_url:
            kwargs["base_url"] = self._base_url
        if self._organization:
            kwargs["organization"] = self._organization
        return OpenAI(**kwargs)

    def _build_kwargs(self, req: Request) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": req.model,
            "messages": req.messages,
            "temperature": req.temperature,
            "top_p": req.top_p,
            "seed": req.seed,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if req.tools:
            kwargs["tools"] = req.tools
        if req.tool_choice is not None:
            kwargs["tool_choice"] = req.tool_choice
        if req.response_format is not None:
            kwargs["response_format"] = req.response_format
        if req.max_tokens is not None:
            kwargs["max_tokens"] = req.max_tokens
        if req.stop:
            kwargs["stop"] = req.stop
        return kwargs

    def stream(self, req: Request) -> Iterator[StreamEvent]:
        try:
            client = self._client()
            stream = client.chat.completions.create(**self._build_kwargs(req))
        except Exception as exc:  # pragma: no cover — exercised by fallback tests
            yield StreamEvent(kind="error", error=f"{type(exc).__name__}: {exc}")
            return

        finish: str | None = None
        try:
            for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                if choices:
                    choice = choices[0]
                    delta = getattr(choice, "delta", None)
                    if delta is not None:
                        text = getattr(delta, "content", None)
                        if text:
                            yield StreamEvent(kind="text_delta", text=text)
                        for tc in getattr(delta, "tool_calls", None) or []:
                            fn = getattr(tc, "function", None)
                            yield StreamEvent(
                                kind="tool_call_delta",
                                tool_call_id=getattr(tc, "id", None),
                                tool_name=getattr(fn, "name", None) if fn else None,
                                arguments_delta=getattr(fn, "arguments", None) if fn else None,
                            )
                    fr = getattr(choice, "finish_reason", None)
                    if fr:
                        finish = fr
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    yield StreamEvent(
                        kind="usage",
                        usage={
                            "input": getattr(usage, "prompt_tokens", 0) or 0,
                            "output": getattr(usage, "completion_tokens", 0) or 0,
                            "cache_read": (
                                getattr(
                                    getattr(usage, "prompt_tokens_details", None),
                                    "cached_tokens",
                                    0,
                                )
                                or 0
                            ),
                            "cache_write": 0,
                        },
                    )
        except Exception as exc:  # pragma: no cover
            yield StreamEvent(kind="error", error=f"{type(exc).__name__}: {exc}")
            return
        yield StreamEvent(kind="finish", finish_reason=finish or "stop")

    def supports_prompt_cache(self) -> bool:
        return True  # server-managed automatic prompt caching

    def supports_reasoning(self) -> bool:
        return False

    def price_per_1k(self) -> Tuple[float, float, float, float]:
        return self._pricing


__all__ = ["OpenAIProvider"]
