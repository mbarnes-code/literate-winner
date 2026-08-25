"""Daytona cloud sandbox backend — scaffold.

No upstream code copied. Design-only reference to the Daytona SDK docs
(https://www.daytona.io/docs) which are not vendored here.
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable design points:
  - Ships as an optional extra: ``pip install devharness[daytona]``. The
    ``daytona_sdk`` package is imported lazily inside ``_client()`` so
    this module imports cleanly without the extra installed.
  - Scaffold only: concrete workspace / preview URL plumbing lands with
    the cloud-backend milestone (spec §12, Milestone 6). Methods that
    would touch the Daytona control plane return a refusal.
"""
from __future__ import annotations

from typing import Any

from devharness.sandbox.protocol import (
    SandboxBackend,
    SandboxClass,
    SandboxRequest,
    SandboxResult,
)


class DaytonaBackend:
    """Scaffold Daytona backend.

    Satisfies the ``SandboxBackend`` Protocol; ``execute`` refuses until
    the cloud plumbing is wired.
    """

    name = "daytona"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.server_url: str = cfg.get("server_url", "")
        self.api_key_env: str = cfg.get("api_key_env", "DAYTONA_API_KEY")
        self._client_obj: Any = None

    def _client(self) -> Any:
        """Lazy-import the Daytona SDK and construct the client.

        TODO(milestone-6): build the client from
        ``daytona_sdk.Daytona(config=...)``, create per-session
        workspaces, and route ``execute`` through ``workspace.exec``.
        """
        if self._client_obj is not None:
            return self._client_obj
        try:
            import daytona_sdk  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "daytona backend requires the 'daytona_sdk' package: "
                "pip install devharness[daytona]"
            ) from e
        self._client_obj = daytona_sdk  # placeholder; real client below.
        return self._client_obj

    def supports(self, sandbox_class: SandboxClass) -> bool:
        # Report False until cloud plumbing lands; factory falls back.
        return False

    def execute(self, req: SandboxRequest) -> SandboxResult:
        return SandboxResult(
            exit_code=126,
            stdout=b"",
            stderr=b"",
            duration_ms=0,
            timed_out=False,
            backend=self.name,
            refusal_reason=(
                "daytona backend scaffold — cloud plumbing not wired yet; "
                "see TODO(milestone-6) in devharness/sandbox/daytona.py"
            ),
        )


__all__ = ["DaytonaBackend"]
