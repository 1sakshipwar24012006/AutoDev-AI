"""AI code generation stage."""

from __future__ import annotations

from pathlib import Path

from workflow_agent.llm import LLMClient
from workflow_agent.models import ChangeSet, Feedback, Issue, Plan, TestRunResult
from workflow_agent.prompts import CODEGEN_SYSTEM, build_codegen_prompt
from workflow_agent.repo_analyzer import RepoContext, read_text_file


def gather_files(
    root: Path, plan: Plan, ctx: RepoContext, *, max_chars: int, max_file_bytes: int
) -> dict[str, str]:
    """Plan files first (read fresh from disk), then other relevant context up to the budget."""
    files: dict[str, str] = {}
    budget = max_chars
    for rel in plan.files_to_modify:
        text = read_text_file(root, rel, max_file_bytes)
        if text is not None:
            files[rel] = text
            budget -= len(text)
    for rel, text in ctx.files.items():
        if rel in files:
            continue
        if budget - len(text) < 0:
            continue
        files[rel] = text
        budget -= len(text)
    return files


def generate_changes(
    llm: LLMClient,
    issue: Issue,
    plan: Plan,
    files: dict[str, str],
    baseline: TestRunResult | None,
    feedback: list[Feedback],
) -> ChangeSet:
    return llm.structured(
        stage="codegen",
        system=CODEGEN_SYSTEM,
        user=build_codegen_prompt(issue, plan, files, baseline, feedback),
        schema=ChangeSet,
    )
