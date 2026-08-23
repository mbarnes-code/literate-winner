"""V4A patch grammar parser and filesystem apply.

Ported from hermes-agent/tools/patch_parser.py
  License: MIT License
  Copyright (c) 2025 Nous Research
  Source: https://github.com/NousResearch/hermes-agent
  Upstream commit: f293e7206b4ddd66042329442c6afebc19a8808d
See NOTICE and THIRD_PARTY_LICENSES.md at project root.

Notable adaptations:
  - Dropped LSP diagnostics, lint plumbing, and PatchResult objects.
  - Inlined a minimal fuzzy_find_and_replace (strict + whitespace-tolerant),
    replacing the tools.fuzzy_match dependency.
  - Removed hermes tools.file_operations dependency; apply operates directly
    against a root directory on the local filesystem.
  - Public surface reduced to parse_v4a_patch, apply_v4a_operations, and a
    convenience apply(patch_text, root_dir) entry point.
  - Registration decorator omitted — see TODO below (devharness registry
    signature differs; wire up when tools/impl/git is finalized).
"""

from __future__ import annotations

import difflib
import re
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional, Tuple


# TODO: register with devharness.tools.registry once impl/git tools are wired
# (spec §4.4 register(name="apply_patch", category="git", sandbox_class="MF",
# approval_class="S", timeout=30, parallel_safe=False)).


class OperationType(Enum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    MOVE = "move"


@dataclass
class HunkLine:
    prefix: str  # ' ', '-', or '+'
    content: str


@dataclass
class Hunk:
    context_hint: Optional[str] = None
    lines: List[HunkLine] = field(default_factory=list)


@dataclass
class PatchOperation:
    operation: OperationType
    file_path: str
    new_path: Optional[str] = None  # MOVE only
    hunks: List[Hunk] = field(default_factory=list)


@dataclass
class ApplyReport:
    success: bool
    files_created: List[str] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
    files_deleted: List[str] = field(default_factory=list)
    diff: str = ""
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_BEGIN_MARKER = re.compile(r'^\*\*\*\s*Begin\s+Patch\s*$')
_END_MARKER = re.compile(r'^\*\*\*\s*End\s+Patch\s*$')
_UPDATE = re.compile(r'\*\*\*\s*Update\s+File:\s*(.+)')
_ADD = re.compile(r'\*\*\*\s*Add\s+File:\s*(.+)')
_DELETE = re.compile(r'\*\*\*\s*Delete\s+File:\s*(.+)')
_MOVE = re.compile(r'\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)')


def parse_v4a_patch(patch_content: str) -> Tuple[List[PatchOperation], Optional[str]]:
    """Parse a V4A format patch.

    Returns (operations, error_message). Error is None on success.
    """
    # Tolerate CRLF-encoded patches by stripping trailing \r on every line.
    lines = [ln[:-1] if ln.endswith('\r') else ln for ln in patch_content.split('\n')]
    operations: List[PatchOperation] = []

    start_idx: Optional[int] = None
    end_idx: Optional[int] = None
    for i, line in enumerate(lines):
        if _BEGIN_MARKER.match(line):
            start_idx = i
        elif _END_MARKER.match(line):
            end_idx = i
            break

    if start_idx is None:
        start_idx = -1
    if end_idx is None:
        end_idx = len(lines)

    i = start_idx + 1
    current_op: Optional[PatchOperation] = None
    current_hunk: Optional[Hunk] = None

    while i < end_idx:
        line = lines[i]

        upd = _UPDATE.match(line)
        add = _ADD.match(line)
        dele = _DELETE.match(line)
        mov = _MOVE.match(line)

        if upd:
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)
            current_op = PatchOperation(OperationType.UPDATE, upd.group(1).strip())
            current_hunk = None
        elif add:
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)
            current_op = PatchOperation(OperationType.ADD, add.group(1).strip())
            current_hunk = Hunk()
        elif dele:
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)
            operations.append(PatchOperation(OperationType.DELETE, dele.group(1).strip()))
            current_op = None
            current_hunk = None
        elif mov:
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)
            operations.append(PatchOperation(
                OperationType.MOVE,
                mov.group(1).strip(),
                new_path=mov.group(2).strip(),
            ))
            current_op = None
            current_hunk = None
        elif line.startswith('@@'):
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                hint_match = re.match(r'@@\s*(.+?)\s*@@', line)
                current_hunk = Hunk(context_hint=hint_match.group(1) if hint_match else None)
        elif current_op and line:
            if current_hunk is None:
                current_hunk = Hunk()
            if line.startswith('+'):
                current_hunk.lines.append(HunkLine('+', line[1:]))
            elif line.startswith('-'):
                current_hunk.lines.append(HunkLine('-', line[1:]))
            elif line.startswith(' '):
                current_hunk.lines.append(HunkLine(' ', line[1:]))
            elif line.startswith('\\'):
                pass  # "\ No newline at end of file"
            else:
                current_hunk.lines.append(HunkLine(' ', line))

        i += 1

    if current_op:
        if current_hunk and current_hunk.lines:
            current_op.hunks.append(current_hunk)
        operations.append(current_op)

    if not operations:
        return operations, None

    errors: List[str] = []
    for op in operations:
        if not op.file_path:
            errors.append("Operation with empty file path")
        if op.operation == OperationType.UPDATE and not op.hunks:
            errors.append(f"UPDATE {op.file_path!r}: no hunks found")
        if op.operation == OperationType.MOVE and not op.new_path:
            errors.append(f"MOVE {op.file_path!r}: missing destination (expected 'src -> dst')")

    if errors:
        return [], "Parse error: " + "; ".join(errors)
    return operations, None


