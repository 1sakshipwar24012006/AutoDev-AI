"""Pydantic data models shared across the workflow."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Issue(BaseModel):
    number: int | None = None
    title: str = Field(min_length=1)
    body: str = ""
    url: str | None = None
    labels: list[str] = Field(default_factory=list)

    def as_text(self) -> str:
        label_line = f"\nLabels: {', '.join(self.labels)}" if self.labels else ""
        return f"Title: {self.title}{label_line}\n\n{self.body}".strip()


class Plan(BaseModel):
    summary: str = Field(description="One or two sentences describing the intended fix.")
    root_cause: str = Field(description="Most likely root cause, grounded in the provided code.")
    files_to_modify: list[str] = Field(
        default_factory=list,
        description="Repo-relative POSIX paths of files to change or create.",
    )
    steps: list[str] = Field(min_length=1, description="Ordered implementation steps.")
    test_strategy: str = Field(description="How the change will be verified.")
    risks: list[str] = Field(default_factory=list)


EditOperation = Literal["replace_text", "create", "overwrite"]


class FileEdit(BaseModel):
    """A single safe file operation.

    - ``replace_text``: ``old_text`` must occur exactly once in the file; it is replaced by ``new_text``.
    - ``create``: write ``content`` to a new file (fails if it exists).
    - ``overwrite``: replace the whole file with ``content``.
    """

    path: str = Field(min_length=1)
    operation: EditOperation
    old_text: str | None = None
    new_text: str | None = None
    content: str | None = None

    @model_validator(mode="after")
    def _check_fields(self) -> "FileEdit":
        if self.operation == "replace_text":
            if not self.old_text:
                raise ValueError("replace_text requires a non-empty old_text")
            if self.new_text is None:
                raise ValueError("replace_text requires new_text (use an empty string to delete)")
            if self.content is not None:
                raise ValueError("replace_text must not set content")
        else:
            if self.content is None:
                raise ValueError(f"{self.operation} requires content")
            if self.old_text is not None or self.new_text is not None:
                raise ValueError(f"{self.operation} must not set old_text/new_text")
        return self


class ChangeSet(BaseModel):
    explanation: str = Field(description="What changed and why.")
    commit_message: str = Field(min_length=1, description="Imperative commit subject, <= 72 chars.")
    edits: list[FileEdit] = Field(min_length=1)


Severity = Literal["blocker", "major", "minor", "nit"]


class ReviewConcern(BaseModel):
    severity: Severity
    message: str


class Review(BaseModel):
    approved: bool
    summary: str
    concerns: list[ReviewConcern] = Field(default_factory=list)

    @model_validator(mode="after")
    def _blockers_reject(self) -> "Review":
        if any(c.severity in ("blocker", "major") for c in self.concerns):
            self.approved = False
        return self


class TestRunResult(BaseModel):
    __test__ = False  # not a pytest test class

    command: str
    returncode: int | None
    passed: bool
    output: str
    duration_seconds: float = 0.0
    timed_out: bool = False
    note: str | None = None

    def short(self) -> str:
        status = "PASSED" if self.passed else "FAILED"
        extra = f" ({self.note})" if self.note else ""
        return f"{status} rc={self.returncode} in {self.duration_seconds:.1f}s{extra}"


class Feedback(BaseModel):
    """Why a previous attempt was rejected; fed into the next generation prompt."""

    attempt: int
    kind: Literal["patch_error", "tests_failed", "review_rejected"]
    detail: str
    previous_edits: str = ""


class AttemptRecord(BaseModel):
    attempt: int
    stage_reached: Literal["generate", "patch", "tests", "review", "accepted"] = "generate"
    changeset: ChangeSet | None = None
    diff: str = ""
    tests: TestRunResult | None = None
    review: Review | None = None
    error: str | None = None


class RunReport(BaseModel):
    run_id: str
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    success: bool = False
    dry_run: bool = False
    mock: bool = False
    repo: str = ""
    issue: Issue | None = None
    plan: Plan | None = None
    baseline_tests: TestRunResult | None = None
    attempts: list[AttemptRecord] = Field(default_factory=list)
    branch: str | None = None
    base_branch: str | None = None
    commit_sha: str | None = None
    pr_url: str | None = None
    final_diff: str = ""
    error: str | None = None
    llm_usage: dict[str, int] = Field(default_factory=dict)
    output_dir: str | None = None
    notes: list[str] = Field(default_factory=list)
