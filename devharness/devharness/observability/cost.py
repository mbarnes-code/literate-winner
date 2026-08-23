"""Token accounting and USD cost estimation.

Ported from vvaharness/util/tokens.py
  License: Apache License 2.0
  Copyright 2026 Visa, Inc.
  Source: https://github.com/visa/visa-vulnerability-agentic-harness
  Upstream commit: 3d972f679d8f5e3838b394edee0b5ea9c626b0fb
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Ported from hermes-agent/agent/billing_usage.py
  License: MIT License
  Copyright (c) 2025 Nous Research
  Source: https://github.com/NousResearch/hermes-agent
  Upstream commit: f293e7206b4ddd66042329442c6afebc19a8808d
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Combines VVAH's thread-safe token counter (input / completion /
cache-read / cache-write with per-phase buckets) with Hermes' UsageBar
formatting helpers, adapted for plain USD accounting. Skipped: VVAH
stage-tag names, Hermes' Nous-portal dollar-plan machinery.
"""

from __future__ import annotations

import math
import threading
import tomllib
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional


@dataclass
class TokenUsage:
    """One inference call's token accounting."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def billable_input(self) -> int:
        # Fresh input + cache-write is billed at input price; cache-read is
        # ~10% and tracked separately so it doesn't inflate the total.
        return self.input_tokens + self.cache_write_tokens


@dataclass
class _PhaseBucket:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    usd: float = 0.0


@dataclass
class _CostEntry:
    provider: str
    model: str
    usage: TokenUsage
    usd: float


def _finite(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


class CostTracker:
    """Process-wide token + USD accounting shared across providers.

    Pricing table shape (per-1M-token USD):
        {"anthropic/claude-3.5-sonnet": {
            "input": 3.00, "output": 15.00,
            "cache_read": 0.30, "cache_write": 3.75,
        }, ...}
    """

    def __init__(self, pricing: Optional[dict[str, dict[str, float]]] = None) -> None:
        self._lock = threading.Lock()
        self._pricing: dict[str, dict[str, float]] = pricing or {}
        self._phase = "unlabeled"
        self._by_phase: dict[str, _PhaseBucket] = defaultdict(_PhaseBucket)
        self._entries: list[_CostEntry] = []
        self._total_usd = 0.0

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        prev = self._phase
        self._phase = name
        try:
            yield
        finally:
            self._phase = prev

    def _price_for(self, provider: str, model: str) -> dict[str, float]:
        key = f"{provider}/{model}"
        if key in self._pricing:
            return self._pricing[key]
        if model in self._pricing:
            return self._pricing[model]
        return {}

    def record(self, provider: str, model: str, usage: TokenUsage) -> float:
        """Record a call and return its USD cost.

        Returns 0.0 when no pricing entry is registered — accounting
        still records the token counts so quotas can be enforced even
        when priced-in-dollars is unknown.
        """
        price = self._price_for(provider, model)
        cost = 0.0
        if price:
            cost += (usage.input_tokens / 1_000_000) * price.get("input", 0.0)
            cost += (usage.output_tokens / 1_000_000) * price.get("output", 0.0)
            cost += (usage.cache_read_tokens / 1_000_000) * price.get(
                "cache_read", price.get("input", 0.0) * 0.1
            )
            cost += (usage.cache_write_tokens / 1_000_000) * price.get(
                "cache_write", price.get("input", 0.0) * 1.25
            )
        with self._lock:
            self._total_usd += cost
            bucket = self._by_phase[self._phase]
            bucket.input_tokens += usage.input_tokens
            bucket.output_tokens += usage.output_tokens
            bucket.cache_read_tokens += usage.cache_read_tokens
            bucket.cache_write_tokens += usage.cache_write_tokens
            bucket.calls += 1
            bucket.usd += cost
            self._entries.append(_CostEntry(provider, model, usage, cost))
        return cost

    def session_total_usd(self) -> float:
        with self._lock:
            return self._total_usd

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total_usd": round(self._total_usd, 6),
                "calls": sum(b.calls for b in self._by_phase.values()),
                "by_phase": {
                    name: {
                        "input_tokens": b.input_tokens,
                        "output_tokens": b.output_tokens,
                        "cache_read_tokens": b.cache_read_tokens,
                        "cache_write_tokens": b.cache_write_tokens,
                        "calls": b.calls,
                        "usd": round(b.usd, 6),
                    }
                    for name, b in self._by_phase.items()
                },
            }

    def format_bar(self, budget_usd: Optional[float] = None, width: int = 30) -> str:
        """Render a Rich-compatible single-line usage bar.

        With a budget, shows ``[####----] $spent / $budget (NN%)``;
        without one, shows ``$spent (N calls)``.
        """
        spent = self.session_total_usd()
        calls = sum(b.calls for b in self._by_phase.values())
        if budget_usd is None or budget_usd <= 0:
            return f"[cost] ${spent:,.4f} ({calls} calls)"
        pct = max(0.0, min(1.0, spent / budget_usd))
        fill = int(round(pct * width))
        bar = "#" * fill + "-" * (width - fill)
        pct_i = int(round(pct * 100))
        return f"[cost] [{bar}] ${spent:,.4f} / ${budget_usd:,.4f} ({pct_i}%)"


def load_pricing_table(path: Optional[str] = None) -> dict[str, dict[str, float]]:
    """Load a pricing table from ``pricing.toml``.

    Format:
        ["anthropic/claude-3.5-sonnet"]
        input = 3.00
        output = 15.00
        cache_read = 0.30
        cache_write = 3.75

    Returns an empty dict when the file is missing so callers degrade
    to token-only accounting instead of raising.
    """
    if path is None:
        path = str(Path(__file__).parent / "pricing.toml")
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    result: dict[str, dict[str, float]] = {}
    for model_key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        prices: dict[str, float] = {}
        for field_name in ("input", "output", "cache_read", "cache_write"):
            v = _finite(entry.get(field_name))
            if v is not None:
                prices[field_name] = v
        if prices:
            result[str(model_key)] = prices
    return result


@dataclass
class UsageBar:
    """Simple bar renderer for a spent-of-total USD figure.

    Adapted from hermes-agent/agent/billing_usage.py::UsageBar, stripped
    of Nous-portal plan/topup distinction.
    """

    remaining_usd: float
    total_usd: float
    spent_usd: float = 0.0
    label: str = "usage"

    @property
    def pct_used(self) -> int:
        if self.total_usd <= 0:
            return 0
        return max(0, min(100, round(self.spent_usd / self.total_usd * 100)))

    def render(self, width: int = 30) -> str:
        if self.total_usd <= 0:
            return f"[{self.label}] ${self.spent_usd:,.4f}"
        frac = max(0.0, min(1.0, self.spent_usd / self.total_usd))
        fill = int(round(frac * width))
        bar = "#" * fill + "-" * (width - fill)
        return (
            f"[{self.label}] [{bar}] "
            f"${self.spent_usd:,.4f} / ${self.total_usd:,.4f} ({self.pct_used}%)"
        )


# Process-wide singleton (VVAH pattern).
TOKENS = CostTracker()
