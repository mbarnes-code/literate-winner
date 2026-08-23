"""Layered config loader: TOML (with YAML fallback), env expansion, overlay.

Ported from vvaharness/config/__init__.py
  License: Apache License 2.0
  Copyright 2026 Visa, Inc.
  Source: https://github.com/visa/visa-vulnerability-agentic-harness
  Upstream commit: 3d972f679d8f5e3838b394edee0b5ea9c626b0fb
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable adaptations:
  - Primary loader is TOML via stdlib ``tomllib``; ``.yaml``/``.yml`` paths
    fall back to PyYAML if it is importable (soft-imported at use site).
  - Dropped VVAH's built-in _STEP_DEFAULTS, step1 overlay helpers
    (apply_step1_overlay / _append_merge / _replace_merge), the
    is_network_path guard, and the profile-registration machinery.
    Only the reusable primitives are ported: _ENV_PAT, _expand_env,
    _expand, _deep_merge, Config, load().
  - ``load(path)`` reads exactly the file passed in; layering with
    hardcoded defaults, ``~/.devharness/config.toml``, and profile
    overlays is left to a caller that composes ``_deep_merge`` calls.
  - Overlay filename is ``config.local.toml`` (or ``.yaml``) instead of
    ``config.local.yaml``; opt-out env var renamed to
    ``DEVHARNESS_NO_LOCAL_CONFIG``.
"""
from __future__ import annotations
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

_ENV_PAT = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")

# Overlay paths already announced this process — load() may be called several
# times per run; dedupe so the config.local.* provenance line is emitted once
# per unique overlay, not once per load() call.
_logged_overlays: set[str] = set()


def _expand_env(m: re.Match) -> str:
    # POSIX ${VAR:-default}: use the default when VAR is unset OR set-but-empty.
    name, default = m.group(1), m.group(2)
    val = os.environ.get(name)
    if val:
        return val
    return default if default is not None else ""


def _expand(val: Any) -> Any:
    if isinstance(val, str):
        return _ENV_PAT.sub(_expand_env, val)
    if isinstance(val, dict):
        return {k: _expand(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_expand(v) for v in val]
    return val


class Config:
    """Thin attribute wrapper over the config dict. ``cfg.provider.model`` etc."""

    def __init__(self, data: dict):
        self._data = data

    def __getattr__(self, name: str) -> Any:
        # Guard against recursion during copy.deepcopy: _data and dunder probes
        # must NOT re-enter via self._data, which is absent while the object is
        # being reconstructed.
        if (name.startswith("__") and name.endswith("__")) or name == "_data":
            raise AttributeError(name)
        data = self.__dict__.get("_data")
        try:
            v = data[name]
        except (KeyError, TypeError) as e:
            raise AttributeError(name) from e
        return Config(v) if isinstance(v, dict) else v

    def __getitem__(self, k: str) -> Any:
        return self._data[k]

    def __contains__(self, k: str) -> bool:
        return k in self._data

    def __repr__(self) -> str:
        return f"Config({self._data!r})"


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_mapping(path: Path) -> dict:
    """Parse *path* as TOML, or YAML if it ends in .yaml/.yml. YAML support
    requires the optional ``pyyaml`` dependency."""
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                f"config {path} is YAML but PyYAML is not installed"
            ) from e
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    if not isinstance(raw, dict):
        raise ValueError(
            f"config {path} must be a mapping, got {type(raw).__name__}"
        )
    return raw


def load(path: str | Path = "config.toml") -> Config:
    """Load *path* (TOML by default, YAML if suffix is ``.yaml``/``.yml``),
    expand ``${VAR}`` / ``${VAR:-default}`` placeholders in every string leaf,
    and overlay any sibling ``config.local.{toml,yaml,yml}`` file.

    Set ``DEVHARNESS_NO_LOCAL_CONFIG`` to any value to skip the overlay
    (reproducible run honouring only the operator-selected config).
    """
    p = Path(path)
    raw = _read_mapping(p)

    # Look for a sibling config.local.{toml,yaml,yml}; the first that exists
    # wins. Overlay can override security-relevant keys, so the merge is made
    # VISIBLE rather than silent: log the overlay file and the top-level keys
    # it overrides.
    local_candidates = [
        p.with_name("config.local.toml"),
        p.with_name("config.local.yaml"),
        p.with_name("config.local.yml"),
    ]
    local = next((c for c in local_candidates if c.exists()), None)
    if local is not None:
        key = str(local.resolve())
        if os.environ.get("DEVHARNESS_NO_LOCAL_CONFIG"):
            if key not in _logged_overlays:
                _logged_overlays.add(key)
                print(
                    f"  config overlay: {local} present but SKIPPED "
                    f"(DEVHARNESS_NO_LOCAL_CONFIG set)",
                    file=sys.stderr,
                )
        else:
            over = _read_mapping(local)
            if key not in _logged_overlays:
                _logged_overlays.add(key)
                overrides = ", ".join(sorted(map(str, over))) or "(empty)"
                print(
                    f"  config overlay: {local} applied "
                    f"(overrides: {overrides})",
                    file=sys.stderr,
                )
            raw = _deep_merge(raw, over)

    cfg = Config(_expand(raw))
    # Record the directory the config was loaded from so input-style paths
    # can be resolved against it.
    cfg._data["_config_dir"] = str(p.resolve().parent)
    return cfg


__all__ = ["Config", "load", "_expand", "_expand_env", "_deep_merge", "_ENV_PAT"]
