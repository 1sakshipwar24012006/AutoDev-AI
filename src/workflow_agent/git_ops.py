"""Git operations with hard safety rails: no force-push, no pushes to protected branches."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable

from workflow_agent.security import (
    PathPolicyError,
    is_protected_branch,
    validate_branch_name,
    validate_relative_path,
)
from workflow_agent.shell import CommandError, CommandResult, run_command

log = logging.getLogger("workflow_agent")
Runner = Callable[..., CommandResult]


class GitSafetyError(Exception):
    """An operation was refused by the git safety policy."""


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "change"


class GitOps:
    def __init__(self, root: Path, runner: Runner = run_command) -> None:
        self.root = root.resolve()
        self._run = runner

    def _git(self, *args: str, check: bool = True, timeout: float = 60) -> CommandResult:
        return self._run(["git", *args], cwd=self.root, timeout=timeout, pass_secrets=True, check=check)

    # -- inspection ---------------------------------------------------------
    def is_repo(self) -> bool:
        try:
            r = self._git("rev-parse", "--is-inside-work-tree", check=False)
        except CommandError:
            return False
        return r.ok and r.stdout.strip() == "true"

    def toplevel(self) -> Path:
        return Path(self._git("rev-parse", "--show-toplevel").stdout.strip()).resolve()

    def has_commits(self) -> bool:
        return self._git("rev-parse", "--verify", "-q", "HEAD", check=False).ok

    def current_branch(self) -> str | None:
        r = self._git("symbolic-ref", "--short", "-q", "HEAD", check=False)
        return r.stdout.strip() if r.ok and r.stdout.strip() else None

    def is_clean(self) -> bool:
        """True if there are no modifications to tracked files (untracked files are ignored)."""
        return self._git("status", "--porcelain", "--untracked-files=no").stdout.strip() == ""

    def branch_exists(self, name: str) -> bool:
        return self._git("show-ref", "--verify", "--quiet", f"refs/heads/{name}", check=False).ok

    def has_remote(self, name: str = "origin") -> bool:
        return self._git("remote", "get-url", name, check=False).ok

    def head_sha(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    # -- branch management --------------------------------------------------
    def unique_branch_name(self, name: str) -> str:
        validate_branch_name(name)
        candidate, n = name, 2
        while self.branch_exists(candidate):
            candidate = f"{name}-{n}"
            n += 1
        return candidate

    def create_branch(self, name: str) -> None:
        validate_branch_name(name)
        if is_protected_branch(name):
            raise GitSafetyError(f"refusing to create protected branch name {name!r}")
        self._git("checkout", "-b", name)

    def checkout(self, name: str) -> None:
        validate_branch_name(name)
        self._git("checkout", name)

    def delete_branch(self, name: str) -> None:
        validate_branch_name(name)
        if is_protected_branch(name):
            raise GitSafetyError(f"refusing to delete protected branch {name!r}")
        self._git("branch", "-D", name)

    # -- commit / push ------------------------------------------------------
    def commit(self, paths: list[str], message: str) -> str:
        """Stage exactly ``paths`` (validated) and commit. Never uses ``git add -A``."""
        if not paths:
            raise GitSafetyError("nothing to commit")
        for rel in paths:
            try:
                validate_relative_path(self.root, rel)
            except PathPolicyError as exc:
                raise GitSafetyError(f"refusing to stage {rel!r}: {exc}") from exc
        self._git("add", "--", *paths)
        self._git("commit", "-m", message)
        return self.head_sha()

    def assert_pushable(self, branch: str) -> None:
        validate_branch_name(branch)
        if is_protected_branch(branch):
            raise GitSafetyError(f"refusing to push protected branch {branch!r}")

    def push(self, branch: str) -> None:
        """Push ``branch`` to origin with an explicit refspec. No force, no protected branches."""
        self.assert_pushable(branch)
        self._git(
            "push", "--set-upstream", "origin", f"refs/heads/{branch}:refs/heads/{branch}", timeout=180
        )


def format_commit_message(subject: str, issue_number: int | None, explanation: str) -> str:
    subject = " ".join(subject.split())[:72] or "Apply automated fix"
    body = explanation.strip()
    parts = [subject]
    if body:
        parts.append(body)
    if issue_number is not None:
        parts.append(f"Refs #{issue_number}")
    parts.append("Generated-by: autonomous-coding-workflow-agent")
    return "\n\n".join(parts)
