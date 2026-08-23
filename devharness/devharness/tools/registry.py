"""Tool registry with self-registration and AST-based discovery.

Ported from hermes-agent/tools/registry.py
  License: MIT License
  Copyright (c) 2025 Nous Research
  Source: https://github.com/NousResearch/hermes-agent
  Upstream commit: f293e7206b4ddd66042329442c6afebc19a8808d
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable adaptations:
  - Dropped ~1000 LOC of hermes-specific machinery: plugin override policies,
    MCP dynamic refresh, check_fn TTL cache with flake suppression, toolset
    aliases, tool_error/tool_result JSON serializers, per-profile scoped
    overlays, and hermes_constants profile scoping.
  - Replaced ToolEntry (schema/handler/check_fn/emoji/etc.) with a devharness
    ToolSpec dataclass matching spec §4.4: name, category, sandbox_class,
    approval_class, timeout, parallel_safe, handler.
  - AST discovery walks devharness.tools.impl.* subpackages instead of a flat
    tools/*.py directory; detects both bare register(...) and @register(...)
    module-level call expressions.
  - Discovery cache still keyed on (mtime_ns, size); path defaults to
    ~/.cache/devharness/tool_discovery.json when DEVHARNESS_HOME is unset.
"""

from __future__ import annotations

import ast
import importlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ToolSpec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolSpec:
    """Registration record for one tool. See spec §4.4."""

    name: str
    category: str                # e.g. "git", "shell", "fs", "network"
    sandbox_class: str           # e.g. "MF" (must-fence), "SF", "NF"
    approval_class: str          # e.g. "N" (none), "S" (soft), "H" (hard)
    timeout: float               # seconds; enforced by executor
    parallel_safe: bool
    handler: Optional[Callable] = None
    description: str = ""
    schema: Dict = field(default_factory=dict)

    def summary(self) -> Dict[str, object]:
        """Deterministic dict for `frozen_specs()` (excludes callable handler)."""
        return {
            "name": self.name,
            "category": self.category,
            "sandbox_class": self.sandbox_class,
            "approval_class": self.approval_class,
            "timeout": self.timeout,
            "parallel_safe": self.parallel_safe,
            "description": self.description,
        }


# ---------------------------------------------------------------------------
# AST-based discovery
# ---------------------------------------------------------------------------

_REGISTER_NAMES = frozenset({"register", "@register"})


def _is_registry_register_call(node: ast.AST) -> bool:
    """True when *node* is a top-level ``register(...)`` or ``@register`` decorator use.

    Detects both call styles:
        register(name="foo", ...)                    # module-level call
        registry.register(name="foo", ...)           # attribute call
        @register("foo", ...)                        # decorator on def/class
    """
    # 1) Plain module-level expression: register(...) or registry.register(...)
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        return _call_is_register(node.value)

    # 2) Decorated function/class at module scope.
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        for deco in node.decorator_list:
            if isinstance(deco, ast.Call) and _call_is_register(deco):
                return True
            if isinstance(deco, ast.Name) and deco.id == "register":
                return True
            if isinstance(deco, ast.Attribute) and deco.attr == "register":
                return True
    return False


def _call_is_register(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name) and func.id == "register":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "register":
        return True
    return False


def _module_registers_tools(module_path: Path) -> bool:
    """Return True if the module has any top-level register(...) call/decorator."""
    try:
        source = module_path.read_text(encoding="utf-8")
    except OSError:
        return False
    if "register" not in source:
        return False
    try:
        tree = ast.parse(source, filename=str(module_path))
    except SyntaxError:
        return False
    return any(_is_registry_register_call(stmt) for stmt in tree.body)


def _iter_tool_modules(root: Path):
    """Yield (module_path, dotted_module_name) for candidate tool source files.

    Walks the given root (assumed to be ``devharness/tools/impl``) and
    surfaces every ``*.py`` file except ``__init__.py`` and dunder files.
    The dotted name is inferred from the relative path so nested
    subpackages (e.g. impl/git/apply_patch.py) work transparently.
    """
    for path in sorted(root.rglob("*.py")):
        if path.name.startswith("__"):
            continue
        rel = path.relative_to(root.parent.parent).with_suffix("")
        # rel like tools/impl/git/apply_patch -> devharness.tools.impl.git.apply_patch
        dotted = "devharness." + ".".join(rel.parts)
        yield path, dotted


# ---------------------------------------------------------------------------
# Discovery cache
# ---------------------------------------------------------------------------

def _discovery_cache_path() -> Optional[Path]:
    home = os.environ.get("DEVHARNESS_HOME")
    if home:
        return Path(home) / "cache" / "tool_discovery.json"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "devharness" / "tool_discovery.json"


