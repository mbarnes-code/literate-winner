"""Docker container-per-session sandbox backend.

No upstream code copied. Design informed by:
  - reference/codex/codex-rs/linux-sandbox/ (Apache-2.0, upstream commit
    c9b19deb09c1841ce7acc33ddb96276030936a29) — network-off default,
    filesystem mount pattern, per-invocation launch model. We use the
    Docker daemon rather than Landlock/bwrap; the design shape is the
    same but the implementation is our own Python.
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable design points:
  - ``docker`` SDK is imported lazily inside ``_client()``; the module
    imports cleanly even when the SDK isn't installed so the factory
    can surface a "not available" message.
  - One container per session, name ``devharness-{session_id}``. First
    ``execute`` in a session creates it; subsequent calls reuse it via
    ``exec_run``. Session teardown (``close``) is caller-driven; if the
    process crashes, ``docker container prune`` handles orphans.
  - ``--network=none`` unless the request's sandbox_class permits egress
    (``NETWORK`` / ``MIXED``); ``MUTATING_ENV`` gets network too so
    package registries are reachable.
  - We refuse ``sandbox_class`` values that our create-args cannot
    enforce (e.g. we can't distinguish "package registry only" from
    "any egress" at the Docker layer — treat ``MUTATING_ENV`` as full
    network and let the tool wrapper narrow it via HTTP proxy config).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from devharness.sandbox.protocol import (
    SandboxBackend,
    SandboxClass,
    SandboxRequest,
    SandboxResult,
    network_allowed,
    shell_allowed,
)

_DEFAULT_IMAGE = "python:3.12-slim"
_WORKSPACE_MOUNT = "/workspace"

# Which classes this backend can enforce. Docker can host all six, but
# ``READ_ONLY`` / ``MUTATING_FILE`` should prefer the local backend to
# avoid container-startup overhead; the factory picks per-class.
_SUPPORTED: frozenset[SandboxClass] = frozenset({
    SandboxClass.READ_ONLY,
    SandboxClass.MUTATING_FILE,
    SandboxClass.MUTATING_GIT,
    SandboxClass.MUTATING_ENV,
    SandboxClass.NETWORK,
    SandboxClass.MIXED,
})


class DockerBackend:
    """Container-per-session Docker backend.

    Not thread-safe across ``execute`` for different sessions: each
    session gets its own container, but the internal container cache
    is guarded by ``_lock`` so concurrent turns in the same session
    reuse the same container.
    """

    name = "docker"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.image: str = cfg.get("image", _DEFAULT_IMAGE)
        self.workspace_mount: str = cfg.get("workspace_mount", _WORKSPACE_MOUNT)
        self._client_obj: Any = None
        self._containers: dict[str, Any] = {}
        self._lock = threading.Lock()

    # ── lazy SDK / daemon handles ────────────────────────────────

    def _client(self) -> Any:
        if self._client_obj is not None:
            return self._client_obj
        try:
            import docker  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "docker backend requires the 'docker' python package: "
                "pip install devharness[docker]"
            ) from e
        self._client_obj = docker.from_env()
        return self._client_obj

    def _container_name(self, session_id: str) -> str:
        # Container names allow [A-Za-z0-9_.-]; session_id may contain colons
        # if it was derived from a URL, so we sanitize aggressively.
        safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in session_id)
        return f"devharness-{safe}"

    def _get_or_create_container(
        self, session_id: str, cwd: Path, needs_network: bool
    ) -> Any:
        key = f"{session_id}:{int(needs_network)}"
        with self._lock:
            existing = self._containers.get(key)
            if existing is not None:
                try:
                    existing.reload()
                    if existing.status in ("running", "created"):
                        return existing
                except Exception:
                    self._containers.pop(key, None)

            client = self._client()
            name = self._container_name(session_id)
            if needs_network:
                name = f"{name}-net"
            # Best-effort cleanup of a stale container from a prior process.
            try:
                stale = client.containers.get(name)
                stale.remove(force=True)
            except Exception:
                pass

            container = client.containers.run(
                self.image,
                command=["sleep", "infinity"],
                name=name,
                detach=True,
                remove=False,
                network_mode="bridge" if needs_network else "none",
                working_dir=self.workspace_mount,
                volumes={
                    str(cwd.resolve()): {
                        "bind": self.workspace_mount,
                        "mode": "rw",
                    },
                },
                # Refuse privileged; drop most caps. Keep the read-only-root
                # off for now because pip installs write to site-packages.
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
            )
            self._containers[key] = container
            return container

    # ── Protocol surface ────────────────────────────────────────

    def supports(self, sandbox_class: SandboxClass) -> bool:
        return sandbox_class in _SUPPORTED

    def execute(self, req: SandboxRequest) -> SandboxResult:
        # Filesystem-only classes shouldn't be paying container overhead
        # for a subprocess. Still, if a caller routes them here, enforce
        # the "no shell" rule the same way local does.
        needs_shell = bool(req.argv)
        if needs_shell and not shell_allowed(req.sandbox_class):
            return SandboxResult(
                exit_code=126,
                stdout=b"",
                stderr=b"",
                duration_ms=0,
                timed_out=False,
                backend=self.name,
                refusal_reason=(
                    f"docker backend refuses shell for sandbox_class="
                    f"{req.sandbox_class.value}"
                ),
            )

        needs_network = network_allowed(req.sandbox_class)
        started = time.monotonic()
        try:
            container = self._get_or_create_container(
                req.session_id, req.cwd, needs_network
            )
        except Exception as e:
            return SandboxResult(
                exit_code=125,
                stdout=b"",
                stderr=str(e).encode("utf-8", errors="replace"),
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=False,
                backend=self.name,
                refusal_reason=f"docker container startup failed: {e}",
            )

        try:
            exec_result = container.exec_run(
                cmd=list(req.argv),
                workdir=self.workspace_mount,
                environment=dict(req.env),
                demux=True,
                # docker-py has no per-exec timeout; we rely on the tool
                # dispatcher to enforce timeout_s at a higher layer.
                # TODO: wrap in a watchdog thread that calls exec_id
                # kill via low-level API.
            )
        except Exception as e:
            return SandboxResult(
                exit_code=125,
                stdout=b"",
                stderr=str(e).encode("utf-8", errors="replace"),
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=False,
                backend=self.name,
                refusal_reason=f"docker exec failed: {e}",
            )

        # demux=True → (stdout_bytes, stderr_bytes) tuple; on some daemon
        # versions the tuple can contain None for empty streams.
        out, err = exec_result.output if isinstance(exec_result.output, tuple) else (exec_result.output, b"")
        return SandboxResult(
            exit_code=int(exec_result.exit_code or 0),
            stdout=out or b"",
            stderr=err or b"",
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=False,
            backend=self.name,
        )

    # ── Lifecycle helpers (caller-driven; not part of Protocol) ─

    def close_session(self, session_id: str) -> None:
        """Stop and remove containers for *session_id*. Idempotent."""
        with self._lock:
            for key in list(self._containers.keys()):
                if not key.startswith(f"{session_id}:"):
                    continue
                c = self._containers.pop(key, None)
                if c is None:
                    continue
                try:
                    c.remove(force=True)
                except Exception:
                    pass


__all__ = ["DockerBackend"]
