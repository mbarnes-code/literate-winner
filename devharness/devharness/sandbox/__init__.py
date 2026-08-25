"""Sandbox package — SandboxBackend Protocol + backend factory.

No upstream code copied. Factory dispatch designed against spec §4.6.
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Public surface:
  - ``SandboxBackend`` / ``SandboxClass`` / ``SandboxRequest`` /
    ``SandboxResult`` — the Protocol and its plain-dataclass payloads.
  - ``get_backend(name, config)`` — construct the named backend. Optional
    cloud backends (``modal`` / ``daytona``) surface a clear "not
    available" ``RuntimeError`` when their SDK isn't installed rather
    than a raw ``ImportError`` from deep inside the backend module.
"""
from __future__ import annotations

from typing import Any

from devharness.sandbox.protocol import (
    SandboxBackend,
    SandboxClass,
    SandboxRequest,
    SandboxResult,
    network_allowed,
    shell_allowed,
)

# Names that map to a concrete backend class.
_BUILTIN_BACKENDS = frozenset({"local", "docker", "landlock"})
_OPTIONAL_BACKENDS = frozenset({"modal", "daytona"})


def get_backend(name: str, config: dict[str, Any] | None = None) -> SandboxBackend:
    """Return a ``SandboxBackend`` for *name*, configured with *config*.

    Raises ``ValueError`` for an unknown name and ``RuntimeError`` when
    an optional backend's SDK isn't installed. Backend construction
    itself is cheap; no daemon / cloud contact happens here.
    """
    key = (name or "").strip().lower()
    cfg = config or {}

    if key == "local":
        from devharness.sandbox.local import LocalBackend
        return LocalBackend()

    if key == "docker":
        try:
            from devharness.sandbox.docker import DockerBackend
        except ImportError as e:
            raise RuntimeError(
                "docker backend not available: pip install devharness[docker]"
            ) from e
        return DockerBackend(cfg)

    if key == "landlock":
        from devharness.sandbox.landlock import LandlockBackend
        return LandlockBackend(cfg)

    if key == "modal":
        try:
            from devharness.sandbox.modal import ModalBackend
        except ImportError as e:
            raise RuntimeError(
                "modal backend not available: pip install devharness[modal]"
            ) from e
        return ModalBackend(cfg)

    if key == "daytona":
        try:
            from devharness.sandbox.daytona import DaytonaBackend
        except ImportError as e:
            raise RuntimeError(
                "daytona backend not available: pip install devharness[daytona]"
            ) from e
        return DaytonaBackend(cfg)

    known = sorted(_BUILTIN_BACKENDS | _OPTIONAL_BACKENDS)
    raise ValueError(f"unknown sandbox backend: {name!r} (known: {known})")


__all__ = [
    "SandboxBackend",
    "SandboxClass",
    "SandboxRequest",
    "SandboxResult",
    "get_backend",
    "network_allowed",
    "shell_allowed",
]
