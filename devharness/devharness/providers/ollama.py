"""Ollama local ``Provider`` implementation.

No upstream code copied — interface conforms to spec §4.3.
Hermes provides no dedicated Ollama transport at commit
``f293e7206b4ddd66042329442c6afebc19a8808d`` (it routes Ollama through the
generic chat_completions transport).

Uses the ``ollama`` Python SDK (``pip install ollama``).  ``base_url``
defaults to ``http://localhost:11434``.
"""

from __future__ import annotations

from typing import Any, Iterator, Tuple

from .base import Provider, Request, StreamEvent


class OllamaProvider(Provider):
    """Ollama local-inference provider."""

    name = "ollama"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._host = cfg.get("host") or cfg.get("base_url") or "http://localhost:11434"
        self._pricing: Tuple[float, float, float, float] = tuple(  # type: ignore[assignment]
            cfg.get("price_per_1k", (0.0, 0.0, 0.0, 0.0))
        )

    def _client(self):
        import ollama  # lazy

        return ollama.Client(host=self._host)

    def _build_kwargs(self, req: Request) -> dict[str, Any]:
        options: dict[str, Any] = {
            "temperature": req.temperature,
            "top_p": req.top_p,
            "seed": req.seed,
        }
        if req.max_tokens is not None:
            options["num_predict"] = req.max_tokens
        if req.stop:
            options["stop"] = list(req.stop)
        kwargs: dict[str, Any] = {
            "model": req.model,
            "messages": req.messages,
            "stream": True,
            "options": options,
        }
        if req.tools:
            kwargs["tools"] = req.tools
        if req.response_format is not None:
            kwargs["format"] = req.response_format
        return kwargs

    def stream(self, req: Request) -> Iterator[StreamEvent]:
        try:
            client = self._client()
            stream = client.chat(**self._build_kwargs(req))
        except Exception as exc:  # pragma: no cover
            yield StreamEvent(kind="error", error=f"{type(exc).__name__}: {exc}")
            return

        finish: str | None = None
        prompt_toks = 0
        eval_toks = 0
        try:
            for chunk in stream:
                message = chunk.get("message") if isinstance(chunk, dict) else getattr(chunk, "message", None)
                if message is not None:
                    text = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
                    if text:
                        yield StreamEvent(kind="text_delta", text=text)
                    tcs = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)
                    for tc in tcs or []:
                        fn = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", None)
                        import json as _json

                        args = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
                        yield StreamEvent(
                            kind="tool_call_delta",
                            tool_call_id=tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None),
                            tool_name=fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None),
                            arguments_delta=args if isinstance(args, str) else _json.dumps(args or {}),
                        )
                done = chunk.get("done") if isinstance(chunk, dict) else getattr(chunk, "done", False)
                if done:
                    finish = (
                        chunk.get("done_reason") if isinstance(chunk, dict) else getattr(chunk, "done_reason", None)
                    ) or "stop"
                    prompt_toks = int(
                        (chunk.get("prompt_eval_count") if isinstance(chunk, dict) else getattr(chunk, "prompt_eval_count", 0))
                        or 0
                    )
                    eval_toks = int(
                        (chunk.get("eval_count") if isinstance(chunk, dict) else getattr(chunk, "eval_count", 0)) or 0
                    )
        except Exception as exc:  # pragma: no cover
            yield StreamEvent(kind="error", error=f"{type(exc).__name__}: {exc}")
            return
        yield StreamEvent(
            kind="usage",
            usage={"input": prompt_toks, "output": eval_toks, "cache_read": 0, "cache_write": 0},
        )
        yield StreamEvent(kind="finish", finish_reason=finish or "stop")

    def supports_prompt_cache(self) -> bool:
        return False

    def supports_reasoning(self) -> bool:
        return False  # some models emit <think> blocks; not exposed as a channel

    def price_per_1k(self) -> Tuple[float, float, float, float]:
        return self._pricing


__all__ = ["OllamaProvider"]