def _load_discovery_cache() -> Dict[str, list]:
    path = _discovery_cache_path()
    if path is None:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_discovery_cache(cache: Dict[str, list]) -> None:
    path = _discovery_cache_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug("Could not write tool discovery cache %s: %s", path, exc)


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Process-global tool registry.

    Public API:
      * ``register(spec)`` / decorator-style ``register(name=..., ...)``
      * ``get_tool(name)``
      * ``list_tools()``
      * ``discover(path=None)``
      * ``frozen_specs()`` — sorted deterministic snapshot
    """

    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}
        self._lock = threading.RLock()
        self._generation: int = 0

    # ---- registration ----

    def register_spec(self, spec: ToolSpec, *, override: bool = False) -> ToolSpec:
        """Register a fully-formed ToolSpec object."""
        with self._lock:
            existing = self._tools.get(spec.name)
            if existing is not None and not override:
                logger.error(
                    "Tool registration REJECTED: %r already registered "
                    "(category=%s). Pass override=True to replace.",
                    spec.name, existing.category,
                )
                return existing
            self._tools[spec.name] = spec
            self._generation += 1
            return spec

    def register(
        self,
        name: Optional[str] = None,
        *,
        category: str = "",
        sandbox_class: str = "NF",
        approval_class: str = "N",
        timeout: float = 30.0,
        parallel_safe: bool = False,
        description: str = "",
        schema: Optional[Dict] = None,
        override: bool = False,
    ):
        """Register a tool. Usable as a decorator or as a direct call.

        Decorator form::

            @registry.register(name="apply_patch", category="git",
                               sandbox_class="MF", approval_class="S",
                               timeout=30, parallel_safe=False)
            def apply_patch(...): ...

        Direct form::

            registry.register("apply_patch", category="git",
                              sandbox_class="MF", approval_class="S",
                              timeout=30, parallel_safe=False,
                              schema={...})(handler)
        """
        def _decorator(handler: Callable) -> Callable:
            resolved_name = name or getattr(handler, "__name__", "")
            if not resolved_name:
                raise ValueError("register(): tool name is required")
            spec = ToolSpec(
                name=resolved_name,
                category=category,
                sandbox_class=sandbox_class,
                approval_class=approval_class,
                timeout=timeout,
                parallel_safe=parallel_safe,
                handler=handler,
                description=description or (handler.__doc__ or "").strip().splitlines()[0]
                if handler.__doc__ else description,
                schema=dict(schema or {}),
            )
            self.register_spec(spec, override=override)
            return handler

        return _decorator

    # ---- lookup ----

    def get_tool(self, name: str) -> Optional[ToolSpec]:
        with self._lock:
            return self._tools.get(name)

    def list_tools(self) -> List[str]:
        with self._lock:
            return sorted(self._tools.keys())

    def all_specs(self) -> List[ToolSpec]:
        with self._lock:
            return sorted(self._tools.values(), key=lambda s: s.name)

    def frozen_specs(self) -> List[Dict[str, object]]:
        """Deterministic sorted list of spec summaries (no handlers)."""
        return [s.summary() for s in self.all_specs()]

    @property
    def generation(self) -> int:
        return self._generation

    # ---- discovery ----

    def discover(self, path: Optional[Path] = None) -> List[str]:
        """Import every tool module under *path* that self-registers.

        *path* defaults to ``devharness/tools/impl`` relative to this file.
        Verdicts are memoized on disk keyed by ``(mtime_ns, size)`` per file.
        """
        tools_impl = Path(path) if path else Path(__file__).resolve().parent / "impl"
        if not tools_impl.exists():
            logger.debug("Tool discovery: no impl directory at %s", tools_impl)
            return []

        cache = _load_discovery_cache()
        fresh_cache: Dict[str, list] = {}
        cache_dirty = False

        module_names: List[str] = []
        for module_path, dotted in _iter_tool_modules(tools_impl):
            abs_key = str(module_path.resolve())
            try:
                st = module_path.stat()
            except OSError:
                continue
            stat_key = (st.st_mtime_ns, st.st_size)
            cached = cache.get(abs_key)
            if (
                isinstance(cached, (list, tuple))
                and len(cached) == 3
                and (cached[0], cached[1]) == stat_key
            ):
                registers = bool(cached[2])
            else:
                registers = _module_registers_tools(module_path)
                cache_dirty = True
            fresh_cache[abs_key] = [stat_key[0], stat_key[1], registers]
            if registers:
                module_names.append(dotted)

        if cache_dirty or set(fresh_cache) != set(cache):
            _save_discovery_cache(fresh_cache)

        imported: List[str] = []
        for mod_name in module_names:
            try:
                importlib.import_module(mod_name)
                imported.append(mod_name)
            except Exception as exc:
                logger.warning("Could not import tool module %s: %s", mod_name, exc)
        return imported


# ---------------------------------------------------------------------------
# Module-level singleton + convenience re-exports
# ---------------------------------------------------------------------------

registry = ToolRegistry()


def register(*args, **kwargs):
    """Module-level shortcut for ``registry.register(...)``."""
    return registry.register(*args, **kwargs)


def get_tool(name: str) -> Optional[ToolSpec]:
    return registry.get_tool(name)


def list_tools() -> List[str]:
    return registry.list_tools()


def discover(path: Optional[Path] = None) -> List[str]:
    return registry.discover(path)


def frozen_specs() -> List[Dict[str, object]]:
    return registry.frozen_specs()


__all__ = [
    "ToolSpec",
    "ToolRegistry",
    "registry",
    "register",
    "get_tool",
    "list_tools",
    "discover",
    "frozen_specs",
]
