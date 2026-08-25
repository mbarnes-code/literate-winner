"""Provider metadata profile and runtime Protocol for LLM backends.

Ported from hermes-agent/providers/base.py
  License: MIT License
  Copyright (c) 2025 Nous Research
  Source: https://github.com/NousResearch/hermes-agent
  Upstream commit: f293e7206b4ddd66042329442c6afebc19a8808d
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable adaptations:
  - Split into two symbols per spec §4.3: ``ProviderProfile`` (declarative
    metadata, ported from hermes) and ``Provider`` (runtime Protocol with
    ``stream`` / ``supports_prompt_cache`` / ``supports_reasoning`` /
    ``price_per_1k`` — defined here, not upstream).
  - User-agent helper renamed to ``profile_user_agent`` and returns
    ``devharness/<version>`` (falls back to plain ``devharness``).
  - Dropped hermes credential-pool / OAuth device-code plumbing; ``auth_type``
    remains a free-form string so subclasses can extend it.  See TODO on
    ``fetch_models`` below for the credentialed-URL follow-up.
  - Dropped LangChain / gateway hooks; there is no equivalent surface in the
    devharness loop.
  - ``fetch_models`` still uses ``urllib.request`` but no longer calls
    hermes's ``urllib_security.open_credentialed_url``.  TODO: swap to the
    devharness credentialed-fetch helper once implemented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Protocol, Tuple, runtime_checkable

logger = logging.getLogger(__name__)

# Sentinel for "omit temperature entirely" (e.g. Kimi: server manages it).
OMIT_TEMPERATURE: Any = object()


def profile_user_agent() -> str:
    """Return a ``devharness/<version>`` UA, with a stable fallback.

    Used by ``ProviderProfile.fetch_models`` so catalog probes don't get
    served the default ``Python-urllib/<ver>`` UA — some providers sit
    behind a WAF that returns 403 for it.
    """
    try:
        from devharness import __version__ as _ver  # lazy: avoid layer cycle at import time

        return f"devharness/{_ver}"
    except Exception:
        return "devharness"


@dataclass
class ProviderProfile:
    """Declarative metadata for an inference provider.

    Profiles describe the provider's identity, auth surface, endpoint
    layout, and capability flags. They do NOT own client construction,
    credential rotation, or streaming — those live on the ``Provider``
    implementation.
    """

    # ── Identity ─────────────────────────────────────────────
    name: str
    api_mode: str = "chat_completions"  # chat_completions|responses|messages|bedrock|genai|ollama
    aliases: Tuple[str, ...] = ()

    # ── Human-readable metadata ──────────────────────────────
    display_name: str = ""
    description: str = ""
    signup_url: str = ""

    # ── Auth & endpoints ─────────────────────────────────────
    env_vars: Tuple[str, ...] = ()
    base_url: str = ""
    models_url: str = ""
    auth_type: str = "api_key"  # api_key|oauth_device_code|oauth_external|aws_sdk|none
    supports_health_check: bool = True

    # ── Capability flags ─────────────────────────────────────
    supports_vision: bool = False
    supports_vision_tool_messages: bool = True
    supports_prompt_cache_key: bool = False
    supports_prompt_cache: bool = False
    supports_reasoning: bool = False

    # ── Model catalog ────────────────────────────────────────
    fallback_models: Tuple[str, ...] = ()
    hostname: str = ""

    # ── Client-level quirks ──────────────────────────────────
    default_headers: dict[str, str] = field(default_factory=dict)

    # ── Request-level quirks ─────────────────────────────────
    fixed_temperature: Any = None
    default_max_tokens: int | None = None
    default_aux_model: str = ""

    # ── Hooks ────────────────────────────────────────────────

    def get_hostname(self) -> str:
        """Return the base hostname (explicit override or derived from base_url)."""
        if self.hostname:
            return self.hostname
        if self.base_url:
            from urllib.parse import urlparse

            return urlparse(self.base_url).hostname or ""
        return ""

    def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Provider-specific message preprocessing. Default: pass-through."""
        return messages

    def build_extra_body(
        self, *, session_id: str | None = None, **context: Any
    ) -> dict[str, Any]:
        """Provider-specific extra_body fields. Default: empty."""
        return {}

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return ``(extra_body_additions, top_level_kwargs)``. Default: empty."""
        return {}, {}

    def default_vision_model(self) -> str | None:
        """Return a default vision model id, or None."""
        return None

    def get_max_tokens(self, model: str | None) -> int | None:
        """Return the default max_tokens cap for *model*."""
        return self.default_max_tokens

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Fetch the live model list from the provider's ``/models`` endpoint.

        Resolution order for the URL:
          1. ``base_url + "/models"`` when the caller passed a base_url that
             differs from this profile's default (user proxy override).
          2. ``self.models_url`` (explicit override).
          3. ``self.base_url + "/models"`` (OpenAI-compat fallback).

        Returns a list of model ids, or ``None`` on failure / no catalog.
        Callers must fall back to ``self.fallback_models`` when ``None`` is
        returned.

        TODO: route this through the devharness credentialed-fetch helper
        (SSRF-safe URL open + secret-scrubbing) once implemented — upstream
        used ``hermes_cli.urllib_security.open_credentialed_url`` here.
        """
        caller_base = (base_url or "").strip()
        effective_base = caller_base or self.base_url
        custom_base = bool(caller_base) and (
            caller_base.rstrip("/") != (self.base_url or "").rstrip("/")
        )
        if custom_base:
            url = caller_base.rstrip("/") + "/models"
        else:
            url = (self.models_url or "").strip()
            if not url:
                if not effective_base:
                    return None
                url = effective_base.rstrip("/") + "/models"

        import json
        import urllib.request

        req = urllib.request.Request(url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", profile_user_agent())
        for k, v in self.default_headers.items():
            req.add_header(k, v)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310  # TODO: credentialed helper
                data = json.loads(resp.read().decode())
            items = data if isinstance(data, list) else data.get("data", [])
            return [m["id"] for m in items if isinstance(m, dict) and "id" in m]
        except Exception as exc:
            logger.debug("fetch_models(%s): %s", self.name, exc)
            return None


# ─────────────────────────────────────────────────────────────
# Runtime Provider Protocol (devharness spec §4.3).
#
# ``ProviderProfile`` is *metadata*; ``Provider`` is the *runtime interface*
# every concrete backend (anthropic, openai, bedrock, ...) implements.  The
# loop's InferenceRunner takes a ``Provider`` and never reaches into a
# profile directly — profile access goes through the capability methods so
# a provider is free to override them (e.g. per-model prompt-cache support).
# ─────────────────────────────────────────────────────────────


@runtime_checkable
class Provider(Protocol):
    """Runtime interface for an LLM inference provider.

    Concrete implementations live in ``devharness/providers/{name}.py`` and
    are registered in ``devharness/providers/__init__.py``.
    """

    name: str

    def stream(self, req: "Request") -> Iterator["StreamEvent"]:  # noqa: F821  # forward refs
        """Send *req* and yield streaming events until completion.

        Must be deterministic given identical ``Request`` (subject to
        provider guarantees) — the loop pins ``temperature``, ``top_p``,
        and ``seed`` via ``Request``.
        """
        ...

    def supports_prompt_cache(self) -> bool:
        """Return True when this provider honours prompt-cache directives."""
        ...

    def supports_reasoning(self) -> bool:
        """Return True when this provider exposes a reasoning/thinking channel."""
        ...

    def price_per_1k(self) -> Tuple[float, float, float, float]:
        """Return ``(input, output, cache_read, cache_write)`` USD per 1K tokens."""
        ...


# ─────────────────────────────────────────────────────────────
# Wire types for Provider.stream() — devharness spec §4.3.
# Kept alongside the Protocol so provider modules import a single symbol
# set from ``providers.base``.  ``Request`` is the loop → provider payload;
# ``StreamEvent`` is the tagged-union yield type.
# ─────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Request:
    """Structured payload passed to ``Provider.stream``.

    Determinism-critical fields (``temperature``, ``top_p``, ``seed``) are
    populated by the loop and must NOT be silently rewritten by providers;
    a provider that cannot honour them raises rather than downgrading.
    """

    messages: list[dict]
    model: str
    temperature: float = 0
    top_p: float = 1
    seed: int = 42
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None
    cache_control: dict | None = None
    response_format: dict | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None


@dataclass(slots=True, frozen=True)
class StreamEvent:
    """Tagged-union event yielded by ``Provider.stream``.

    Only the fields relevant to ``kind`` are populated; the rest are
    ``None``.  Consumers dispatch on ``kind``:

    - ``text_delta``      → ``text``
    - ``tool_call_delta`` → ``tool_call_id``, ``tool_name``, ``arguments_delta``
    - ``usage``           → ``usage``  (``{input,output,cache_read,cache_write}``)
    - ``finish``          → ``finish_reason``
    - ``error``           → ``error``
    """

    kind: Literal["text_delta", "tool_call_delta", "usage", "finish", "error"]
    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str | None = None
    usage: dict | None = None
    finish_reason: str | None = None
    error: str | None = None


__all__ = [
    "OMIT_TEMPERATURE",
    "Provider",
    "ProviderProfile",
    "Request",
    "StreamEvent",
    "profile_user_agent",
]
