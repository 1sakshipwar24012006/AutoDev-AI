"""Security helpers: secret redaction, path validation, branch and command policy."""

from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path, PureWindowsPath
from typing import Iterable, Sequence


class PathPolicyError(ValueError):
    """Raised when a path (or branch / command) violates the safety policy."""


# --------------------------------------------------------------------------- #
# Secret redaction
# --------------------------------------------------------------------------- #

REDACTED = "[REDACTED]"
_REGISTERED: set[str] = set()

_TOKEN_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
)
_AUTH_HEADER = re.compile(r"(?i)(authorization:\s*(?:bearer|token)\s+)[A-Za-z0-9._\-]{8,}")


def register_secret(value: str | None) -> None:
    """Remember a secret value so it is scrubbed from all logs and reports."""
    if value and len(value) >= 8:
        _REGISTERED.add(value)


def clear_registered_secrets() -> None:
    _REGISTERED.clear()


def redact(text: str, extra: Iterable[str] = ()) -> str:
    """Replace known secret values and common token formats with a placeholder."""
    if not text:
        return text
    secrets = {s for s in (*_REGISTERED, *extra) if s}
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _AUTH_HEADER.sub(lambda m: m.group(1) + REDACTED, text)
    return text


# --------------------------------------------------------------------------- #
# Path policy
# --------------------------------------------------------------------------- #

# Files the agent may never write.
DEFAULT_PROTECTED_GLOBS: tuple[str, ...] = (
    ".git",
    ".git/*",
    "*/.git/*",
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.kdbx",
    "id_rsa*",
    "id_ed25519*",
    ".ssh/*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials.json",
    "*credentials*.json",
    "service-account*.json",
    ".github/workflows/*",
)

# Files never sent to the model (secrets). Workflows are fine to read.
SENSITIVE_READ_GLOBS: tuple[str, ...] = tuple(g for g in DEFAULT_PROTECTED_GLOBS if g != ".github/workflows/*")

# Template files that are safe to read and edit.
SAFE_TEMPLATE_NAMES = frozenset({".env.example", ".env.sample", ".env.template"})

_TEST_GLOBS = (
    "tests/*",
    "test/*",
    "*/tests/*",
    "*/test/*",
    "__tests__/*",
    "*/__tests__/*",
    "test_*.py",
    "*/test_*.py",
    "*_test.py",
    "conftest.py",
    "*/conftest.py",
    "*.test.*",
    "*.spec.*",
)

_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)

PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "dev", "trunk", "production", "prod", "staging", "head"})
PROTECTED_BRANCH_PREFIXES = ("release/", "releases/", "hotfix/")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")


def _match_any(rel_posix: str, globs: Iterable[str]) -> bool:
    lowered = rel_posix.lower()
    base = lowered.rsplit("/", 1)[-1]
    for glob in globs:
        g = glob.lower()
        if fnmatch.fnmatchcase(lowered, g) or fnmatch.fnmatchcase(base, g):
            return True
    return False


def is_sensitive_file(rel_posix: str) -> bool:
    """True if the file likely holds secrets and must never be sent to the model."""
    base = rel_posix.lower().rsplit("/", 1)[-1]
    if base in SAFE_TEMPLATE_NAMES:
        return False
    return _match_any(rel_posix, SENSITIVE_READ_GLOBS)


def is_test_path(rel_posix: str) -> bool:
    return _match_any(rel_posix, _TEST_GLOBS)


def validate_relative_path(
    root: Path,
    rel: str,
    protected_globs: Sequence[str] = DEFAULT_PROTECTED_GLOBS,
    *,
    allow_protected: bool = False,
) -> Path:
    """Validate a model-supplied relative path and return its resolved absolute path.

    Rejects empty paths, NUL bytes, absolute/drive/UNC paths, ``..`` traversal,
    symlink escapes, Windows reserved names, ``.git`` internals and protected files.
    """
    if not isinstance(rel, str) or not rel.strip():
        raise PathPolicyError("empty path")
    if "\x00" in rel:
        raise PathPolicyError("NUL byte in path")
    cleaned = rel.strip().replace("\\", "/")
    if len(cleaned) > 300:
        raise PathPolicyError("path too long")
    if cleaned.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", cleaned) or PureWindowsPath(rel).drive:
        raise PathPolicyError(f"absolute paths are not allowed: {rel!r}")
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if not parts:
        raise PathPolicyError(f"path resolves to the repository root: {rel!r}")
    if any(p == ".." for p in parts):
        raise PathPolicyError(f"path traversal is not allowed: {rel!r}")
    for part in parts:
        if _WINDOWS_RESERVED.match(part) or part.endswith((" ", ".")):
            raise PathPolicyError(f"illegal path component {part!r} in {rel!r}")

    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*parts)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise PathPolicyError(f"path escapes the repository: {rel!r}")
    if candidate.is_symlink():
        raise PathPolicyError(f"refusing to write through a symlink: {rel!r}")
    rel_posix = resolved.relative_to(root_resolved).as_posix()
    if rel_posix == ".":
        raise PathPolicyError(f"path resolves to the repository root: {rel!r}")

    if not allow_protected:
        lowered_parts = [p.lower() for p in rel_posix.split("/")]
        if ".git" in lowered_parts:
            raise PathPolicyError(f"git internals are protected: {rel!r}")
        base = lowered_parts[-1]
        if base not in SAFE_TEMPLATE_NAMES and _match_any(rel_posix, protected_globs):
            raise PathPolicyError(f"protected file: {rel!r}")
    return resolved


def to_rel_posix(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


# --------------------------------------------------------------------------- #
# Branch and command policy
# --------------------------------------------------------------------------- #

def validate_branch_name(name: str) -> str:
    if not isinstance(name, str) or not _BRANCH_RE.match(name):
        raise PathPolicyError(f"invalid branch name: {name!r}")
    if ".." in name or name.endswith(("/", ".", ".lock")) or "//" in name or "@{" in name:
        raise PathPolicyError(f"invalid branch name: {name!r}")
    return name


def is_protected_branch(name: str) -> bool:
    lowered = name.lower()
    return lowered in PROTECTED_BRANCHES or lowered.startswith(PROTECTED_BRANCH_PREFIXES)


_PYTHON_NAMES = {"python", "python3", "py"}
_ALLOWED_TEST_EXECUTABLES = _PYTHON_NAMES | {"pytest"}


def validate_test_command(command: Sequence[str]) -> list[str]:
    """Allow only python/pytest test commands (no shell). Bare ``python`` -> this interpreter."""
    if not command or not all(isinstance(c, str) and c for c in command):
        raise PathPolicyError("test command must be a non-empty list of strings")
    if any("\x00" in c or "\n" in c for c in command):
        raise PathPolicyError("test command contains illegal characters")
    exe = command[0]
    base = Path(exe).name.lower()
    if base.endswith(".exe"):
        base = base[:-4]
    if base not in _ALLOWED_TEST_EXECUTABLES:
        raise PathPolicyError(
            f"test executable {exe!r} is not allowed; use python, python3, py or pytest"
        )
    args = list(command)
    if base in _PYTHON_NAMES and not Path(exe).is_absolute():
        rest = args[1:]
        if base == "py" and rest and re.fullmatch(r"-\d(\.\d+)?", rest[0]):
            rest = rest[1:]
        return [sys.executable, *rest]
    return args
