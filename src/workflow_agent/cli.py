"""Command line interface."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from workflow_agent import __version__
from workflow_agent.config import ConfigError, Settings
from workflow_agent.demo import create_demo_repo, sample_issue_path
from workflow_agent.llm import ClaudeClient, LLMClient, LLMError, MockLLMClient, default_fixture_path
from workflow_agent.logging_utils import setup_logging
from workflow_agent.models import RunReport
from workflow_agent.repo_analyzer import analyze_repo
from workflow_agent.security import PathPolicyError
from workflow_agent.shell import CommandError
from workflow_agent.workflow import Workflow, WorkflowError, WorkflowOptions

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workflow-agent",
        description="Autonomous coding workflow agent: GitHub issue -> tested, reviewed pull request.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--env-file", help="path to a .env file (default: .env in the launch directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="resolve an issue in a repository")
    run.add_argument("--repo", default=".", help="path to the git repository root (default: .)")
    src = run.add_mutually_exclusive_group(required=True)
    src.add_argument("--issue", type=int, help="GitHub issue number (fetched with the gh CLI)")
    src.add_argument("--issue-file", type=Path, help="local .md/.json issue instead of GitHub")
    run.add_argument("--github-repo", help="owner/name for gh (default: inferred from the repo)")
    run.add_argument("--dry-run", action="store_true",
                     help="work on a temp copy; no branch, commit, push or PR is created")
    run.add_argument("--mock", action="store_true", help="use canned LLM responses (no API key, no cost)")
    run.add_argument("--mock-fixture", type=Path, help="JSON fixture for --mock (default: bundled sample fix)")
    run.add_argument("--create-pr", action="store_true",
                     help="push the agent branch and open a PR (never merges, never pushes main)")
    run.add_argument("--draft", action="store_true", help="open the PR as a draft")
    run.add_argument("--base", help="PR base branch (default: the branch checked out at start)")
    run.add_argument("--branch", help="name for the agent branch (default: agent/issue-N-slug)")
    run.add_argument("--max-attempts", type=int, help="generate/test/review attempts (default 3)")
    run.add_argument("--test-command", help='test command, e.g. "python -m pytest -q"')
    run.add_argument("--allow-dirty", action="store_true", help="allow uncommitted tracked changes")
    run.add_argument("--allow-edit-existing-tests", action="store_true",
                     help="let the agent modify existing test files (blocked by default)")
    run.add_argument("--output-dir", type=Path, help="where to write reports/patches (default: agent_runs)")

    demo = sub.add_parser("demo", help="run the bundled buggy sample app through the agent (mock LLM)")
    demo.add_argument("--dry-run", action="store_true", help="do not create a branch/commit in the demo repo")
    demo.add_argument("--cleanup", action="store_true", help="delete the demo repo afterwards")
    demo.add_argument("--output-dir", type=Path)

    analyze = sub.add_parser("analyze", help="show what repository context would be sent to the model")
    analyze.add_argument("--repo", default=".")
    analyze.add_argument("--issue-text", default="", help="issue text used to rank relevant files")
    return parser


def _build_llm(settings: Settings, mock: bool, fixture: Path | None) -> LLMClient:
    if mock:
        return MockLLMClient.from_file(fixture or default_fixture_path())
    return ClaudeClient(
        api_key=settings.require_api_key(), model=settings.model,
        max_tokens=settings.max_tokens, timeout=settings.llm_timeout,
    )


def _print_summary(report: RunReport) -> None:
    out = sys.stdout
    out.write("\n" + "=" * 64 + "\n")
    out.write(f"Result : {'SUCCESS' if report.success else 'FAILED'}"
              f"{' (dry-run)' if report.dry_run else ''}{' (mock)' if report.mock else ''}\n")
    if report.issue:
        out.write(f"Issue  : {report.issue.title}\n")
    out.write(f"Attempts: {len(report.attempts)}\n")
    if report.branch:
        out.write(f"Branch : {report.branch}\n")
    if report.commit_sha:
        out.write(f"Commit : {report.commit_sha}\n")
    if report.pr_url:
        out.write(f"PR     : {report.pr_url}\n")
    if report.error:
        out.write(f"Error  : {report.error}\n")
    for note in report.notes:
        out.write(f"Note   : {note}\n")
    if report.output_dir:
        out.write(f"Report : {report.output_dir}\n")
    out.write("=" * 64 + "\n")


def _cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    if args.test_command:
        import os
        import shlex
        settings.test_command = shlex.split(args.test_command, posix=(os.name != "nt"))
    options = WorkflowOptions(
        repo=Path(args.repo), issue_number=args.issue, issue_file=args.issue_file,
        dry_run=args.dry_run, mock=args.mock, create_pr=args.create_pr, draft_pr=args.draft,
        base_branch=args.base, branch_name=args.branch, allow_dirty=args.allow_dirty,
        allow_edit_existing_tests=args.allow_edit_existing_tests, max_attempts=args.max_attempts,
        github_repo=args.github_repo, output_dir=args.output_dir,
    )
    llm = _build_llm(settings, args.mock, args.mock_fixture)
    report = Workflow(settings, options, llm).run()
    _print_summary(report)
    return EXIT_OK if report.success else EXIT_FAILED


def _cmd_demo(args: argparse.Namespace, settings: Settings) -> int:
    repo = create_demo_repo()
    sys.stdout.write(f"Demo repository: {repo}\n")
    options = WorkflowOptions(
        repo=repo, issue_file=sample_issue_path(), mock=True, dry_run=args.dry_run,
        output_dir=args.output_dir or repo.parent / "agent_runs",
    )
    report = Workflow(settings, options, MockLLMClient.from_file(default_fixture_path())).run()
    _print_summary(report)
    if not args.cleanup:
        sys.stdout.write(f"\nInspect it with:\n  git -C \"{repo}\" log --oneline --all\n"
                         f"  git -C \"{repo}\" show {report.branch or '<branch>'}\n")
    else:
        shutil.rmtree(repo.parent, ignore_errors=True)
    return EXIT_OK if report.success else EXIT_FAILED


def _cmd_analyze(args: argparse.Namespace, settings: Settings) -> int:
    ctx = analyze_repo(Path(args.repo), args.issue_text, max_chars=settings.max_context_chars,
                       max_file_bytes=settings.max_file_bytes)
    sys.stdout.write(f"{len(ctx.all_paths)} files in repository\n\nSelected context files:\n")
    for path in ctx.files:
        sys.stdout.write(f"  {path} ({len(ctx.files[path])} chars)\n")
    if ctx.omitted:
        sys.stdout.write(f"\nOmitted ({len(ctx.omitted)}): {', '.join(ctx.omitted[:20])}\n")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    try:
        settings = Settings.from_env(args.env_file)
        handler = {"run": _cmd_run, "demo": _cmd_demo, "analyze": _cmd_analyze}[args.command]
        return handler(args, settings)
    except (ConfigError, WorkflowError, PathPolicyError, FileNotFoundError, LLMError, CommandError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_USAGE
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        return 130
