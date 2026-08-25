"""SSE parser for provider streaming responses (spec §4.3).

No upstream code copied — design from the HTML5 EventSource / SSE spec.
The hermes-agent transports file cited in the task description does not
exist at this path in the reference tree, so no upstream code is lifted.

Given an iterable of raw HTTP body chunks (``bytes``), ``iter_sse_events``
yields ``SSEEvent(event, data)`` tuples per SSE framing: lines beginning
``event:`` set the event type; lines beginning ``data:`` accumulate into
the payload; a blank line dispatches. Comments (``:``-prefixed) are
skipped. ``iter_stream_events`` maps SSE events onto the provider-neutral
``StreamEvent`` type so ``InferenceRunner`` consumes one shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Iterator


@dataclass(slots=True, frozen=True)
class SSEEvent:
    """One dispatched SSE event."""

    event: str
    data: str


def _iter_lines(chunks: Iterable[bytes | str]) -> Iterator[str]:
    """Split byte/str chunks into logical lines (CR, LF, or CRLF)."""

    buf = ""
    for chunk in chunks:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        buf += chunk
        # SSE line terminators: LF, CR, or CRLF.
        while True:
            i_lf = buf.find("\n")
            i_cr = buf.find("\r")
            if i_lf == -1 and i_cr == -1:
                break
            if i_lf == -1:
                idx, skip = i_cr, 1
            elif i_cr == -1:
                idx, skip = i_lf, 1
            elif i_cr < i_lf:
                # CR alone or CRLF
                idx = i_cr
                skip = 2 if i_lf == i_cr + 1 else 1
            else:
                idx, skip = i_lf, 1
            yield buf[:idx]
            buf = buf[idx + skip:]
    if buf:
        yield buf


def iter_sse_events(chunks: Iterable[bytes | str]) -> Iterator[SSEEvent]:
    """Parse raw SSE-framed chunks into dispatched ``SSEEvent`` records."""

    event: str = "message"
    data_lines: list[str] = []

    for raw in _iter_lines(chunks):
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                yield SSEEvent(event=event, data="\n".join(data_lines))
                data_lines = []
                event = "message"
            continue
        if line.startswith(":"):
            # Comment / keep-alive.
            continue
        if ":" in line:
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""
        if field == "event":
            event = value or "message"
        elif field == "data":
            data_lines.append(value)
        elif field == "id":
            continue
        elif field == "retry":
            continue
        # Unknown fields are ignored per SSE spec.


def iter_stream_events(chunks: Iterable[bytes | str]) -> Iterator[Any]:
    """Map SSE events onto ``providers.base.StreamEvent`` for the loop.

    Providers vary in their event schemas; this default mapping handles
    the OpenAI Chat Completions ``data: {...}`` shape and the
    ``data: [DONE]`` sentinel.
    """

    # Lazy import to keep providers/base out of the SSE parser's import graph.
    from devharness.providers.base import StreamEvent

    for sse in iter_sse_events(chunks):
        if sse.data == "[DONE]":
            yield StreamEvent(kind="finish", finish_reason="stop")
            return
        try:
            payload = json.loads(sse.data)
        except json.JSONDecodeError:
            continue
        choices = payload.get("choices") or []
        for choice in choices:
            delta = choice.get("delta") or {}
            if "content" in delta and delta["content"]:
                yield StreamEvent(kind="text_delta", text=delta["content"])
            for tc in delta.get("tool_calls") or ():
                fn = tc.get("function") or {}
                yield StreamEvent(
                    kind="tool_call_delta",
                    tool_call_id=str(tc.get("id") or tc.get("index") or ""),
                    tool_name=fn.get("name") or None,
                    arguments_delta=fn.get("arguments") or None,
                )
            finish = choice.get("finish_reason")
            if finish:
                yield StreamEvent(kind="finish", finish_reason=finish)
        if "usage" in payload and payload["usage"]:
            u = payload["usage"] or {}
            yield StreamEvent(kind="usage", usage={
                "input": int(u.get("prompt_tokens", 0)),
                "output": int(u.get("completion_tokens", 0)),
                "cache_read": int(u.get("cache_read_input_tokens", 0)),
                "cache_write": int(u.get("cache_creation_input_tokens", 0)),
            })


__all__ = ["SSEEvent", "iter_sse_events", "iter_stream_events"]
