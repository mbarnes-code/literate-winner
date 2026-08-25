"""SandboxBackend Protocol — common interface for all sandbox backends.

No upstream code copied — designed from scratch against the
devharness spec §4.6 sandbox-class → capability matrix.
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable design points:
  - ``SandboxClass`` mirrors the six classes declared in spec §4.6 exactly;
    every ``ToolSpec.sandbox_class`` value is drawn from this enum.
  - ``SandboxRequest`` / ``SandboxResult`` are plain dataclasses (frozen on
    the request side) so they can be diff'd, cached by hash, and recorded
    in the ``rollout.db`` audit log without pydantic overhead.
  - The Protocol has no lifecycle hooks (``open``/``close``) at this layer;
    backends do their own lazy warm-up on first ``execute`` so factory
    construction never talks to Docker/Modal/Daytona.
  - ``supports`` is a *static* per-class capability check so the loop can
    reject a mis-scoped tool BEFORE handing bytes to the backend.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable


class SandboxClass(str, Enum):
    """Isolation class declared at tool-registration time (spec §4.6).

    The string value doubles as the on-disk / on-wire identifier so it can
    be persisted in ``rollout.db`` and compared across replays without a
    round-trip through the enum type.
    """

    READ_ONLY = "read-only"
    MUTATING_FILE = "mutating-file"
    MUTATING_GIT = "mutating-git"
    MUTATING_ENV = "mutating-env"
    NETWORK = "network"
    MIXED = "mixed"


# Classes that permit spawning a shell / subprocess at all. See spec §4.6:
#   read-only    → ❌   mutating-file → ❌   network → ❌
#   mutating-git → git   mutating-env → pkg mgr   mixed → on
_SHELL_ALLOWED: frozenset[SandboxClass] = frozenset({
    SandboxClass.MUTATING_GIT,
    SandboxClass.MUTATING_ENV,
    SandboxClass.MIXED,
})

# Classes that permit network egress. Spec §4.6:
#   read-only, mutating-file, mutating-git → ❌
#   mutating-env → package registry only    network → on
#   mixed → on (approval-gated by ApprovalGate, not this layer)
_NETWORK_ALLOWED: frozenset[SandboxClass] = frozenset({
    SandboxClass.MUTATING_ENV,
    SandboxClass.NETWORK,
    SandboxClass.MIXED,
})


def shell_allowed(cls: SandboxClass) -> bool:
    """Return True if *cls* is permitted to spawn a subprocess at all."""
    return cls in _SHELL_ALLOWED


def network_allowed(cls: SandboxClass) -> bool:
    """Return True if *cls* is permitted egress; ``MIXED`` still needs approval."""
    return cls in _NETWORK_ALLOWED


@dataclass(frozen=True)
class SandboxRequest:
    """One sandbox execution request.

    All fields are captured verbatim into ``SessionStore`` so a replay can
    reconstruct the exact process invocation.
    """

    argv: tuple[str, ...]
    cwd: Path
    sandbox_class: SandboxClass
    session_id: str
    env: Mapping[str, str] = field(default_factory=dict)
    stdin: bytes | None = None
    timeout_s: float = 60.0
    # Optional caller-scoped tag (e.g. tool name) for observability spans.
    label: str = ""


@dataclass
class SandboxResult:
    """Outcome of a sandbox execution.

    ``backend`` is the name of the backend that ran the request (e.g.
    ``"local"``, ``"docker"``) so the loop can annotate spans and the
    rollout can prove which isolation was actually applied.
    """

    exit_code: int
    stdout: bytes
    stderr: bytes
    duration_ms: int
    timed_out: bool
    backend: str
    # Populated when the backend refuses a request outright (e.g. shell not
    # allowed for the requested class). ``exit_code`` will be non-zero.
    refusal_reason: str | None = None


@runtime_checkable
class SandboxBackend(Protocol):
    """The Protocol every backend implements (spec §4.6).

    Implementations MUST be thread-safe: ``execute`` can be called
    concurrently by the tool dispatcher when the batch is parallel-safe.
    """

    name: str

    def supports(self, sandbox_class: SandboxClass) -> bool:
        """Return True if this backend can host the given class.

        Backends refuse classes they cannot enforce (e.g. ``local`` refuses
        ``MUTATING_ENV`` because it cannot roll back package installs).
        """
        ...

    def execute(self, req: SandboxRequest) -> SandboxResult:
        """Run *req* under this backend's isolation and return the result.

        MUST NOT raise for tool-level failures (non-zero exit, timeout);
        those are reported through ``SandboxResult``. May raise for
        backend-infrastructure failures (Docker daemon down, missing
        binary), which the caller escalates to a session error.
        """
        ...


__all__ = [
    "SandboxBackend",
    "SandboxClass",
    "SandboxRequest",
    "SandboxResult",
    "shell_allowed",
    "network_allowed",
]
