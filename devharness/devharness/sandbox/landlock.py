"""Landlock/bwrap sandbox backend — shells out to a Rust helper binary.

No upstream code copied. Design informed by:
  - reference/codex/codex-rs/linux-sandbox/ (Apache-2.0, upstream commit
    c9b19deb09c1841ce7acc33ddb96276030936a29) — the pattern of a small
    Linux-only helper that applies ``no_new_privs`` + seccomp + bubblewrap
    around one exec. We ship our own crate at
    ``crates/devharness-sandbox-linux/`` with an independent implementation.
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable design points:
  - The Rust binary path comes from the ``DEVHARNESS_SANDBOX_LINUX_BIN``
    env var (set by the mise / build tooling). If unset OR the current
    platform isn't Linux, ``supports`` returns False and ``execute``
    returns a refusal so the factory can fall back to docker/local.
  - The wire format between Python and the helper is a single JSON blob
    on stdin describing the request; stdout is the child's stdout, and
    a structured trailer on stderr carries the exit code and timing.
    (The Rust side implements this; here we only produce/consume it.)
    TODO: switch to a length-prefixed frame once the crate lands so we
    can multiplex stdout+stderr cleanly.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from typing import Any

from devharness.sandbox.protocol import (
    SandboxBackend,
    SandboxClass,
    SandboxRequest,
    SandboxResult,
    network_allowed,
    shell_allowed,
)

_ENV_BIN = "DEVHARNESS_SANDBOX_LINUX_BIN"

# Landlock+bwrap can enforce all six classes; the loop typically picks
# this backend only when the operator opts in via config.
_SUPPORTED: frozenset[SandboxClass] = frozenset(SandboxClass)


class LandlockBackend:
    """Landlock/bwrap backend that delegates to a Rust helper binary.

    Thread-safe: each ``execute`` spawns a fresh helper process.
    """

    name = "landlock"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        # Explicit override in config wins over env var; env var wins over None.
        self.binary: str | None = cfg.get("binary") or os.environ.get(_ENV_BIN)

    # ── availability check ─────────────────────────────────────

    def _availability_error(self) -> str | None:
        if platform.system() != "Linux":
            return (
                "landlock backend is Linux-only; "
                f"current platform={platform.system()}"
            )
        if not self.binary:
            return (
                f"landlock backend requires ${_ENV_BIN} to point at the "
                "devharness-sandbox-linux binary (build "
                "crates/devharness-sandbox-linux/ or install a prebuilt one)"
            )
        if not os.path.isfile(self.binary) or not os.access(self.binary, os.X_OK):
            return (
                f"landlock helper not found or not executable: {self.binary}"
            )
        return None

    def supports(self, sandbox_class: SandboxClass) -> bool:
        if self._availability_error() is not None:
            return False
        return sandbox_class in _SUPPORTED

    def execute(self, req: SandboxRequest) -> SandboxResult:
        err = self._availability_error()
        if err is not None:
            return SandboxResult(
                exit_code=127,
                stdout=b"",
                stderr=err.encode("utf-8"),
                duration_ms=0,
                timed_out=False,
                backend=self.name,
                refusal_reason=err,
            )
        if req.argv and not shell_allowed(req.sandbox_class):
            return SandboxResult(
                exit_code=126,
                stdout=b"",
                stderr=b"",
                duration_ms=0,
                timed_out=False,
                backend=self.name,
                refusal_reason=(
                    f"landlock backend refuses shell for sandbox_class="
                    f"{req.sandbox_class.value}"
                ),
            )

        wire = {
            "argv": list(req.argv),
            "cwd": str(req.cwd.resolve()),
            "sandbox_class": req.sandbox_class.value,
            "env": dict(req.env),
            "timeout_s": req.timeout_s,
            "network": network_allowed(req.sandbox_class),
            "session_id": req.session_id,
        }
        payload = json.dumps(wire, sort_keys=True).encode("utf-8")
        started = time.monotonic()
        # Prepend a stdin frame (JSON request) then the caller's stdin.
        # The Rust side reads until the first newline for the frame,
        # then forwards the rest to the child.
        stdin_blob = payload + b"\n" + (req.stdin or b"")

        try:
            proc = subprocess.run(
                [self.binary],  # type: ignore[list-item]
                input=stdin_blob,
                capture_output=True,
                timeout=req.timeout_s + 5.0,  # small grace over child timeout
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            return SandboxResult(
                exit_code=124,
                stdout=e.stdout or b"",
                stderr=(e.stderr or b"") + b"\n[timeout]",
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
                backend=self.name,
            )
        return SandboxResult(
            exit_code=proc.returncode,
            stdout=proc.stdout or b"",
            stderr=proc.stderr or b"",
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=False,
            backend=self.name,
        )


__all__ = ["LandlockBackend"]