# ---------------------------------------------------------------------------
# Minimal fuzzy find-and-replace
# ---------------------------------------------------------------------------

def _fuzzy_find_and_replace(
    text: str,
    search: str,
    replacement: str,
    *,
    replace_all: bool = False,
) -> Tuple[str, int, str, Optional[str]]:
    """Attempt strict, then whitespace-tolerant, find/replace.

    Returns (new_text, count, strategy, error). ``strategy`` is one of
    ``"strict"``, ``"ws"``, or ``""`` (no match).
    """
    if not search:
        return text, 0, "", "empty search pattern"

    # 1) Strict literal match.
    if search in text:
        if replace_all:
            new = text.replace(search, replacement)
            return new, text.count(search), "strict", None
        return text.replace(search, replacement, 1), 1, "strict", None

    # 2) Whitespace-tolerant: collapse runs of whitespace inside the search
    # pattern into ``\s+`` and try again. Preserves indentation intent while
    # forgiving trailing-space and tab/space drift.
    def _escape_ws_tolerant(s: str) -> str:
        parts = re.split(r'(\s+)', s)
        out: List[str] = []
        for part in parts:
            if part and part.strip() == "":
                out.append(r'\s+')
            else:
                out.append(re.escape(part))
        return "".join(out)

    pattern = _escape_ws_tolerant(search)
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return text, 0, "", f"regex compile failed: {exc}"

    matches = list(rx.finditer(text))
    if not matches:
        return text, 0, "", "search text not found"
    if not replace_all:
        m = matches[0]
        return text[:m.start()] + replacement + text[m.end():], 1, "ws", None
    # replace_all path
    new = rx.sub(lambda _m: replacement, text)
    return new, len(matches), "ws", None


def _is_already_applied(text: str, search: str, replacement: str) -> bool:
    """True when replacement is already present and search text is gone."""
    return replacement in text and search not in text


def _count_occurrences(text: str, pattern: str) -> int:
    count = 0
    start = 0
    while True:
        pos = text.find(pattern, start)
        if pos == -1:
            return count
        count += 1
        start = pos + 1


# ---------------------------------------------------------------------------
# Filesystem-backed apply
# ---------------------------------------------------------------------------

