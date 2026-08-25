"""result — ToolResult dataclass.

No upstream code copied — Hermes and VVAH both return plain dicts /
strings from tool handlers; this dataclass is the devharness shape
mandated by spec §4.4 so downstream steps (redaction, spillover, OTel)
have a single boundary object.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """One tool-call return payload.

    Fields per spec §4.4:
      - ``ok``: True when the tool completed without a handled error.
      - ``content``: primary payload the model will see (string
        preferred; structured JSON allowed and stringified by ``to_message``).
      - ``error``: short error tag when ``ok is False``.
      - ``exit_code``: for shell-like tools; None otherwise.
      - ``duration_ms``: wall-clock cost.
      - ``artifacts``: side-channel pointers (spillover paths, snapshot
        SHAs, diff URIs) — not shown to the model unless explicitly
        embedded in ``content``.
      - ``truncated``: True when ResultSpillover trimmed the payload.
      - ``metadata``: opaque extras the loop/observer may consume.
    """

    ok: bool
    content: Any = ""
    error: str | None = None
    exit_code: int | None = None
    duration_ms: float = 0.0
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_message(self, *, role: str = "tool",
                   tool_call_id: str) -> dict[str, Any]:
        """Render as an OpenAI-compatible chat message dict."""
        if isinstance(self.content, (dict, list)):
            import json
            content = json.dumps(self.content, sort_keys=True,
                                 separators=(",", ":"), default=str)
        else:
            content = "" if self.content is None else str(self.content)
        if not self.ok and self.error and not content.startswith("[error]"):
            content = f"[error] {self.error}\n{content}".rstrip()
        return {
            "role": role,
            "tool_call_id": tool_call_id,
            "content": content,
        }
