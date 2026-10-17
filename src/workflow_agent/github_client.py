"""GitHub access through the official `gh` CLI. There is deliberately no merge capability."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Callable

from workflow_agent.models import Issue
from workflow_agent.shell import CommandError, CommandResult, run_command

Runner = Callable[..., CommandResult]
_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
_URL_RE = re.compile(r"https://\S+")


class GitHubError(Exception):
    pass


class GitHubClient:
    def __init__(self, cwd: Path, repo: str | None = None, runner: Runner = run_command) -> None:
        if repo is not None and not _REPO_RE.match(repo):
            raise GitHubError(f"invalid GitHub repository {repo!r}; expected 'owner/name'")
        self.cwd = cwd
        self.repo = repo
        self._run = runner

    def _gh(self, *args: str, timeout: float = 90) -> CommandResult:
        argv = ["gh", *args]
        if self.repo:
            argv += ["--repo", self.repo]
        try:
            return self._run(argv, cwd=self.cwd, timeout=timeout, pass_secrets=True, check=True)
        except CommandError as exc:
            raise GitHubError(str(exc)) from exc

    def check_ready(self) -> None:
        try:
            result = self._run(["gh", "auth", "status"], cwd=self.cwd, timeout=30, pass_secrets=True)
        except CommandError as exc:
            raise GitHubError(f"GitHub CLI unavailable: {exc}. Install it from https://cli.github.com/") from exc
        if not result.ok:
            raise GitHubError("GitHub CLI is not authenticated. Run `gh auth login` first.")

    def fetch_issue(self, number: int) -> Issue:
        if not isinstance(number, int) or number <= 0:
            raise GitHubError("issue number must be a positive integer")
        result = self._gh("issue", "view", str(number), "--json", "number,title,body,labels,url")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubError(f"unexpected output from gh: {exc}") from exc
        return Issue(
            number=data.get("number"),
            title=data.get("title") or f"Issue #{number}",
            body=data.get("body") or "",
            url=data.get("url"),
            labels=[lbl.get("name", "") for lbl in data.get("labels", []) if isinstance(lbl, dict)],
        )

    def create_pull_request(self, *, title: str, body: str, base: str, head: str, draft: bool = False) -> str:
        """Open a PR (never merges). Returns the PR URL."""
        with tempfile.TemporaryDirectory() as tmp:
            body_file = Path(tmp) / "pr_body.md"
            body_file.write_text(body, encoding="utf-8")
            args = [
                "pr", "create",
                f"--title={title}",
                f"--body-file={body_file}",
                f"--base={base}",
                f"--head={head}",
            ]
            if draft:
                args.append("--draft")
            result = self._gh(*args, timeout=120)
        urls = _URL_RE.findall(result.stdout)
        return urls[-1] if urls else result.stdout.strip()