class _RootedFS:
    """Minimal filesystem interface rooted at *root* (jailed against escapes)."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            raise ValueError(f"Path escapes root: {rel!r}")
        return p

    def read_text(self, rel: str) -> Optional[str]:
        try:
            return self._resolve(rel).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def exists(self, rel: str) -> bool:
        try:
            return self._resolve(rel).exists()
        except ValueError:
            return False

    def write_text(self, rel: str, content: str) -> None:
        p = self._resolve(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def delete(self, rel: str) -> None:
        self._resolve(rel).unlink()

    def move(self, src_rel: str, dst_rel: str) -> None:
        src = self._resolve(src_rel)
        dst = self._resolve(dst_rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))


def _validate(operations: List[PatchOperation], fs: _RootedFS) -> List[str]:
    """Dry-run validation; returns list of errors (empty means safe to apply)."""
    errors: List[str] = []
    pending: dict = {}          # rel_path -> post-op content
    removed: set = set()
    real_change_count = 0

    def _read(rel: str) -> Tuple[Optional[str], Optional[str]]:
        if rel in removed and rel not in pending:
            return None, "file not found"
        if rel in pending:
            return pending[rel], None
        content = fs.read_text(rel)
        if content is None:
            return None, "file not found"
        return content, None

    for op in operations:
        if op.operation != OperationType.UPDATE:
            real_change_count += 1

        if op.operation == OperationType.UPDATE:
            content, err = _read(op.file_path)
            if err:
                errors.append(f"{op.file_path}: {err}")
                continue
            simulated = content or ""
            for idx, hunk in enumerate(op.hunks, start=1):
                search_lines = [l.content for l in hunk.lines if l.prefix in {' ', '-'}]
                removed_lines = [l.content for l in hunk.lines if l.prefix == '-']
                added_lines = [l.content for l in hunk.lines if l.prefix == '+']
                if not removed_lines and not added_lines:
                    continue
                real_change_count += 1
                if not search_lines:
                    if hunk.context_hint:
                        occ = _count_occurrences(simulated, hunk.context_hint)
                        if occ == 0:
                            errors.append(
                                f"{op.file_path}: addition-only hunk context hint "
                                f"'{hunk.context_hint}' not found"
                            )
                        elif occ > 1:
                            errors.append(
                                f"{op.file_path}: addition-only hunk context hint "
                                f"'{hunk.context_hint}' is ambiguous ({occ} occurrences)"
                            )
                    continue

                search_pattern = '\n'.join(search_lines)
                replace_lines = [l.content for l in hunk.lines if l.prefix in {' ', '+'}]
                replacement = '\n'.join(replace_lines)
                if search_lines == replace_lines:
                    continue

                new_simulated, count, _strategy, match_error = _fuzzy_find_and_replace(
                    simulated, search_pattern, replacement, replace_all=False
                )
                if count == 0:
                    if _is_already_applied(simulated, search_pattern, replacement):
                        continue
                    label = f"'{hunk.context_hint}'" if hunk.context_hint else "(no hint)"
                    err_bits = f"{op.file_path}: hunk {idx} {label} not found"
                    if match_error:
                        err_bits += f" — {match_error}"
                    errors.append(err_bits)
                else:
                    simulated = new_simulated
            pending[op.file_path] = simulated

        elif op.operation == OperationType.DELETE:
            _c, err = _read(op.file_path)
            if err:
                errors.append(f"{op.file_path}: file not found for deletion")
            else:
                removed.add(op.file_path)
                pending.pop(op.file_path, None)

        elif op.operation == OperationType.MOVE:
            if not op.new_path:
                errors.append(f"{op.file_path}: MOVE missing destination path")
                continue
            src_content, src_err = _read(op.file_path)
            if src_err:
                errors.append(f"{op.file_path}: source file not found for move")
            _dst_content, dst_err = _read(op.new_path)
            if not dst_err:
                errors.append(
                    f"{op.new_path}: destination already exists — move would overwrite"
                )
            if not src_err and dst_err:
                pending[op.new_path] = src_content or ""
                pending.pop(op.file_path, None)
                removed.add(op.file_path)

        # ADD needs no pre-check; write_text creates parents.

    if not errors and real_change_count == 0:
        errors.append("Patch contains no changes (only context lines were provided)")
    return errors


def _apply_update(op: PatchOperation, fs: _RootedFS) -> Tuple[bool, str]:
    current = fs.read_text(op.file_path)
    if current is None:
        return False, f"Cannot read file: {op.file_path}"

    new_content = current
    for hunk in op.hunks:
        search_lines: List[str] = []
        replace_lines: List[str] = []
        for line in hunk.lines:
            if line.prefix == ' ':
                search_lines.append(line.content)
                replace_lines.append(line.content)
            elif line.prefix == '-':
                search_lines.append(line.content)
            elif line.prefix == '+':
                replace_lines.append(line.content)

        if search_lines and search_lines == replace_lines:
            continue

        if search_lines:
            search_pattern = '\n'.join(search_lines)
            replacement = '\n'.join(replace_lines)
            new_content, count, _strategy, err = _fuzzy_find_and_replace(
                new_content, search_pattern, replacement, replace_all=False
            )
            if count == 0:
                if _is_already_applied(new_content, search_pattern, replacement):
                    continue
                return False, f"Could not apply hunk: {err or 'not found'}"
        else:
            insert_text = '\n'.join(replace_lines)
            if hunk.context_hint:
                occ = _count_occurrences(new_content, hunk.context_hint)
                if occ == 0:
                    new_content = new_content.rstrip('\n') + '\n' + insert_text + '\n'
                elif occ > 1:
                    return False, (
                        f"Addition-only hunk: context hint '{hunk.context_hint}' "
                        f"is ambiguous ({occ} occurrences)"
                    )
                else:
                    hint_pos = new_content.find(hunk.context_hint)
                    eol = new_content.find('\n', hint_pos)
                    if eol != -1:
                        new_content = new_content[:eol + 1] + insert_text + '\n' + new_content[eol + 1:]
                    else:
                        new_content = new_content + '\n' + insert_text
            else:
                new_content = new_content.rstrip('\n') + '\n' + insert_text + '\n'

    fs.write_text(op.file_path, new_content)
    diff = ''.join(difflib.unified_diff(
        current.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{op.file_path}",
        tofile=f"b/{op.file_path}",
    ))
    return True, diff


def _apply_add(op: PatchOperation, fs: _RootedFS) -> Tuple[bool, str]:
    content_lines: List[str] = [
        line.content for hunk in op.hunks for line in hunk.lines if line.prefix == '+'
    ]
    content = '\n'.join(content_lines)
    if fs.exists(op.file_path):
        return False, f"Add target already exists: {op.file_path}"
    fs.write_text(op.file_path, content)
    diff = f"--- /dev/null\n+++ b/{op.file_path}\n"
    diff += '\n'.join(f"+{ln}" for ln in content_lines)
    return True, diff


def _apply_delete(op: PatchOperation, fs: _RootedFS) -> Tuple[bool, str]:
    prior = fs.read_text(op.file_path)
    if prior is None:
        return False, f"Cannot delete {op.file_path}: file not found"
    fs.delete(op.file_path)
    diff = ''.join(difflib.unified_diff(
        prior.splitlines(keepends=True), [],
        fromfile=f"a/{op.file_path}", tofile="/dev/null",
    ))
    return True, diff or f"# Deleted: {op.file_path}"


def _apply_move(op: PatchOperation, fs: _RootedFS) -> Tuple[bool, str]:
    assert op.new_path is not None
    fs.move(op.file_path, op.new_path)
    return True, f"# Moved: {op.file_path} -> {op.new_path}"


def apply_v4a_operations(
    operations: List[PatchOperation],
    root: Any,
) -> ApplyReport:
    """Apply parsed V4A operations against a filesystem root.

    Uses validate-then-apply: if any operation fails the dry-run check, no
    files are modified.
    """
    fs = _RootedFS(Path(root))
    errors = _validate(operations, fs)
    if errors:
        return ApplyReport(
            success=False,
            error="Patch validation failed (no files were modified):\n"
                  + "\n".join(f"  - {e}" for e in errors),
        )

    created: List[str] = []
    modified: List[str] = []
    deleted: List[str] = []
    diffs: List[str] = []
    apply_errors: List[str] = []

    for op in operations:
        try:
            if op.operation == OperationType.ADD:
                ok, out = _apply_add(op, fs)
                (created if ok else apply_errors).append(op.file_path if ok else f"add {op.file_path}: {out}")
                if ok:
                    diffs.append(out)
            elif op.operation == OperationType.DELETE:
                ok, out = _apply_delete(op, fs)
                if ok:
                    deleted.append(op.file_path); diffs.append(out)
                else:
                    apply_errors.append(f"delete {op.file_path}: {out}")
            elif op.operation == OperationType.MOVE:
                ok, out = _apply_move(op, fs)
                if ok:
                    modified.append(f"{op.file_path} -> {op.new_path}"); diffs.append(out)
                else:
                    apply_errors.append(f"move {op.file_path}: {out}")
            elif op.operation == OperationType.UPDATE:
                ok, out = _apply_update(op, fs)
                if ok:
                    modified.append(op.file_path); diffs.append(out)
                else:
                    apply_errors.append(f"update {op.file_path}: {out}")
        except Exception as exc:  # defensive — filesystem race, permission, etc.
            apply_errors.append(f"{op.file_path}: {exc}")

    report = ApplyReport(
        success=not apply_errors,
        files_created=created,
        files_modified=modified,
        files_deleted=deleted,
        diff='\n'.join(diffs),
    )
    if apply_errors:
        report.error = (
            "Apply phase failed (state may be inconsistent — run `git diff` to assess):\n"
            + "\n".join(f"  - {e}" for e in apply_errors)
        )
    return report


def apply(patch_text: str, root_dir: Any) -> ApplyReport:
    """Parse *patch_text* and apply it to *root_dir* in one call."""
    operations, error = parse_v4a_patch(patch_text)
    if error:
        return ApplyReport(success=False, error=error)
    if not operations:
        return ApplyReport(success=True)
    return apply_v4a_operations(operations, root_dir)


__all__ = [
    "OperationType",
    "HunkLine",
    "Hunk",
    "PatchOperation",
    "ApplyReport",
    "parse_v4a_patch",
    "apply_v4a_operations",
    "apply",
]
