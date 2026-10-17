"""AI planning stage."""

from __future__ import annotations

import logging
from pathlib import Path

from workflow_agent.llm import LLMClient
from workflow_agent.models import Issue, Plan, TestRunResult
from workflow_agent.prompts import PLAN_SYSTEM, build_plan_prompt
from workflow_agent.repo_analyzer import RepoContext
from workflow_agent.security import PathPolicyError, validate_relative_path

log = logging.getLogger("workflow_agent")


def make_plan(
    llm: LLMClient, root: Path, issue: Issue, ctx: RepoContext, baseline: TestRunResult | None
) -> Plan:
    plan = llm.structured(
        stage="plan",
        system=PLAN_SYSTEM,
        user=build_plan_prompt(issue, ctx.tree, ctx.files, baseline),
        schema=Plan,
    )
    safe_files: list[str] = []
    for rel in plan.files_to_modify:
        try:
            validate_relative_path(root, rel)
            safe_files.append(rel.replace("\\", "/").lstrip("./"))
        except PathPolicyError as exc:
            log.warning("plan listed an unsafe path and it was dropped: %s", exc)
    plan.files_to_modify = safe_files
    return plan
