"""Repository inspection: file listing, safe reading and relevance ranking."""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from workflow_agent.security import is_sensitive_file, is_test_path
from workflow_agent.shell import CommandError, run_command

log = logging.getLogger("workflow_agent")

IGNORED_DIRS = frozenset(
    {
        ".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".idea", ".vscode",
        "agent_runs", "site-packages", ".eggs", ".cache",
    }
)
_STOPWORDS = frozenset(
    "the and for with that this from have has not are but when what should would could into "
    "than then there their about after before issue bug fix error returns return instead "
    "expected actual because while using used".split()
)
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_PATH_MENTION_RE = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]{1,5}")


@dataclass
class RepoContext:
    tree: str
    files: dict[str, str] = field(default_factory=dict)
    omitted: list[str] = field(default_factory=list)
    all_paths: list[str] = field(default_factory=list)


def read_text_file(root: Path, rel: str, max_bytes: int) -> str | None:
    """Read a UTF-8 text file inside ``root``; None if missing, binary, sensitive or too large."""
    if is_sensitive_file(rel):
        return None
    path = root / rel
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > max_bytes:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def list_repo_files(root: Path) -> list[str]:
    """Tracked + untracked-but-not-ignored files (git) or a filtered walk (non-git)."""
    root = root.resolve()
    paths: list[str] = []
    if (root / ".git").exists():
        try:
            result = run_command(
                ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                cwd=root,
                timeout=60,
                pass_secrets=True,
                max_output=50_000_000,
            )
            if result.ok:
                paths = [p for p in result.stdout.split("\0") if p]
        except CommandError:
            paths = []
    if not paths:
        for current, dirs, files in _walk(root):
            for name in files:
                paths.append((current / name).relative_to(root).as_posix())
    cleaned: list[str] = []
    for rel in sorted(set(paths)):
        parts = rel.split("/")
        if any(p in IGNORED_DIRS or p.endswith(".egg-info") for p in parts[:-1]):
            continue
        if is_sensitive_file(rel) or (root / rel).is_symlink() or not (root / rel).is_file():
            continue
        cleaned.append(rel)
    return cleaned


def _walk(root: Path):
    import os

    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.endswith(".egg-info")]
        yield Path(current), dirs, files


def _tokens(text: str) -> Counter:
    return Counter(t.lower() for t in _TOKEN_RE.findall(text) if t.lower() not in _STOPWORDS)


def rank_files(root: Path, paths: list[str], issue_text: str, max_file_bytes: int) -> list[tuple[float, str, str]]:
    issue_tokens = _tokens(issue_text)
    mentioned = {m.replace("\\", "/").lstrip("./") for m in _PATH_MENTION_RE.findall(issue_text)}
    ranked: list[tuple[float, str, str]] = []
    for rel in paths:
        text = read_text_file(root, rel, max_file_bytes)
        if text is None:
            continue
        score = 0.0
        if any(rel == m or rel.endswith("/" + m) for m in mentioned):
            score += 100
        for token in _tokens(rel.replace("/", " ").replace(".", " ").replace("_", " ")):
            if token in issue_tokens:
                score += 4
        content_tokens = _tokens(text)
        score += sum(min(content_tokens[t], 5) for t in issue_tokens if t in content_tokens)
        if is_test_path(rel):
            score *= 0.9
        ranked.append((score, rel, text))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked


def analyze_repo(
    root: Path,
    issue_text: str,
    *,
    max_chars: int = 60000,
    max_file_bytes: int = 50000,
    max_tree_lines: int = 400,
) -> RepoContext:
    root = root.resolve()
    paths = list_repo_files(root)
    tree_lines = paths[:max_tree_lines]
    tree = "\n".join(tree_lines)
    if len(paths) > max_tree_lines:
        tree += f"\n... ({len(paths) - max_tree_lines} more files)"

    ctx = RepoContext(tree=tree, all_paths=paths)
    budget = max_chars
    for score, rel, text in rank_files(root, paths, issue_text, max_file_bytes):
        if budget - len(text) < 0 and ctx.files:
            ctx.omitted.append(rel)
            continue
        if score <= 0 and len(ctx.files) >= 3:
            ctx.omitted.append(rel)
            continue
        ctx.files[rel] = text
        budget -= len(text)
    log.debug("analyzed %d files; %d included in context", len(paths), len(ctx.files))
    return ctx
