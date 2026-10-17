"""Load issues from local Markdown / JSON files."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from workflow_agent.models import Issue


class IssueError(Exception):
    pass


def load_issue_file(path: str | Path) -> Issue:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise IssueError(f"cannot read issue file {p}: {exc}") from exc
    if p.suffix.lower() == ".json":
        try:
            return Issue.model_validate_json(text)
        except ValidationError as exc:
            raise IssueError(f"invalid issue JSON: {exc}") from exc
    lines = text.splitlines()
    title = ""
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip():
            title = line.lstrip("# ").strip()
            body_start = i + 1
            break
    if not title:
        raise IssueError(f"issue file {p} is empty")
    return Issue(title=title, body="\n".join(lines[body_start:]).strip())
