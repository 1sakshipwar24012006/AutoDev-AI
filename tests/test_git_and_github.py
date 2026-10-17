import json
import shutil
import subprocess

import pytest

from workflow_agent.git_ops import GitOps, GitSafetyError, format_commit_message, slugify
from workflow_agent.github_client import GitHubClient, GitHubError
from workflow_agent.models import AttemptRecord, ChangeSet, FileEdit, Issue, Plan, Review, RunReport, TestRunResult
from workflow_agent.reporting import build_pr_body, build_pr_title
from workflow_agent.shell import CommandError, CommandResult

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def test_slugify_and_commit_message():
    assert slugify("Fix: Crash on EMPTY list!!") == "fix-crash-on-empty-list"
    assert slugify("###") == "change"
    msg = format_commit_message("x" * 100 + "\nmore", 12, "Explained.")
    assert len(msg.splitlines()[0]) == 72 and "Refs #12" in msg and "Generated-by:" in msg


def test_git_ops_branch_commit_and_cleanliness(demo_repo):
    git = GitOps(demo_repo)
    assert git.is_repo() and git.has_commits() and git.current_branch() == "main" and git.is_clean()
    assert git.toplevel() == demo_repo.resolve()
    git.create_branch("agent/test")
    assert git.current_branch() == "agent/test"
    assert git.unique_branch_name("agent/test") == "agent/test-2"
    (demo_repo / "shop" / "pricing.py").write_text("# changed\n")
    (demo_repo / "unrelated.txt").write_text("leave me alone")
    assert not git.is_clean()
    sha = git.commit(["shop/pricing.py"], "Change pricing")
    assert len(sha) == 40 and git.is_clean()
    tracked = subprocess.run(["git", "ls-files"], cwd=demo_repo, capture_output=True, text=True).stdout
    assert "unrelated.txt" not in tracked  # only explicitly listed paths are staged
    git.checkout("main")
    git.delete_branch("agent/test")
    assert not git.branch_exists("agent/test")


def test_git_ops_refuses_protected_things(demo_repo):
    git = GitOps(demo_repo)
    for name in ("main", "master", "release/1.0"):
        with pytest.raises(GitSafetyError):
            git.create_branch(name)
        with pytest.raises(GitSafetyError):
            git.push(name)
        with pytest.raises(GitSafetyError):
            git.delete_branch(name)
    with pytest.raises(GitSafetyError):
        git.commit([".env"], "secrets")
    with pytest.raises(GitSafetyError):
        git.commit([], "nothing")


def test_push_uses_explicit_refspec_without_force(demo_repo):
    calls = []

    def fake_runner(args, **kwargs):
        calls.append(args)
        return CommandResult(args=list(args), returncode=0, stdout="", stderr="", duration=0)

    GitOps(demo_repo, runner=fake_runner).push("agent/issue-1-x")
    push = calls[-1]
    assert push[:2] == ["git", "push"] and "origin" in push
    assert "refs/heads/agent/issue-1-x:refs/heads/agent/issue-1-x" in push
    assert not any(a in ("--force", "-f", "--force-with-lease", "--mirror", "--delete") for a in push)


def test_has_remote_false_without_origin(demo_repo):
    assert GitOps(demo_repo).has_remote() is False


# ---- GitHub client (fake runner; no network, no gh needed) -------------------

class Recorder:
    def __init__(self, stdout="", returncode=0):
        self.calls, self.stdout, self.returncode = [], stdout, returncode

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        result = CommandResult(args=list(args), returncode=self.returncode, stdout=self.stdout, stderr="err", duration=0)
        if kwargs.get("check") and not result.ok:
            raise CommandError("failed")
        return result


def test_fetch_issue_parses_json(tmp_path):
    payload = {"number": 5, "title": "Bug", "body": None, "url": "https://github.com/o/r/issues/5", "labels": [{"name": "bug"}]}
    rec = Recorder(stdout=json.dumps(payload))
    issue = GitHubClient(tmp_path, repo="o/r", runner=rec).fetch_issue(5)
    assert issue.number == 5 and issue.body == "" and issue.labels == ["bug"]
    args = rec.calls[0][0]
    assert args[:4] == ["gh", "issue", "view", "5"] and args[-2:] == ["--repo", "o/r"]


def test_github_client_validation(tmp_path):
    with pytest.raises(GitHubError):
        GitHubClient(tmp_path, repo="bad repo; rm -rf /")
    with pytest.raises(GitHubError):
        GitHubClient(tmp_path, runner=Recorder()).fetch_issue(-1)
    with pytest.raises(GitHubError):
        GitHubClient(tmp_path, runner=Recorder(stdout="not json")).fetch_issue(1)
    with pytest.raises(GitHubError):
        GitHubClient(tmp_path, runner=Recorder(returncode=1)).fetch_issue(1)


def test_check_ready(tmp_path):
    GitHubClient(tmp_path, runner=Recorder()).check_ready()
    with pytest.raises(GitHubError, match="not authenticated"):
        GitHubClient(tmp_path, runner=Recorder(returncode=1)).check_ready()


def test_create_pr_never_merges_and_returns_url(tmp_path):
    rec = Recorder(stdout="Creating pull request...\nhttps://github.com/o/r/pull/9\n")
    url = GitHubClient(tmp_path, runner=rec).create_pull_request(
        title="-t; evil", body="body", base="main", head="agent/x", draft=True)
    assert url == "https://github.com/o/r/pull/9"
    argv = rec.calls[0][0]
    assert argv[:3] == ["gh", "pr", "create"] and "--draft" in argv
    assert "--title=-t; evil" in argv and "--base=main" in argv and "--head=agent/x" in argv
    assert not any("merge" in a for a in argv)
    assert not hasattr(GitHubClient, "merge_pull_request")


def test_pr_title_and_body():
    changeset = ChangeSet(explanation="Did the thing.", commit_message="m",
                          edits=[FileEdit(path="a.py", operation="create", content="x")])
    passing = TestRunResult(command="pytest", returncode=0, passed=True, output="ok")
    report = RunReport(run_id="r", issue=Issue(number=3, title="Crash"), plan=Plan(
        summary="s", root_cause="Because.", steps=["a"], test_strategy="t"),
        baseline_tests=TestRunResult(command="pytest", returncode=1, passed=False, output="bad"),
        attempts=[AttemptRecord(attempt=1, stage_reached="accepted", changeset=changeset, tests=passing,
                                review=Review(approved=True, summary="Looks good."))])
    assert build_pr_title(report) == "Fix #3: Crash"
    body = build_pr_body(report)
    for needle in ("Did the thing.", "Closes #3", "Because.", "`a.py` (create)", "Baseline tests: FAILED",
                   "Tests after change: PASSED", "Looks good.", "not merged automatically"):
        assert needle in body
