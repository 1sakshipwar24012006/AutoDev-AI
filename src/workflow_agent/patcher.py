"""Safe, atomic, reversible application of model-proposed file edits."""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from workflow_agent.models import ChangeSet
from workflow_agent.security import (
    DEFAULT_PROTECTED_GLOBS,
    PathPolicyError,
    is_test_path,
    to_rel_posix,
    validate_relative_path,
)


class PatchError(Exception):
    """The change set could not be applied safely."""


class PatchPolicy(BaseModel):
    max_files: int = 10
    max_bytes_per_file: int = 200_000
    protected_globs: tuple[str, ...] = DEFAULT_PROTECTED_GLOBS
    allow_edit_existing_tests: bool = False
    allow_new_files: bool = True


@dataclass
class AppliedPatch:
    root: Path
    touched: list[str]
    diff: str
    _backups: dict[Path, bytes | None] = field(default_factory=dict)
    _created_dirs: list[Path] = field(default_factory=list)

    def rollback(self) -> None:
        """Restore every touched file to its original bytes and remove created files/dirs."""
        for path, original in self._backups.items():
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(original)
            except OSError as exc:  # pragma: no cover - best effort
                raise PatchError(f"rollback failed for {path}: {exc}") from exc
        for directory in self._created_dirs:
            try:
                directory.rmdir()
            except OSError:
                pass


def _normalize_eol(text: str, crlf: bool) -> str:
    text = text.replace("\r\n", "\n")
    return text.replace("\n", "\r\n") if crlf else text


def _lines(text: str) -> list[str]:
    lines = re.findall(r"[^\n]*\n|[^\n]+", text)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n\\ No newline at end of file\n"
    return lines


def _file_diff(rel: str, old: str | None, new: str) -> str:
    fromfile = "/dev/null" if old is None else f"a/{rel}"
    body = "".join(
        difflib.unified_diff(_lines(old or ""), _lines(new), fromfile=fromfile, tofile=f"b/{rel}")
    )
    if not body:
        return ""
    header = f"diff --git a/{rel} b/{rel}\n"
    if old is None:
        header += "new file mode 100644\n"
    return header + body


def _missing_dirs(path: Path) -> list[Path]:
    missing: list[Path] = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    return missing  # deepest first


def apply_patch(root: Path, changeset: ChangeSet, policy: PatchPolicy | None = None) -> AppliedPatch:
    """Validate all edits in memory, then write them atomically. Roll back on any failure."""
    policy = policy or PatchPolicy()
    root = root.resolve()

    originals: dict[Path, str | None] = {}
    state: dict[Path, str | None] = {}
    crlf: dict[Path, bool] = {}
    rels: dict[Path, str] = {}
    order: list[Path] = []

    def load(path: Path, rel: str) -> None:
        if path in state:
            return
        order.append(path)
        rels[path] = rel
        if path.exists():
            if not path.is_file():
                raise PatchError(f"{rel}: not a regular file")
            try:
                text = path.read_bytes().decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PatchError(f"{rel}: binary or non-UTF-8 file cannot be edited") from exc
            originals[path] = text
            state[path] = text
            crlf[path] = "\r\n" in text
        else:
            originals[path] = None
            state[path] = None
            crlf[path] = False

    for index, edit in enumerate(changeset.edits, start=1):
        try:
            path = validate_relative_path(root, edit.path, policy.protected_globs)
        except PathPolicyError as exc:
            raise PatchError(f"edit {index}: {exc}") from exc
        rel = to_rel_posix(root, path)
        load(path, rel)
        existed = originals[path] is not None

        if len(order) > policy.max_files:
            raise PatchError(f"change set touches more than {policy.max_files} files")

        if existed and is_test_path(rel) and not policy.allow_edit_existing_tests:
            raise PatchError(
                f"edit {index}: modifying existing test file {rel} is not allowed "
                "(add a new test file instead, or pass --allow-edit-existing-tests)"
            )

        current = state[path]
        if edit.operation == "create":
            if current is not None:
                raise PatchError(f"edit {index}: {rel} already exists; use replace_text or overwrite")
            if not policy.allow_new_files:
                raise PatchError(f"edit {index}: creating new files is disabled")
            state[path] = edit.content or ""
        elif edit.operation == "overwrite":
            if current is None and not policy.allow_new_files:
                raise PatchError(f"edit {index}: creating new files is disabled")
            state[path] = _normalize_eol(edit.content or "", crlf[path])
        else:  # replace_text
            if current is None:
                raise PatchError(f"edit {index}: {rel} does not exist; cannot replace_text")
            old = _normalize_eol(edit.old_text or "", crlf[path])
            new = _normalize_eol(edit.new_text or "", crlf[path])
            count = current.count(old)
            if count == 0:
                raise PatchError(
                    f"edit {index}: old_text not found in {rel}; copy it exactly from the provided file"
                )
            if count > 1:
                raise PatchError(
                    f"edit {index}: old_text occurs {count} times in {rel}; include more surrounding "
                    "lines so it is unique"
                )
            state[path] = current.replace(old, new, 1)

        size = len((state[path] or "").encode("utf-8"))
        if size > policy.max_bytes_per_file:
            raise PatchError(f"edit {index}: {rel} would be {size} bytes (limit {policy.max_bytes_per_file})")

    changed = [p for p in order if state[p] != originals[p]]
    if not changed:
        raise PatchError("change set produces no effective changes")

    diff = "".join(_file_diff(rels[p], originals[p], state[p] or "") for p in changed)
    applied = AppliedPatch(
        root=root,
        touched=[rels[p] for p in changed],
        diff=diff,
        _backups={p: (originals[p].encode("utf-8") if originals[p] is not None else None) for p in changed},
    )

    try:
        for path in changed:
            missing = _missing_dirs(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            applied._created_dirs.extend(missing)
            tmp = path.with_name(path.name + ".agent-tmp")
            tmp.write_bytes((state[path] or "").encode("utf-8"))
            os.replace(tmp, path)
    except OSError as exc:
        applied.rollback()
        raise PatchError(f"failed to write files: {exc}") from exc
    return applied
