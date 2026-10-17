import shutil

import pytest

from workflow_agent.cli import build_parser, main

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def test_parser_requires_command_and_issue_source():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--issue", "1", "--issue-file", "x.md"])
    args = build_parser().parse_args(["run", "--issue", "4", "--dry-run", "--mock"])
    assert args.issue == 4 and args.dry_run and args.mock


def test_run_without_api_key_is_a_clean_usage_error(demo_repo, monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    code = main(["run", "--repo", str(demo_repo), "--issue", "1"])
    assert code == 2 and "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_mock_plus_create_pr_rejected(demo_repo, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from workflow_agent.demo import sample_issue_path
    code = main(["run", "--repo", str(demo_repo), "--issue-file", str(sample_issue_path()), "--mock", "--create-pr"])
    assert code == 2 and "--mock cannot be combined" in capsys.readouterr().err


def test_demo_command_end_to_end(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert main(["demo", "--cleanup"]) == 0
    assert "SUCCESS" in capsys.readouterr().out


def test_demo_dry_run_and_analyze(monkeypatch, capsys, tmp_path, sample_copy):
    monkeypatch.chdir(tmp_path)
    assert main(["demo", "--dry-run", "--cleanup"]) == 0
    capsys.readouterr()
    assert main(["analyze", "--repo", str(sample_copy), "--issue-text", "apply_discount pricing"]) == 0
    assert "shop/pricing.py" in capsys.readouterr().out


def test_run_with_mock_fixture_and_custom_test_command(demo_repo, monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    from workflow_agent.demo import sample_issue_path
    code = main(["run", "--repo", str(demo_repo), "--issue-file", str(sample_issue_path()), "--mock", "--dry-run",
                 "--test-command", "python -m pytest -q -p no:cacheprovider"])
    assert code == 0
    code = main(["run", "--repo", str(demo_repo), "--issue-file", str(sample_issue_path()), "--mock", "--dry-run",
                 "--test-command", "bash -c 'exit 0'"])
    assert code == 2  # disallowed executable is a usage error
