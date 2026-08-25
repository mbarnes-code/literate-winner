"""schema — JSON-schema generation from Python type hints.

No upstream code copied — Hermes' tools ship hand-written schemas and
its ``schema_sanitizer`` only rewrites existing JSON schemas; VVAH has
no equivalent. Spec §4.4 requires parameters to be auto-derived from
type hints, so this module walks ``inspect.signature`` + ``typing``
metadata and emits the OpenAI function-calling shape:

    {
      "type": "function",
      "function": {
        "name":        <str>,
        "description": <str>,
        "parameters": {"type": "object", "properties": {...},
                       "required": [...], "additionalProperties": false},
      },
    }

Supported hint kinds: ``str``, ``int``, ``float``, ``bool``,
``list[T]``, ``dict[str, T]``, ``Optional[T]``, ``Literal[...]``, and
``pydantic.BaseModel`` subclasses (``.model_json_schema()`` inlined).
Unknown types fall through to ``{"type": "string"}`` with the original
name recorded under ``x-devharness-fallback``.
"""
from __future__ import annotations

import inspect
import typing
from typing import Any, Callable, get_args, get_origin


_PRIMITIVES: dict[type, dict[str, Any]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    type(None): {"type": "null"},
}


def _is_pydantic_model(cls: Any) -> bool:
    try:
        from pydantic import BaseModel  # type: ignore[import-not-found]
    except ImportError:
        return False
    return isinstance(cls, type) and issubclass(cls, BaseModel)


def _optional_inner(tp: Any) -> Any | None:
    """If tp is Optional[T] (aka Union[T, None]) return T else None."""
    origin = get_origin(tp)
    if origin is typing.Union or (origin is not None and origin.__name__ == "UnionType"):
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return None


def type_to_schema(tp: Any) -> dict[str, Any]:
    """Convert one type hint into a JSON-schema fragment."""
    if tp in _PRIMITIVES:
        return dict(_PRIMITIVES[tp])
    if tp is Any or tp is inspect.Parameter.empty:
        return {}
    inner = _optional_inner(tp)
    if inner is not None:
        base = type_to_schema(inner)
        base.setdefault("nullable", True)
        return base
    origin = get_origin(tp)
    if origin is typing.Literal:
        vals = list(get_args(tp))
        # collapse if all values share a JSON-primitive type
        py_types = {type(v) for v in vals}
        if py_types <= {str, int, float, bool}:
            enum_type = next(iter(py_types), str)
            base = dict(_PRIMITIVES[enum_type])
            base["enum"] = vals
            return base
        return {"enum": vals}
    if origin in (list, tuple, set, frozenset):
        args = get_args(tp)
        item = type_to_schema(args[0]) if args else {}
        return {"type": "array", "items": item}
    if origin is dict:
        args = get_args(tp)
        val_schema = type_to_schema(args[1]) if len(args) == 2 else {}
        return {"type": "object", "additionalProperties": val_schema}
    if _is_pydantic_model(tp):
        try:
            return tp.model_json_schema()
        except Exception:  # pragma: no cover - pydantic edge
            return {"type": "object"}
    return {"type": "string", "x-devharness-fallback": repr(tp)}


def build_schema(fn: Callable[..., Any], *,
                 name: str | None = None,
                 description: str | None = None) -> dict[str, Any]:
    """Build an OpenAI function-calling schema for ``fn``."""
    sig = inspect.signature(fn)
    try:
        hints = typing.get_type_hints(fn)
    except Exception:  # pragma: no cover - unresolvable hint
        hints = {}
    props: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        tp = hints.get(pname, param.annotation)
        props[pname] = type_to_schema(tp)
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    doc = description if description is not None else (
        inspect.getdoc(fn) or ""
    ).strip()
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": props,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {
            "name": name or fn.__name__,
            "description": doc,
            "parameters": parameters,
        },
    }
