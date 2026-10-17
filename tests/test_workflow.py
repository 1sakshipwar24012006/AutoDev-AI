import json
import shutil
import subprocess
from pathlib import Path

import pytest

from workflow_agent.demo import sample_issue_path
from workflow_agent.llm import MockLLMClient, default_fixture_path
from workflow_agent.workflow import Workflow, WorkflowError, WorkflowOptions

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

FIXTURE = json.loads(default_fixture_path().read_text())


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def make(settings, repo, fixture=FIXTURE, **opts):
    options = WorkflowOptions(repo=repo, issue_file=sample_issue_path(), mock=True, output_dir=settings.output_dir, **opts)
    mock = MockLLMClient(fixture)
    return Workflow(settings, options, mock), mock


def test_real_run_commits_on_new_branch_with_retry(settings, demo_repo):
    before = git(demo_repo, "rev-parse", "main")
    wf, mock = make(settings, demo_repo)
    report = wf.run()
    assert report.success, report.error
    assert [a.stage_reached for a in report.attempts] == ["tests", "accepted"]  # attempt 1 failed tests -> retried
    assert report.attempts[0].tests and not report.attempts[0].tests.passed
    assert mock.calls == ["plan", "codegen", "codegen", "review"]  # review only after tests pass
    assert report.branch and report.branch.startswith("agent/")
    # user's checkout restored, main untouched, work is on the agent branch only
    assert git(demo_repo, "branch", "--show-current") == "main"
    assert git(demo_repo, "rev-parse", "main") == before
    assert "return 0.0" not in (demo_repo / "shop/pricing.py").read_text()
    files = git(demo_repo, "diff", "--name-only", f"main..{report.branch}").splitlines()
    assert sorted(files) == ["shop/pricing.py", "tests/test_pricing_regression.py"]
    assert git(demo_repo, "rev-parse", report.branch) == report.commit_sha
    # artefacts
    run_dir = Path(report.output_dir)
    assert (run_dir / "report.json").exists() and (run_dir / "changes.patch").exists() and (run_dir / "pr_body.md").exists()
    # the fix actually works on the branch
    git(demo_repo, "checkout", "-q", report.branch)
    done = subprocess.run(["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=demo_repo, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout


def test_dry_run_never_touches_repo(settings, demo_repo):
    branches_before = git(demo_repo, "branch", "--list")
    wf, _ = make(settings, demo_repo, dry_run=True)
    report = wf.run()
    assert report.success and report.dry_run and report.commit_sha is None and report.branch is None
    assert git(demo_repo, "branch", "--list") == branches_before
    assert git(demo_repo, "status", "--porcelain") == ""
    assert "return sum(prices) / len(prices)" in (demo_repo / "shop/pricing.py").read_text()
    assert report.final_diff


def test_dry_run_works_on_non_git_directory(settings, sample_copy):
    report = make(settings, sample_copy, dry_run=True)[0].run()
    assert report.success, report.error


def test_failure_rolls_back_and_removes_branch(settings, demo_repo):
    bad = {**FIXTURE, "codegen": [FIXTURE["codegen"][0]]}  # always the partial (insufficient) fix
    wf, _ = make(settings, demo_repo, fixture=bad)
    report = wf.run()
    assert not report.success and "no acceptable change" in report.error
    assert len(report.attempts) == 3
    assert git(demo_repo, "branch", "--list") .strip().lstrip("* ") == "main"
    assert git(demo_repo, "status", "--porcelain") == ""
    assert "(percent / 100)" in (demo_repo / "shop/pricing.py").read_text()  # reverted
    assert (Path(report.output_dir) / "last_attempt_rejected.patch").exists()


def test_review_rejection_triggers_retry(settings, demo_repo):
    rejecting = {"approved": False, "summary": "nope", "concerns": [{"severity": "blocker", "message": "bad"}]}
    fixture = {**FIXTURE, "codegen": [FIXTURE["codegen"][1]], "review": [rejecting, FIXTURE["review"][0]]}
    report = make(settings, demo_repo, fixture=fixture)[0].run()
    assert report.success
    assert [a.stage_reached for a in report.attempts] == ["review", "accepted"]


def test_patch_policy_violation_is_reported_and_retried(settings, demo_repo):
    evil = {"explanation": "x", "commit_message": "m",
            "edits": [{"path": ".github/workflows/ci.yml", "operation": "create", "content": "on: push"}]}
    fixture = {**FIXTURE, "codegen": [evil, FIXTURE["codegen"][1]]}
    report = make(settings, demo_repo, fixture=fixture)[0].run()
    assert report.success and "protected file" in report.attempts[0].error
    assert not (demo_repo / ".github").exists()


def test_existing_test_files_cannot_be_weakened(settings, demo_repo):
    cheat = {"explanation": "x", "commit_message": "m", "edits": [
        {"path": "tests/test_pricing.py", "operation": "overwrite", "content": "def test_ok():\n    pass\n"}]}
    fixture = {**FIXTURE, "codegen": [cheat]}
    report = make(settings, demo_repo, fixture=fixture, max_attempts=1)[0].run()
    assert not report.success and "existing test file" in report.attempts[0].error
    assert "assert apply_discount" in (demo_repo / "tests/test_pricing.py").read_text()


def test_dirty_tree_is_refused(settings, demo_repo):
    (demo_repo / "README.md").write_text("local edit\n")
    report = make(settings, demo_repo)[0].run()
    assert not report.success and "uncommitted changes" in report.error
    report = make(settings, demo_repo, allow_dirty=True)[0].run()
    assert report.success


def test_option_validation(settings, demo_repo):
    with pytest.raises(WorkflowError, match="exactly one"):
        Workflow(settings, WorkflowOptions(repo=demo_repo), MockLLMClient({}))
    with pytest.raises(WorkflowError, match="--mock cannot be combined"):
        Workflow(settings, WorkflowOptions(repo=demo_repo, issue_file=sample_issue_path(), mock=True, create_pr=True), MockLLMClient({}))
    with pytest.raises(WorkflowError, match="invalid branch"):
        Workflow(settings, WorkflowOptions(repo=demo_repo, issue_file=sample_issue_path(), branch_name="a b"), MockLLMClient({}))
    with pytest.raises(WorkflowError, match="does not exist"):
        Workflow(settings, WorkflowOptions(repo=demo_repo / "nope", issue_file=sample_issue_path()), MockLLMClient({}))


def test_not_a_git_repo_is_refused_without_dry_run(settings, sample_copy):
    report = make(settings, sample_copy)[0].run()
    assert not report.success and "not a git repository" in report.error


class FakeGitHub:
    def __init__(self):
        self.created = []

    def check_ready(self):
        pass

    def create_pull_request(self, **kwargs):
        self.created.append(kwargs)
        return "https://github.com/o/r/pull/1"


def test_pr_flow_pushes_agent_branch_only_and_never_merges(settings, demo_repo, tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(demo_repo, "remote", "add", "origin", str(bare))
    git(demo_repo, "push", "-q", "origin", "main")
    gh = FakeGitHub()
    options = WorkflowOptions(repo=demo_repo, issue_file=sample_issue_path(), create_pr=True, draft_pr=True,
                              output_dir=settings.output_dir)
    # WorkflowOptions.mock stays False here only so the (fake) PR path is exercised; the LLM is still mocked
    wf = Workflow(settings, options, MockLLMClient(FIXTURE), github=gh)
    report = wf.run()
    assert report.success, report.error
    assert report.pr_url == "https://github.com/o/r/pull/1"
    pr = gh.created[0]
    assert pr["base"] == "main" and pr["head"] == report.branch and pr["draft"] is True
    remote_branches = subprocess.run(["git", "branch", "--list"], cwd=bare, capture_output=True, text=True).stdout
    assert report.branch in remote_branches and "main" in remote_branches
    assert git(bare, "rev-parse", "main") == git(demo_repo, "rev-parse", "main")  # main on remote unchanged


def test_pr_requires_remote(settings, demo_repo):
    options = WorkflowOptions(repo=demo_repo, issue_file=sample_issue_path(), create_pr=True, output_dir=settings.output_dir)
    report = Workflow(settings, options, MockLLMClient(FIXTURE), github=FakeGitHub()).run()
    assert not report.success and "origin" in report.error
