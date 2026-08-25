"""Sandbox runner — execute the planned tool batch (spec §4.6).

No upstream code copied — design from spec §4.6.
The step consumes ``ctx.planned_batches`` (produced by ``ToolDispatch`` /
``plan_batches``), invokes ``SandboxBackend.execute`` for each call, and
emits one ``AppendMessage(role="tool", ...)`` per result — the shape the
provider expects for the next inference turn.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from devharness.loop.effects import AppendMessage, Applied, NotApplicable, StepResult
from devharness.loop.state import ThreadState, TurnContext


def _tool_call_id(call: Any) -> str:
    if isinstance(call, Mapping):
        return str(call.get("id") or call.get("tool_call_id") or "")
    return str(getattr(call, "id", "") or getattr(call, "tool_call_id", ""))


def _tool_name(call: Any) -> str:
    if isinstance(call, Mapping):
        fn = call.get("function") or {}
        return str(fn.get("name") or call.get("name") or "")
    fn = getattr(call, "function", None)
    if fn is not None:
        return str(getattr(fn, "name", "") or "")
    return str(getattr(call, "name", "") or "")


def _tool_args(call: Any) -> dict[str, Any]:
    raw: Any
    if isinstance(call, Mapping):
        fn = call.get("function") or {}
        raw = fn.get("arguments", call.get("arguments"))
    else:
        fn = getattr(call, "function", None)
        raw = getattr(fn, "arguments", None) if fn is not None else getattr(call, "arguments", None)
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {"_raw": raw}
        return parsed if isinstance(parsed, dict) else {"_value": parsed}
    if isinstance(raw, Mapping):
        return dict(raw)
    return {"_value": raw}


class SandboxRunner:
    """Run each planned tool call through the sandbox and append results."""

    name = "SandboxRunner"

    def __init__(self, backend: Any, tool_specs: Mapping[str, Any] | None = None) -> None:
        # ``tool_specs`` maps ``tool_name -> ToolSpec`` (spec §4.4). We only
        # need ``.sandbox_class`` and ``.handler`` for dispatch.
        self._backend = backend
        self._tool_specs = dict(tool_specs or {})

    def apply(self, state: ThreadState, ctx: TurnContext) -> StepResult:
        batches = ctx.planned_batches
        if not batches:
            return NotApplicable

        # Lazy import to avoid pulling the whole sandbox package unless used.
        from devharness.sandbox.protocol import SandboxClass, SandboxRequest

        effects: list = []
        for call in batches:
            name = _tool_name(call)
            call_id = _tool_call_id(call)
            args = _tool_args(call)
            spec = self._tool_specs.get(name)
            handler = getattr(spec, "handler", None) if spec is not None else None

            if handler is not None:
                # In-process tool — no sandbox process needed.
                try:
                    result = handler(**args)
                    content = result if isinstance(result, str) else json.dumps(result, default=str)
                except Exception as exc:  # surface failure to the model
                    content = f"[tool {name!r} raised {type(exc).__name__}: {exc}]"
            else:
                # External command — dispatch via SandboxBackend.
                sandbox_class = getattr(spec, "sandbox_class", None) or SandboxClass.READ_ONLY
                argv = args.get("argv") or args.get("command") or [name]
                if isinstance(argv, str):
                    argv = [argv]
                req = SandboxRequest(
                    argv=tuple(str(a) for a in argv),
                    cwd=Path(ctx.cwd) if ctx.cwd else Path.cwd(),
                    sandbox_class=sandbox_class,
                    session_id=state.session_id,
                    label=name,
                )
                result = self._backend.execute(req)
                stdout = getattr(result, "stdout", b"") or b""
                stderr = getattr(result, "stderr", b"") or b""
                content_bytes = stdout + (b"\n---STDERR---\n" + stderr if stderr else b"")
                content = content_bytes.decode("utf-8", errors="replace")

            ctx.tool_results.append({"tool_call_id": call_id, "name": name, "content": content})
            effects.append(AppendMessage(
                role="tool",
                content=content,
                name=name or None,
                tool_call_id=call_id or None,
            ))

        # Clear planned batches so the step is idempotent within a turn.
        ctx.planned_batches = ()
        return Applied(tuple(effects))


__all__ = ["SandboxRunner"]
