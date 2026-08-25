"""Google GenAI (Gemini) ``Provider`` implementation.

No upstream code copied — interface conforms to spec §4.3.
Hermes' ``agent/transports/chat_completions.py`` (MIT, © 2025 Nous Research,
commit ``f293e7206b4ddd66042329442c6afebc19a8808d``) was consulted for the
Gemini ``thinkingConfig`` translation but is not copied.  Hermes does not
ship a dedicated ``google_genai`` transport at that commit.

Uses the ``google-genai`` SDK.  Determinism is best-effort: the SDK exposes
``seed`` only in ``generation_config``; some model versions ignore it.
"""

from __future__ import annotations

from typing import Any, Iterator, Tuple

from .base import Provider, Request, StreamEvent


def _openai_msgs_to_genai(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """Return ``(system_instruction, contents)``.

    Gemini has a top-level ``system_instruction`` and ``contents`` where each
    item is ``{"role": "user"|"model", "parts": [{"text": ...}]}``.  Any
    leading ``system`` message is peeled off.
    """
    system: str | None = None
    if messages and messages[0].get("role") == "system":
        head, *rest = messages
        c = head.get("content")
        system = c if isinstance(c, str) else "\n".join(
            b.get("text", "") for b in (c or []) if isinstance(b, dict)
        )
        messages = rest
    contents: list[dict] = []
    for m in messages:
        role = "model" if m.get("role") == "assistant" else "user"
        c = m.get("content")
        parts: list[dict]
        if isinstance(c, str):
            parts = [{"text": c}]
        elif isinstance(c, list):
            parts = []
            for b in c:
                if isinstance(b, dict) and "text" in b:
                    parts.append({"text": b["text"]})
        else:
            parts = [{"text": ""}]
        contents.append({"role": role, "parts": parts})
    return system, contents


class GoogleGenAIProvider(Provider):
    """Google GenAI (Gemini) provider."""

    name = "google_genai"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._api_key = cfg.get("api_key")
        self._pricing: Tuple[float, float, float, float] = tuple(  # type: ignore[assignment]
            cfg.get("price_per_1k", (0.0, 0.0, 0.0, 0.0))
        )

    def _client(self):
        from google import genai  # lazy

        return genai.Client(api_key=self._api_key) if self._api_key else genai.Client()

    def _build_kwargs(self, req: Request) -> dict[str, Any]:
        system, contents = _openai_msgs_to_genai(req.messages)
        gen_cfg: dict[str, Any] = {
            "temperature": req.temperature,
            "top_p": req.top_p,
            "seed": req.seed,
        }
        if req.max_tokens is not None:
            gen_cfg["max_output_tokens"] = req.max_tokens
        if req.stop:
            gen_cfg["stop_sequences"] = list(req.stop)
        if system:
            gen_cfg["system_instruction"] = system
        if req.tools:
            gen_cfg["tools"] = req.tools
        return {"model": req.model, "contents": contents, "config": gen_cfg}

    def stream(self, req: Request) -> Iterator[StreamEvent]:
        try:
            client = self._client()
            stream = client.models.generate_content_stream(**self._build_kwargs(req))
        except Exception as exc:  # pragma: no cover
            yield StreamEvent(kind="error", error=f"{type(exc).__name__}: {exc}")
            return

        finish: str | None = None
        try:
            for chunk in stream:
                text = getattr(chunk, "text", None)
                if text:
                    yield StreamEvent(kind="text_delta", text=text)
                for cand in getattr(chunk, "candidates", None) or []:
                    parts = getattr(getattr(cand, "content", None), "parts", None) or []
                    for part in parts:
                        fc = getattr(part, "function_call", None)
                        if fc is not None:
                            import json as _json

                            yield StreamEvent(
                                kind="tool_call_delta",
                                tool_call_id=getattr(fc, "id", None) or getattr(fc, "name", None),
                                tool_name=getattr(fc, "name", None),
                                arguments_delta=_json.dumps(dict(getattr(fc, "args", {}) or {})),
                            )
                    fr = getattr(cand, "finish_reason", None)
                    if fr:
                        finish = str(fr)
                usage = getattr(chunk, "usage_metadata", None)
                if usage is not None:
                    yield StreamEvent(
                        kind="usage",
                        usage={
                            "input": int(getattr(usage, "prompt_token_count", 0) or 0),
                            "output": int(getattr(usage, "candidates_token_count", 0) or 0),
                            "cache_read": int(getattr(usage, "cached_content_token_count", 0) or 0),
                            "cache_write": 0,
                        },
                    )
        except Exception as exc:  # pragma: no cover
            yield StreamEvent(kind="error", error=f"{type(exc).__name__}: {exc}")
            return
        yield StreamEvent(kind="finish", finish_reason=finish or "stop")

    def supports_prompt_cache(self) -> bool:
        return True  # Gemini's cachedContent feature

    def supports_reasoning(self) -> bool:
        return True

    def price_per_1k(self) -> Tuple[float, float, float, float]:
        return self._pricing


__all__ = ["GoogleGenAIProvider"]
