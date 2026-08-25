"""AWS Bedrock Converse API ``Provider`` implementation.

No upstream code copied — interface conforms to spec §4.3.
Hermes' ``agent/transports/bedrock.py`` (MIT, © 2025 Nous Research, commit
``f293e7206b4ddd66042329442c6afebc19a8808d``) was consulted for the Converse
message shape (``role`` + list of content blocks with typed keys); the
upstream file is a ``ProviderTransport`` that delegates to a separate
adapter, not a ``Provider``.

Auth model: uses ``boto3`` credentials (env / IMDS / shared config).  Config
accepts ``region_name`` and an optional pre-built ``client`` for tests.
"""

from __future__ import annotations

from typing import Any, Iterator, Tuple

from .base import Provider, Request, StreamEvent


def _openai_msgs_to_converse(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split ``messages`` into ``(system, converse_messages)``.

    Bedrock Converse expects ``system`` as a top-level list of
    ``{"text": ...}`` blocks and ``messages`` items with ``content`` as a list
    of typed blocks (``{"text": ...}`` for now).  Tool-use blocks pass through
    when the caller has already shaped them.
    """
    system: list[dict] = []
    conv: list[dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            if isinstance(content, str):
                system.append({"text": content})
            elif isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and "text" in b:
                        system.append({"text": b["text"]})
            continue
        blocks: list[dict]
        if isinstance(content, str):
            blocks = [{"text": content}]
        elif isinstance(content, list):
            blocks = [b if isinstance(b, dict) else {"text": str(b)} for b in content]
        else:
            blocks = [{"text": ""}]
        conv.append({"role": "assistant" if role == "assistant" else "user", "content": blocks})
    return system, conv


class BedrockProvider(Provider):
    """AWS Bedrock Converse API provider."""

    name = "bedrock"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._region = cfg.get("region_name") or cfg.get("region") or "us-east-1"
        self._client_override = cfg.get("client")
        self._pricing: Tuple[float, float, float, float] = tuple(  # type: ignore[assignment]
            cfg.get("price_per_1k", (0.0, 0.0, 0.0, 0.0))
        )

    def _client(self):
        if self._client_override is not None:
            return self._client_override
        import boto3  # lazy — SDK optional at import time

        return boto3.client("bedrock-runtime", region_name=self._region)

    def _build_kwargs(self, req: Request) -> dict[str, Any]:
        system, conv = _openai_msgs_to_converse(req.messages)
        inference: dict[str, Any] = {"temperature": req.temperature, "topP": req.top_p}
        if req.max_tokens is not None:
            inference["maxTokens"] = req.max_tokens
        if req.stop:
            inference["stopSequences"] = list(req.stop)
        kwargs: dict[str, Any] = {
            "modelId": req.model,
            "messages": conv,
            "inferenceConfig": inference,
        }
        if system:
            kwargs["system"] = system
        if req.tools:
            kwargs["toolConfig"] = {"tools": req.tools}
        return kwargs

    def stream(self, req: Request) -> Iterator[StreamEvent]:
        try:
            client = self._client()
            resp = client.converse_stream(**self._build_kwargs(req))
        except Exception as exc:  # pragma: no cover
            yield StreamEvent(kind="error", error=f"{type(exc).__name__}: {exc}")
            return

        finish: str | None = None
        active_tool: dict[int, dict[str, Any]] = {}
        try:
            for event in resp.get("stream", []):
                if "contentBlockStart" in event:
                    start = event["contentBlockStart"]
                    tu = start.get("start", {}).get("toolUse")
                    if tu:
                        active_tool[start.get("contentBlockIndex", 0)] = tu
                elif "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"]
                    idx = delta.get("contentBlockIndex", 0)
                    d = delta.get("delta", {})
                    if "text" in d and d["text"]:
                        yield StreamEvent(kind="text_delta", text=d["text"])
                    elif "toolUse" in d:
                        tu = active_tool.get(idx, {})
                        yield StreamEvent(
                            kind="tool_call_delta",
                            tool_call_id=tu.get("toolUseId"),
                            tool_name=tu.get("name"),
                            arguments_delta=d["toolUse"].get("input", ""),
                        )
                elif "messageStop" in event:
                    finish = event["messageStop"].get("stopReason") or "stop"
                elif "metadata" in event:
                    usage = event["metadata"].get("usage") or {}
                    yield StreamEvent(
                        kind="usage",
                        usage={
                            "input": int(usage.get("inputTokens") or 0),
                            "output": int(usage.get("outputTokens") or 0),
                            "cache_read": int(usage.get("cacheReadInputTokens") or 0),
                            "cache_write": int(usage.get("cacheWriteInputTokens") or 0),
                        },
                    )
        except Exception as exc:  # pragma: no cover
            yield StreamEvent(kind="error", error=f"{type(exc).__name__}: {exc}")
            return
        yield StreamEvent(kind="finish", finish_reason=finish or "stop")

    def supports_prompt_cache(self) -> bool:
        return True  # Bedrock exposes cache_read/cache_write on supported models

    def supports_reasoning(self) -> bool:
        return True

    def price_per_1k(self) -> Tuple[float, float, float, float]:
        return self._pricing


__all__ = ["BedrockProvider"]
