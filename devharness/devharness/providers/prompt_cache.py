"""Per-provider ``cache_control`` block attachment helpers.

Design lift (no code copied):
  Source:   reference/deepagents/libs/deepagents/deepagents/middleware/_prompt_caching.py
  License:  MIT License
  Copyright (c) LangChain, Inc.
  Upstream: https://github.com/langchain-ai/deepagents
  Upstream commit: 23b83ad50f63d241d0069a3dc426d43b211adf2e

DeepAgents composes a stack of provider-specific LangChain middlewares
(``AnthropicPromptCachingMiddleware``, ``BedrockPromptCachingMiddleware``,
``FireworksPromptCachingMiddleware``) with ``unsupported_model_behavior=ignore``.
We keep the same organizing pattern — one attach-hook per provider family,
soft-failing on providers that don't support caching — but reimplement without
LangChain since :class:`~devharness.providers.base.Request` is a plain dataclass
in devharness.

Semantics:
  ``attach(provider_name, request)`` returns a NEW request with any provider-
  specific ``cache_control`` markers threaded through.  Callers that never
  need caching don't have to call this at all; the loop invokes it once per
  turn just before ``Provider.stream``.

Providers that manage caching server-side (OpenAI, Google Gemini) receive
only a ``prompt_cache_key`` propagation.  Providers that manage caching via
in-body markers (Anthropic, Bedrock) get the marker walk delegated to their
own module (:mod:`~devharness.providers.anthropic`), which owns the
authoritative Anthropic 4-block cap logic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any, Callable

from .base import Request

_HANDLERS: dict[str, Callable[[Request], Request]] = {}


def register(provider_name: str) -> Callable[[Callable[[Request], Request]], Callable[[Request], Request]]:
    """Decorator: register *fn* as the cache-attach handler for *provider_name*."""

    def _deco(fn: Callable[[Request], Request]) -> Callable[[Request], Request]:
        _HANDLERS[provider_name] = fn
        return fn

    return _deco


def stable_cache_key(system_prompt: str, tools: list[dict] | None) -> str:
    """Return a content-addressed 16-char key for prompt-cache routing.

    Mirrors the Chat-Completions-style ``prompt_cache_key`` convention — hash
    over the stable prefix (system prompt + tool schemas) so equivalent
    prefixes route to the same cache bucket without leaking session identity.
    """
    payload = json.dumps(
        {"system": system_prompt, "tools": tools or []},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def attach(provider_name: str, request: Request) -> Request:
    """Return *request* with provider-appropriate cache markers attached.

    Unsupported provider → returns *request* unchanged (design-lifted from
    DeepAgents' ``unsupported_model_behavior=ignore`` posture).
    """
    handler = _HANDLERS.get(provider_name)
    if handler is None:
        return request
    return handler(request)


@register("anthropic")
def _attach_anthropic(request: Request) -> Request:
    """Anthropic in-body markers.

    Actual walker lives in :func:`devharness.providers.anthropic._apply_ephemeral_marker`
    to keep the 4-block cap logic single-sourced.  We flag ``cache_control`` on
    the request so the provider's ``_build_kwargs`` knows to apply the walk;
    the current AnthropicProvider walks unconditionally, so this is a no-op
    that documents intent.
    """
    marker = {"type": "ephemeral"}
    if request.cache_control == marker:
        return request
    return replace(request, cache_control=marker)


@register("bedrock")
def _attach_bedrock(request: Request) -> Request:
    """Bedrock Converse expects ``{"cachePoint": {"type": "default"}}`` blocks.

    We surface intent via ``cache_control``; the Bedrock provider is
    responsible for translating that into Converse's cachePoint shape when
    building its kwargs.
    """
    marker = {"type": "default", "wire": "bedrock_cache_point"}
    if request.cache_control == marker:
        return request
    return replace(request, cache_control=marker)


@register("openai")
def _attach_openai(request: Request) -> Request:
    """OpenAI Chat Completions is server-managed; only propagate the key."""
    if not request.cache_control:
        return request
    return request  # nothing to add — key already on request


@register("openai_responses")
def _attach_openai_responses(request: Request) -> Request:
    """OpenAI Responses honours ``prompt_cache_key``; upgrade if bare marker."""
    cc = request.cache_control or {}
    if cc.get("key"):
        return request
    key = stable_cache_key(_extract_system(request.messages), request.tools)
    return replace(request, cache_control={"key": key, **cc})


@register("google_genai")
def _attach_google_genai(request: Request) -> Request:
    """Gemini ``cachedContent`` is per-request server object; propagate name."""
    return request  # caller supplies cached_content id in cache_control if any


def _extract_system(messages: list[dict]) -> str:
    if not messages or messages[0].get("role") not in {"system", "developer"}:
        return ""
    content = messages[0].get("content")
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content or "")


__all__ = ["attach", "register", "stable_cache_key"]
