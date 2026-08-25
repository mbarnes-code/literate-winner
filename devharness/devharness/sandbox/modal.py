"""Modal cloud sandbox backend — scaffold.

No upstream code copied. Design-only reference to the Modal SDK docs
(https://modal.com/docs) which are not vendored here.
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable design points:
  - Ships as an optional extra: ``pip install devharness[modal]``. The
    ``modal`` package is imported lazily inside ``_app()`` so the
    module imports cleanly on installs without the extra.
  - This file is intentionally a scaffold: the concrete Modal ``App`` +
    ``Sandbox`` plumbing lands with the cloud-backend milestone
    (spec §12, Milestone 6). Every method that would touch the Modal
    API raises ``NotImplementedError`` with a pointer to the TODO.
"""
from __future__ import annotations

from typing import Any

from devharness.sandbox.protocol import (
    SandboxBackend,
    SandboxClass,
    SandboxRequest,
    SandboxResult,
)


class ModalBackend:
    """Scaffold Modal backend.

    Satisfies the ``SandboxBackend`` Protocol so the factory can construct
    it, but ``execute`` refuses until the cloud plumbing is wired.
    """

    name = "modal"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.app_name: str = cfg.get("app_name", "devharness")
        self.image_tag: str = cfg.get("image", "python:3.12-slim")
        self._app: Any = None  # populated lazily by _app()

    def _app(self) -> Any:
        """Lazy-import ``modal`` and construct the App on first use.

        TODO(milestone-6): build the ``modal.App`` with an image derived
        from the workspace lockfile, mount the cwd as a ``Volume`` or
        ``NetworkFileSystem``, and cache the App across sessions.
        """
        if self._app is not None:
            return self._app
        try:
            import modal  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "modal backend requires the 'modal' package: "
                "pip install devharness[modal]"
            ) from e
        self._app = modal.App(self.app_name)
        return self._app

    def supports(self, sandbox_class: SandboxClass) -> bool:
        # Report False until the cloud plumbing lands so the factory
        # falls back to docker/local rather than silently refusing at
        # execute() time.
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
                "modal backend scaffold — cloud plumbing not wired yet; "
                "see TODO(milestone-6) in devharness/sandbox/modal.py"
            ),
        )


__all__ = ["ModalBackend"]
