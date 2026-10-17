"""Create a throw-away git repository from the bundled buggy sample app."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from workflow_agent.shell import run_command


def find_sample_app() -> Path:
    candidate = Path(__file__).resolve().parents[2] / "sample_app"
    if not (candidate / "shop" / "pricing.py").is_file():
        raise FileNotFoundError(
            "sample_app/ not found. Run from a source checkout of the project (or `pip install -e .`)."
        )
    return candidate


def create_demo_repo(dest: Path | None = None) -> Path:
    """Copy sample_app into ``dest`` (or a temp dir), then ``git init`` + initial commit on ``main``."""
    sample = find_sample_app()
    base = dest or Path(tempfile.mkdtemp(prefix="agent-demo-"))
    repo = base / "demo-repo" if dest is None else base
    shutil.copytree(
        sample, repo,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "issue.md", "*.pyc"),
    )

    def git(*args: str) -> None:
        run_command(["git", *args], cwd=repo, timeout=60, pass_secrets=True, check=True)

    git("init", "-q")
    git("symbolic-ref", "HEAD", "refs/heads/main")
    git("config", "user.name", "Demo User")
    git("config", "user.email", "demo@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("add", "-A")
    git("commit", "-q", "-m", "Initial commit: sample shop app (with bugs)")
    return repo


def sample_issue_path() -> Path:
    return find_sample_app() / "issue.md"
