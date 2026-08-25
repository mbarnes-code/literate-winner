"""Provider registry — factory for built-in :class:`Provider` implementations.

Spec §4.3.  ``get_provider(name, config)`` returns a configured provider
instance for the requested backend.  Providers self-register their SDKs
lazily so importing this module never requires optional SDKs to be installed.
"""

from __future__ import annotations

from typing import Any, Callable

from .anthropic import AnthropicProvider
from .base import OMIT_TEMPERATURE, Provider, ProviderProfile, Request, StreamEvent, profile_user_agent
from .bedrock import BedrockProvider
from .fallback import FallbackProvider
from .google_genai import GoogleGenAIProvider
from .ollama import OllamaProvider
from .openai import OpenAIProvider
from .openai_responses import OpenAIResponsesProvider

_BUILTIN: dict[str, Callable[[dict[str, Any]], Provider]] = {
    "anthropic": lambda cfg: AnthropicProvider(cfg),
    "openai": lambda cfg: OpenAIProvider(cfg),
    "openai_responses": lambda cfg: OpenAIResponsesProvider(cfg),
    "bedrock": lambda cfg: BedrockProvider(cfg),
    "google_genai": lambda cfg: GoogleGenAIProvider(cfg),
    "ollama": lambda cfg: OllamaProvider(cfg),
}


def get_provider(name: str, config: dict[str, Any] | None = None) -> Provider:
    """Return a configured provider for *name*.

    Raises ``KeyError`` when *name* is not a known built-in.  The
    ``fallback`` provider is composite and must be constructed directly via
    :class:`FallbackProvider` — it is intentionally NOT in the registry so
    the caller has to name each hop.
    """
    try:
        factory = _BUILTIN[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown provider {name!r}; registered: {sorted(_BUILTIN)}"
        ) from exc
    return factory(config or {})


def registered_providers() -> list[str]:
    """Return the sorted list of registered built-in provider names."""
    return sorted(_BUILTIN)


__all__ = [
    "OMIT_TEMPERATURE",
    "AnthropicProvider",
    "BedrockProvider",
    "FallbackProvider",
    "GoogleGenAIProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "OpenAIResponsesProvider",
    "Provider",
    "ProviderProfile",
    "Request",
    "StreamEvent",
    "get_provider",
    "profile_user_agent",
    "registered_providers",
]
