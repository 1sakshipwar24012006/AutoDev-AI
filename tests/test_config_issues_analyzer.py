import json

import pytest

from workflow_agent.config import ConfigError, Settings
from workflow_agent.issues import IssueError, load_issue_file
from workflow_agent.repo_analyzer import analyze_repo, list_repo_files, read_text_file
from workflow_agent.security import redact


def test_settings_defaults_and_overrides():
    s = Settings.from_env(environ={"ANTHROPIC_API_KEY": " sk-ant-abcdefghij ", "AGENT_MAX_ATTEMPTS": "5",
                                   "AGENT_TEST_COMMAND": "python -m pytest -x", "AGENT_MODEL": "m1"})
    assert s.max_attempts == 5 and s.model == "m1" and s.test_command == ["python", "-m", "pytest", "-x"]
    assert s.require_api_key() == "sk-ant-abcdefghij"


def test_api_key_never_appears_in_repr_or_logs():
    s = Settings.from_env(environ={"ANTHROPIC_API_KEY": "sk-ant-verysecretkey123"})
    assert "verysecretkey123" not in repr(s) and "verysecretkey123" not in s.model_dump_json()
    assert "verysecretkey123" not in redact("failed with key sk-ant-verysecretkey123")


def test_missing_key_and_bad_values():
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        Settings.from_env(environ={}).require_api_key()
    with pytest.raises(ConfigError):
        Settings.from_env(environ={"AGENT_MAX_ATTEMPTS": "many"})
    with pytest.raises(ConfigError):
        Settings.from_env(environ={"AGENT_MAX_ATTEMPTS": "0"})


def test_env_file_loading(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    env = tmp_path / "my.env"
    env.write_text("ANTHROPIC_API_KEY=sk-ant-fromfile12345\nAGENT_MODEL=file-model\n")
    s = Settings.from_env(env_file=env)
    assert s.require_api_key() == "sk-ant-fromfile12345" and s.model == "file-model"
    with pytest.raises(ConfigError, match="not found"):
        Settings.from_env(env_file=tmp_path / "missing.env")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_MODEL", raising=False)


def test_issue_file_markdown_and_json(tmp_path):
    md = tmp_path / "i.md"
    md.write_text("# Title here\n\nBody line 1\nBody 2\n")
    issue = load_issue_file(md)
    assert issue.title == "Title here" and "Body 2" in issue.body and issue.number is None
    js = tmp_path / "i.json"
    js.write_text(json.dumps({"number": 7, "title": "T", "body": "B", "labels": ["bug"]}))
    assert load_issue_file(js).number == 7
    (tmp_path / "empty.md").write_text("\n\n")
    for bad in (tmp_path / "empty.md", tmp_path / "nope.md"):
        with pytest.raises(IssueError):
            load_issue_file(bad)
    (tmp_path / "bad.json").write_text("{}")
    with pytest.raises(IssueError):
        load_issue_file(tmp_path / "bad.json")


def test_analyzer_ranks_relevant_files_and_hides_secrets(sample_copy):
    (sample_copy / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-topsecret12345\n")
    (sample_copy / "server.pem").write_text("-----BEGIN PRIVATE KEY-----")
    (sample_copy / "blob.bin").write_bytes(b"\x00\x01\x02")
    (sample_copy / "__pycache__").mkdir()
    (sample_copy / "__pycache__" / "x.pyc").write_bytes(b"1")
    paths = list_repo_files(sample_copy)
    assert "shop/pricing.py" in paths
    assert ".env" not in paths and "server.pem" not in paths
    assert not any("__pycache__" in p for p in paths)
    ctx = analyze_repo(sample_copy, "apply_discount in shop/pricing.py returns the wrong value")
    assert list(ctx.files)[0] == "shop/pricing.py"
    assert "blob.bin" not in ctx.files and ".env" not in ctx.files
    assert all("topsecret" not in text for text in ctx.files.values())


def test_analyzer_respects_budget_and_file_size(sample_copy):
    (sample_copy / "huge.py").write_text("x = 1\n" * 20000)
    ctx = analyze_repo(sample_copy, "pricing", max_chars=2500, max_file_bytes=50_000)
    assert "huge.py" not in ctx.files
    assert sum(len(t) for t in ctx.files.values()) <= 2500 + max(len(t) for t in ctx.files.values())
    assert read_text_file(sample_copy, "huge.py", 1000) is None
    assert read_text_file(sample_copy, ".env", 1000) is None
