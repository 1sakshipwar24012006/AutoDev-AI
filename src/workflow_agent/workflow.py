"""Orchestrates: issue -> analysis -> plan -> generate -> patch -> test -> review -> commit -> PR."""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from workflow_agent.codegen import gather_files, generate_changes
from workflow_agent.config import Settings
from workflow_agent.git_ops import GitOps, GitSafetyError, format_commit_message, slugify
from workflow_agent.github_client import GitHubClient, GitHubError
from workflow_agent.issues import IssueError, load_issue_file
from workflow_agent.llm import LLMClient, LLMError
from workflow_agent.models import AttemptRecord, Feedback, Issue, RunReport
from workflow_agent.patcher import PatchError, PatchPolicy, apply_patch
from workflow_agent.planner import make_plan
from workflow_agent.prompts import summarize_changeset
from workflow_agent.repo_analyzer import IGNORED_DIRS, analyze_repo
from workflow_agent.reporting import build_pr_body, build_pr_title, create_run_dir, write_artifacts
from workflow_agent.reviewer import review_changes
from workflow_agent.security import PathPolicyError, redact, validate_branch_name
from workflow_agent.shell import CommandError
from workflow_agent.test_executor import TestExecutor

log = logging.getLogger("workflow_agent")


class WorkflowError(Exception):
    """A usage or precondition error (bad options, dirty tree, missing tools)."""


@dataclass
class WorkflowOptions:
    repo: Path
    issue_number: int | None = None
    issue_file: Path | None = None
    dry_run: bool = False
    mock: bool = False
    create_pr: bool = False
    draft_pr: bool = False
    base_branch: str | None = None
    branch_name: str | None = None
    allow_dirty: bool = False
    allow_edit_existing_tests: bool = False
    max_attempts: int | None = None
    github_repo: str | None = None
    output_dir: Path | None = None


def validate_options(o: WorkflowOptions) -> None:
    if (o.issue_number is None) == (o.issue_file is None):
        raise WorkflowError("provide exactly one of --issue or --issue-file")
    if o.mock and o.create_pr:
        raise WorkflowError("--mock cannot be combined with --create-pr (mock output must never reach GitHub)")
    if not o.repo.is_dir():
        raise WorkflowError(f"repository path does not exist: {o.repo}")
    for name in (o.branch_name, o.base_branch):
        if name:
            try:
                validate_branch_name(name)
            except PathPolicyError as exc:
                raise WorkflowError(str(exc)) from exc


