"""OpenTelemetry emitter for gen_ai.* semantic conventions.

No upstream code copied — spec §4.14 explicitly says DIY on top of
``opentelemetry-api`` + ``opentelemetry-exporter-otlp`` (Codex and Goose
OTel emitters are Rust and study-only). Fields follow the OTel GenAI
semantic conventions (``gen_ai.provider.name``,
``gen_ai.request.model``, ``gen_ai.response.finish_reasons``,
``gen_ai.usage.*``) plus devharness-specific attributes
(``session.id``, ``turn.id``, ``tool.*``, ``approval.decision``).

The SDK is lazy-imported: when ``opentelemetry-api`` is missing, all
methods become no-ops and every span is a NullSpan. Callers never
have to check.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

_tracer = None
_ready = False
_disabled = False


def _init_tracer() -> Any:
    """Lazily construct a tracer. Returns None on missing SDK."""
    global _tracer, _ready, _disabled
    if _ready:
        return _tracer
    _ready = True
    if os.environ.get("DEVHARNESS_OTEL_DISABLED"):
        _disabled = True
        return None
    try:
        from opentelemetry import trace  # type: ignore[import-not-found]
    except ImportError:
        _disabled = True
        return None
    # Prefer an already-configured global TracerProvider; only wire an
    # OTLP exporter if the caller left the SDK unconfigured.
    provider = trace.get_tracer_provider()
    if type(provider).__name__ == "ProxyTracerProvider":
        try:
            from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
            from opentelemetry.sdk.trace.export import (  # type: ignore[import-not-found]
                BatchSpanProcessor,
            )
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore[import-not-found]
                OTLPSpanExporter,
            )
            provider = TracerProvider()
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(provider)
        except ImportError:
            # SDK-api only install: emit into a no-op provider.
            pass
    _tracer = trace.get_tracer("devharness")
    return _tracer


class _NullSpan:
    def set_attribute(self, *_: Any, **__: Any) -> None: ...

    def record_exception(self, *_: Any, **__: Any) -> None: ...

    def set_status(self, *_: Any, **__: Any) -> None: ...

    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, *_: Any) -> None: ...


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    """Start an OTel span; yield NullSpan when SDK is missing."""
    tracer = _init_tracer()
    if tracer is None:
        yield _NullSpan()
        return
    with tracer.start_as_current_span(name) as sp:
        for k, v in (attributes or {}).items():
            try:
                sp.set_attribute(k, v)
            except Exception:  # pragma: no cover - attr type guard
                sp.set_attribute(k, str(v))
        yield sp


def emit_llm_call(*, provider: str, model: str, session_id: str,
                  turn_id: int, usage: dict[str, int],
                  finish_reasons: list[str] | None = None) -> None:
    """Emit one ``gen_ai.*`` span for a completed LLM call."""
    attrs: dict[str, Any] = {
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": model,
        "session.id": session_id,
        "turn.id": turn_id,
    }
    for k, v in usage.items():
        attrs[f"gen_ai.usage.{k}"] = int(v)
    if finish_reasons:
        attrs["gen_ai.response.finish_reasons"] = list(finish_reasons)
    with span("gen_ai.chat.completion", attrs):
        pass


def emit_tool_call(*, tool_name: str, session_id: str, turn_id: int,
                   duration_ms: float, status: str,
                   approval_decision: str | None = None) -> None:
    """Emit one span for a completed tool call."""
    attrs: dict[str, Any] = {
        f"tool.{tool_name}.duration_ms": float(duration_ms),
        f"tool.{tool_name}.status": status,
        "session.id": session_id,
        "turn.id": turn_id,
    }
    if approval_decision is not None:
        attrs["approval.decision"] = approval_decision
    with span(f"devharness.tool.{tool_name}", attrs):
        pass


def is_available() -> bool:
    """True when the OTel SDK is importable."""
    _init_tracer()
    return not _disabled
