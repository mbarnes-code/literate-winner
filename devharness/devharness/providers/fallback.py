"""Cross-provider fallback chain.

No upstream code copied — interface conforms to spec §4.3.

Semantics (spec §4.3):
  - On HTTP 429 from a provider, wait ``Retry-After`` seconds (bounded), then
    try the same provider once more before rotating.
  - On HTTP 5xx (or a connection-level error), rotate to the next provider
    immediately.
  - On any other exception, rotate to the next provider immediately.
  - When the chain is exhausted, re-raise the last error as an ``error``
    :class:`StreamEvent`.

Failures are detected mid-stream when a provider yields an ``error``
StreamEvent, or synchronously at ``iter()`` time.  In both cases the fallback
switches only when NO ``text_delta`` / ``tool_call_delta`` has been forwarded
downstream yet — once bytes are on the wire we cannot mid-stream retry
without model-level corruption.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterator, Tuple

from .base import Provider, Request, StreamEvent

logger = logging.getLogger(__name__)

_MAX_RETRY_AFTER = 60.0  # cap for `Retry-After` waits (seconds)


def _classify(exc: BaseException) -> tuple[str, float]:
    """Return ``(kind, retry_after_seconds)`` for *exc*.

    ``kind`` is one of ``"rate"``, ``"server"``, ``"other"``.  Provider SDKs
    surface HTTP status codes differently; we inspect common attributes.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
    text = str(exc)
    retry_after = 0.0
    hdr = getattr(getattr(exc, "response", None), "headers", None)
    if hdr is not None:
        raw = hdr.get("retry-after") or hdr.get("Retry-After")
        try:
            retry_after = min(float(raw), _MAX_RETRY_AFTER) if raw else 0.0
        except (TypeError, ValueError):
            retry_after = 0.0
    if status == 429 or "rate limit" in text.lower() or "429" in text:
        return "rate", retry_after
    if isinstance(status, int) and 500 <= status < 600:
        return "server", 0.0
    if re.search(r"5\d\d\b", text):
        return "server", 0.0
    return "other", 0.0


def _retry_after_from_event(event: StreamEvent) -> float:
    """Best-effort ``Retry-After`` extraction from an ``error`` event."""
    err = event.error or ""
    m = re.search(r"retry-after[:\s]+(\d+(?:\.\d+)?)", err, re.I)
    if not m:
        return 0.0
    try:
        return min(float(m.group(1)), _MAX_RETRY_AFTER)
    except ValueError:
        return 0.0


class FallbackProvider(Provider):
    """Rotate through *chain* on transient errors.

    ``supports_prompt_cache`` / ``supports_reasoning`` / ``price_per_1k``
    delegate to the head of the chain — the loop treats a fallback provider
    as its declared primary for capability advertising.
    """

    name = "fallback"

    def __init__(self, chain: list[Provider], *, sleep: Any = time.sleep) -> None:
        if not chain:
            raise ValueError("FallbackProvider requires a non-empty chain")
        self._chain = list(chain)
        self._sleep = sleep

    def stream(self, req: Request) -> Iterator[StreamEvent]:
        last_error: str | None = None
        for idx, provider in enumerate(self._chain):
            attempts = 0
            while True:
                attempts += 1
                started = False
                try:
                    stream = provider.stream(req)
                    for event in stream:
                        if event.kind == "error":
                            last_error = event.error
                            if not started and attempts == 1:
                                wait = _retry_after_from_event(event)
                                if wait > 0 and "429" in (event.error or ""):
                                    logger.info(
                                        "provider=%s hit 429, waiting %.1fs before retry",
                                        provider.name, wait,
                                    )
                                    self._sleep(wait)
                                    break  # inner for → outer while retries same provider
                            break  # rotate
                        if event.kind in {"text_delta", "tool_call_delta"}:
                            started = True
                        yield event
                        if event.kind == "finish":
                            return
                    else:
                        # Generator exhausted without a finish/error event.
                        return
                    # Reached break inside the for-loop; decide retry vs rotate.
                    if not started and attempts == 1 and last_error and "429" in last_error:
                        continue  # same provider one more time
                    break  # rotate to next provider
                except BaseException as exc:  # noqa: BLE001
                    kind, retry_after = _classify(exc)
                    last_error = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "provider=%s failed (%s): %s", provider.name, kind, last_error,
                    )
                    if kind == "rate" and attempts == 1:
                        if retry_after > 0:
                            self._sleep(retry_after)
                        continue  # retry same provider once
                    break  # rotate
        yield StreamEvent(kind="error", error=last_error or "fallback chain exhausted")

    def supports_prompt_cache(self) -> bool:
        return self._chain[0].supports_prompt_cache()

    def supports_reasoning(self) -> bool:
        return self._chain[0].supports_reasoning()

    def price_per_1k(self) -> Tuple[float, float, float, float]:
        return self._chain[0].price_per_1k()


__all__ = ["FallbackProvider"]