class Workflow:
    def __init__(
        self,
        settings: Settings,
        options: WorkflowOptions,
        llm: LLMClient,
        github: GitHubClient | None = None,
        executor: TestExecutor | None = None,
    ) -> None:
        validate_options(options)
        self.settings = settings
        self.o = options
        self.llm = llm
        self.github = github
        self.executor = executor or TestExecutor(settings.test_command, settings.test_timeout)
        self.policy = PatchPolicy(allow_edit_existing_tests=options.allow_edit_existing_tests)
        self.max_attempts = options.max_attempts or settings.max_attempts

    # ------------------------------------------------------------------ #
    def run(self) -> RunReport:
        out_base = self.o.output_dir or self.settings.output_dir
        run_id, run_dir = create_run_dir(out_base, self.o.issue_number)
        report = RunReport(
            run_id=run_id, dry_run=self.o.dry_run, mock=self.o.mock,
            repo=str(self.o.repo.resolve()), output_dir=str(run_dir),
        )
        tmp_root: Path | None = None
        git: GitOps | None = None
        original_branch: str | None = None
        created_branch: str | None = None
        committed = False
        try:
            repo = self.o.repo.resolve()
            git, original_branch = self._preflight(repo, report)
            issue = self._load_issue(repo)
            report.issue = issue
            log.info("Issue: %s", issue.title)

            workdir = repo
            if self.o.dry_run:
                tmp_root = Path(tempfile.mkdtemp(prefix="agent-dryrun-"))
                workdir = tmp_root / "repo"
                shutil.copytree(
                    repo, workdir, symlinks=True,
                    ignore=shutil.ignore_patterns(*IGNORED_DIRS, ".env", "*.pem", "*.key"),
                )
                log.info("DRY RUN: working on a throw-away copy; the repository will not be modified")

            log.info("Step 1/6: analysing repository")
            ctx = analyze_repo(
                workdir, issue.as_text(),
                max_chars=self.settings.max_context_chars, max_file_bytes=self.settings.max_file_bytes,
            )
            log.info("  %d files, %d sent as context", len(ctx.all_paths), len(ctx.files))

            log.info("Step 2/6: running baseline tests")
            baseline = self.executor.run(workdir)
            report.baseline_tests = baseline
            log.info("  baseline: %s", baseline.short())
            if baseline.passed:
                report.notes.append("Baseline tests already pass; the issue may not be covered by tests.")

            log.info("Step 3/6: planning")
            plan = make_plan(self.llm, workdir, issue, ctx, baseline)
            report.plan = plan
            log.info("  plan: %s", plan.summary)

            if git is not None:
                base = self.o.base_branch or original_branch
                report.base_branch = base
                name = self.o.branch_name or (
                    f"agent/issue-{issue.number}-{slugify(issue.title)}"
                    if issue.number is not None else f"agent/{slugify(issue.title)}"
                )
                created_branch = git.unique_branch_name(name)
                git.create_branch(created_branch)
                report.branch = created_branch
                log.info("  created branch %s", created_branch)

            log.info("Step 4/6: generate -> patch -> test -> review (max %d attempts)", self.max_attempts)
            accepted = None
            feedback: list[Feedback] = []
            for n in range(1, self.max_attempts + 1):
                log.info("  attempt %d/%d", n, self.max_attempts)
                rec = AttemptRecord(attempt=n)
                report.attempts.append(rec)
                files = gather_files(
                    workdir, plan, ctx,
                    max_chars=self.settings.max_context_chars, max_file_bytes=self.settings.max_file_bytes,
                )
                changeset = generate_changes(self.llm, issue, plan, files, baseline, feedback)
                rec.changeset = changeset
                rec.stage_reached = "patch"
                try:
                    applied = apply_patch(workdir, changeset, self.policy)
                except PatchError as exc:
                    rec.error = str(exc)
                    log.warning("    patch rejected: %s", exc)
                    feedback.append(Feedback(attempt=n, kind="patch_error", detail=str(exc),
                                             previous_edits=summarize_changeset(changeset)))
                    continue
                rec.diff = applied.diff
                rec.stage_reached = "tests"
                log.info("    patched %s", ", ".join(applied.touched))

                tests = self.executor.run(workdir)
                rec.tests = tests
                log.info("    tests: %s", tests.short())
                if not tests.passed:
                    feedback.append(Feedback(attempt=n, kind="tests_failed", detail=tests.output,
                                             previous_edits=summarize_changeset(changeset)))
                    applied.rollback()
                    continue

                rec.stage_reached = "review"
                review = review_changes(self.llm, issue, plan, changeset, applied.diff, tests)
                rec.review = review
                log.info("    review: approved=%s (%d concerns)", review.approved, len(review.concerns))
                if not review.approved:
                    detail = review.summary + "\n" + "\n".join(f"[{c.severity}] {c.message}" for c in review.concerns)
                    feedback.append(Feedback(attempt=n, kind="review_rejected", detail=detail,
                                             previous_edits=summarize_changeset(changeset)))
                    applied.rollback()
                    continue

                rec.stage_reached = "accepted"
                accepted = (applied, changeset)
                break

            if accepted is None:
                raise WorkflowError(
                    f"no acceptable change after {self.max_attempts} attempt(s); see the run report for details"
                )

            applied, changeset = accepted
            report.final_diff = applied.diff

            log.info("Step 5/6: commit")
            message = format_commit_message(changeset.commit_message, issue.number, changeset.explanation)
            if git is None:
                log.info("  DRY RUN: would commit %s on a new branch", ", ".join(applied.touched))
            else:
                report.commit_sha = git.commit(applied.touched, message)
                committed = True
                log.info("  committed %s on %s", report.commit_sha[:10], created_branch)

            log.info("Step 6/6: pull request")
            if not self.o.create_pr:
                log.info("  skipped (use --create-pr to push the branch and open a PR)")
            elif git is None:
                log.info("  DRY RUN: would push the branch and open a pull request")
            else:
                assert created_branch and report.base_branch
                git.push(created_branch)
                report.pr_url = self._github(repo).create_pull_request(
                    title=build_pr_title(report), body=build_pr_body(report),
                    base=report.base_branch, head=created_branch, draft=self.o.draft_pr,
                )
                log.info("  opened %s (not merged; review it on GitHub)", report.pr_url)
            report.success = True
        except (WorkflowError, PatchError, LLMError, GitHubError, GitSafetyError, IssueError,
                CommandError, PathPolicyError) as exc:
            report.success = False
            report.error = redact(str(exc))
            log.error("Failed: %s", report.error)
        except Exception as exc:  # unexpected: keep the repo safe, surface the error
            report.success = False
            report.error = redact(f"unexpected {type(exc).__name__}: {exc}")
            log.exception("Unexpected failure")
        finally:
            self._restore(git, original_branch, created_branch, committed, report)
            if tmp_root is not None:
                shutil.rmtree(tmp_root, ignore_errors=True)
            report.llm_usage = dict(getattr(self.llm, "usage", {}))
            from datetime import datetime, timezone
            report.finished_at = datetime.now(timezone.utc)
            write_artifacts(report, run_dir)
        return report

    # ------------------------------------------------------------------ #
    def _preflight(self, repo: Path, report: RunReport) -> tuple[GitOps | None, str | None]:
        if self.o.dry_run:
            if self.o.create_pr:
                report.notes.append("dry-run: no branch, commit, push or PR will be created")
            return None, None
        git = GitOps(repo)
        if not git.is_repo():
            raise WorkflowError(f"{repo} is not a git repository")
        if git.toplevel() != repo:
            raise WorkflowError(f"--repo must be the repository root ({git.toplevel()})")
        if not git.has_commits():
            raise WorkflowError("the repository has no commits yet")
        original = git.current_branch()
        if original is None and not self.o.base_branch:
            raise WorkflowError("HEAD is detached; check out a branch or pass --base")
        if not self.o.allow_dirty and not git.is_clean():
            raise WorkflowError("working tree has uncommitted changes to tracked files; commit/stash or use --allow-dirty")
        if self.o.create_pr:
            if not git.has_remote("origin"):
                raise WorkflowError("no 'origin' remote configured; cannot open a pull request")
            self._github(repo).check_ready()
        return git, original

    def _github(self, repo: Path) -> GitHubClient:
        if self.github is None:
            self.github = GitHubClient(repo, self.o.github_repo or self.settings.github_repo)
        return self.github

    def _load_issue(self, repo: Path) -> Issue:
        if self.o.issue_file is not None:
            return load_issue_file(self.o.issue_file)
        assert self.o.issue_number is not None
        return self._github(repo).fetch_issue(self.o.issue_number)

    def _restore(self, git: GitOps | None, original: str | None, created: str | None,
                 committed: bool, report: RunReport) -> None:
        """Leave the user's checkout as we found it; drop the agent branch if nothing was committed."""
        if git is None or created is None:
            return
        try:
            if git.current_branch() != original and original:
                git.checkout(original)
            if not committed:
                git.delete_branch(created)
                report.branch = None
            else:
                report.notes.append(f"Switch to the result with: git switch {created}")
        except (CommandError, GitSafetyError) as exc:
            msg = f"could not restore branch state automatically: {redact(str(exc))}"
            report.notes.append(msg)
            log.warning(msg)
