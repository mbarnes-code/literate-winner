"""In-process sandbox backend — path-jailed subprocess exec on the host.

No new upstream code copied. This backend wraps ``devharness.sandbox._jail``
(itself ported from vvaharness/backends/localtools.py, Apache-2.0) and
runs shell subprocesses via ``subprocess.run`` with a jailed cwd, a
filtered environment, and a per-request timeout.
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable design points:
  - Refuses any request whose ``sandbox_class`` does not permit a shell
    (``READ_ONLY``, ``MUTATING_FILE``, ``NETWORK``). Filesystem-only
    tools should call ``_jail._read`` / ``_glob`` / ``_grep`` directly;
    they don't need this backend at all.
  - Refuses ``MUTATING_ENV`` because the local host has no rollback for
    package installs; use the docker/landlock backend for that class.
  - ``NETWORK`` egress on the host is not blockable in-process; we refuse
    the class here and defer to docker/landlock.
  - Env is scrubbed to a minimal allow-list so a compromised subprocess
    can't read the operator's cloud tokens from the parent env.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from devharness.sandbox._jail import _jail
from devharness.sandbox.protocol import (
    SandboxBackend,
    SandboxClass,
    SandboxRequest,
    SandboxResult,
    shell_allowed,
)

# Env vars that are always safe to forward to a child (tool wrappers rely
# on these; everything else is stripped so cloud tokens don't leak).
_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE",
    "TZ", "TERM", "SHELL", "PWD", "TMPDIR", "TMP", "TEMP",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
})

# Which sandbox classes this backend can enforce.
_SUPPORTED: frozenset[SandboxClass] = frozenset({
    SandboxClass.MUTATING_GIT,
    SandboxClass.MIXED,
})


def _filter_env(caller_env: dict[str, str] | None) -> dict[str, str]:
    """Return a scrubbed env: allow-list from the parent, overlay caller vars.

    Caller-supplied vars win over parent so tools can set (e.g.)
    ``GIT_AUTHOR_NAME`` per-call without mutating the process env.
    """
    base = {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST}
    if caller_env:
        base.update(caller_env)
    return base


class LocalBackend:
    """Path-jailed host execution. Thread-safe (subprocess.run is)."""

    name = "local"

    def supports(self, sandbox_class: SandboxClass) -> bool:
        return sandbox_class in _SUPPORTED

    def execute(self, req: SandboxRequest) -> SandboxResult:
        # Refuse classes we cannot enforce in-process.
        if not shell_allowed(req.sandbox_class):
            return SandboxResult(
                exit_code=126,
                stdout=b"",
                stderr=b"",
                duration_ms=0,
                timed_out=False,
                backend=self.name,
                refusal_reason=(
                    f"local backend refuses shell for sandbox_class="
                    f"{req.sandbox_class.value}"
                ),
            )
        if not self.supports(req.sandbox_class):
            return SandboxResult(
                exit_code=126,
                stdout=b"",
                stderr=b"",
                duration_ms=0,
                timed_out=False,
                backend=self.name,
                refusal_reason=(
                    f"local backend cannot enforce sandbox_class="
                    f"{req.sandbox_class.value}; use docker or landlock"
                ),
            )

        root = req.cwd.resolve()
        if not root.is_dir():
            return SandboxResult(
                exit_code=127,
                stdout=b"",
                stderr=b"",
                duration_ms=0,
                timed_out=False,
                backend=self.name,
                refusal_reason=f"cwd does not exist or is not a directory: {root}",
            )
        # Sanity-check the cwd through the jail primitive so a symlink
        # pointing outside the caller's intended root is rejected before
        # we exec anything inside it.
        if _jail(root, ".") is None:
            return SandboxResult(
                exit_code=126,
                stdout=b"",
                stderr=b"",
                duration_ms=0,
                timed_out=False,
                backend=self.name,
                refusal_reason=f"cwd fails jail check: {root}",
            )

        env = _filter_env(dict(req.env))
        argv = list(req.argv)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(root),
                env=env,
                input=req.stdin,
                timeout=req.timeout_s,
                capture_output=True,
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
        except FileNotFoundError as e:
            return SandboxResult(
                exit_code=127,
                stdout=b"",
                stderr=str(e).encode("utf-8", errors="replace"),
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=False,
                backend=self.name,
                refusal_reason=f"argv[0] not found: {argv[0] if argv else ''}",
            )

        return SandboxResult(
            exit_code=proc.returncode,
            stdout=proc.stdout or b"",
            stderr=proc.stderr or b"",
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=False,
            backend=self.name,
        )


__all__ = ["LocalBackend"]

# Re-export path so callers can do `from devharness.sandbox.local import _jail`
# when they need direct filesystem-jailed reads (e.g. read_file / grep tools).
_ = Path  # keep pathlib import edge-case (some linters flag as unused)
