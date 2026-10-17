"""AI self-review stage."""

from __future__ import annotations

from workflow_agent.llm import LLMClient
from workflow_agent.models import ChangeSet, Issue, Plan, Review, TestRunResult
from workflow_agent.prompts import REVIEW_SYSTEM, build_review_prompt


def review_changes(
    llm: LLMClient, issue: Issue, plan: Plan, changeset: ChangeSet, diff: str, tests: TestRunResult
) -> Review:
    return llm.structured(
        stage="review",
        system=REVIEW_SYSTEM,
        user=build_review_prompt(issue, plan, changeset, diff, tests),
        schema=Review,
    )
