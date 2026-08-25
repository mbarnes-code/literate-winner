"""MemoryProvider Protocol — common interface for memory backends.

No upstream code copied — this Protocol is derived from
docs/devharness-spec.md §4.2 (memory contract) so backends can be
swapped without touching loop code. A backend implements the four
methods below; the loop reads keys before each turn (``read``) and
writes updates after (``write`` / ``snapshot``).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryProvider(Protocol):
    """Read/write key-value memory. Values are opaque strings.

    ``snapshot`` returns a full dict-of-key-to-value for observability /
    session-manifest embedding; ``list_keys`` is the streaming variant
    for large stores.
    """

    def read(self, key: str) -> str | None: ...

    def write(self, key: str, value: str) -> None: ...

    def list_keys(self) -> list[str]: ...

    def snapshot(self) -> dict[str, Any]: ...
