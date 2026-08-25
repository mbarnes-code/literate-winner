"""Pydantic Settings schema for devharness configuration.

No upstream code copied — this is the spec-driven schema for the
merged config dict produced by ``devharness/config/__init__.py``.
Every knob in docs/devharness-spec.md §9.2 has a typed model here so
the loader can validate the resolved dict before session start.

The base class is ``pydantic.BaseModel``: at the time of writing
``pyproject.toml`` only lists ``pydantic`` as a target dependency (the
spec's ``pydantic-settings`` is not yet declared, so we do not depend
on it here). Env-var expansion is handled upstream by
``config._expand_env`` before the dict lands in this schema.
"""
from __future__ import annotations

from typing import Any, Literal

try:  # pragma: no cover - trivial import guard
    from pydantic import BaseModel, Field  # type: ignore[import-not-found]
except ImportError as _e:  # deferred hard failure
    _IMPORT_ERR = _e

    class BaseModel:  # type: ignore[no-redef]
        """Fallback stub raised on first instantiation when pydantic
        is not installed. Lets ``python -m compileall`` succeed."""

        def __init__(self, *_: Any, **__: Any) -> None:
            raise ImportError(
                "pydantic is required for devharness.config.schema"
            ) from _IMPORT_ERR

    def Field(*_: Any, **__: Any) -> Any:  # type: ignore[no-redef]
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Sub-models (one per spec §9.2 section)
# ─────────────────────────────────────────────────────────────────────────────


class ProviderSettings(BaseModel):
    """One provider/model binding (matches ``[model.*]`` table entries)."""

    provider: str
    model: str
    temperature: float = 0.0
    top_p: float = 1.0
    seed: int = 42
    max_tokens: int = 8192


class CompactionSettings(BaseModel):
    """§4.8 / §9.2 compaction knobs."""

    trigger_tokens: int = 100_000
    keep_recent_messages: int = 20
    summary_model: str = "anthropic:claude-haiku-4-5"
    anti_thrash_cooldown_s: int = 600


class RedactionCustomPattern(BaseModel):
    name: str
    regex: str


class RedactionSettings(BaseModel):
    """§4.9 redactor knobs."""

    enabled: bool = True
    custom: list[RedactionCustomPattern] = Field(default_factory=list)


class DockerSandboxSettings(BaseModel):
    image: str = "ghcr.io/mbarnes-code/devharness/runtime:latest"
    mount: str = "rw,cwd:/workspace"
    network: Literal["none", "host", "bridge"] = "none"


class SandboxSettings(BaseModel):
    """§4.6 sandbox selection."""

    default: Literal["local", "docker", "landlock", "modal", "daytona"] = "local"
    docker: DockerSandboxSettings = Field(default_factory=DockerSandboxSettings)


class BudgetSettings(BaseModel):
    """§9.2 budget caps."""

    max_turns: int = 100
    max_input_tokens: int = 2_000_000
    max_output_tokens: int = 500_000
    max_cost_usd: float = 10.0


class MemorySettings(BaseModel):
    """§4.2 memory backend selection."""

    backend: Literal["local", "none"] = "local"
    path: str | None = None


class OtelSettings(BaseModel):
    endpoint: str = "http://localhost:4317"
    enabled: bool = True


class JsonlSettings(BaseModel):
    enabled: bool = True


class ObservabilitySettings(BaseModel):
    """§4.14 observer knobs."""

    otel: OtelSettings = Field(default_factory=OtelSettings)
    jsonl: JsonlSettings = Field(default_factory=JsonlSettings)


class ApprovalSettings(BaseModel):
    """§4.7 approval mode + cache scope."""

    mode: Literal["suggest", "auto-edit", "auto"] = "suggest"
    require_approval_for_git_push: bool = True
    cache_within_session: bool = True


class RegistrySettings(BaseModel):
    """§4.4 registry / discovery cache."""

    discovery_cache_path: str | None = None
    include_categories: list[str] = Field(default_factory=list)
    exclude_tools: list[str] = Field(default_factory=list)


class SessionSettings(BaseModel):
    id_strategy: Literal["uuid", "deterministic"] = "uuid"


class FallbackEntry(BaseModel):
    provider: str
    model: str


# ─────────────────────────────────────────────────────────────────────────────
# Root
# ─────────────────────────────────────────────────────────────────────────────


class RootSettings(BaseModel):
    """Top-level merged config schema (spec §9)."""

    session: SessionSettings = Field(default_factory=SessionSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    model: dict[str, ProviderSettings] = Field(default_factory=dict)
    fallback_chain: dict[str, list[FallbackEntry]] = Field(default_factory=dict)
    approval: ApprovalSettings = Field(default_factory=ApprovalSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    compaction: CompactionSettings = Field(default_factory=CompactionSettings)
    redaction: RedactionSettings = Field(default_factory=RedactionSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    registry: RegistrySettings = Field(default_factory=RegistrySettings)
    secrets: dict[str, str] = Field(default_factory=dict)
    tools: dict[str, dict[str, Any]] = Field(default_factory=dict)
