import os
import sys

import pytest

from workflow_agent.security import (
    PathPolicyError, clear_registered_secrets, is_protected_branch, is_sensitive_file, is_test_path,
    redact, register_secret, validate_branch_name, validate_relative_path, validate_test_command,
)


@pytest.mark.parametrize("bad", [
    "", "   ", "/etc/passwd", "C:\\Windows\\x", "C:/x", "~/x", "../x", "a/../../x", "a\\..\\..\\x",
    ".git/config", ".GIT/hooks/pre-commit", "sub/.git/config", ".env", ".env.local", "config/prod.env",
    "keys/server.pem", "id_rsa", ".github/workflows/ci.yml", "con", "nul.txt", "a\x00b", "x" * 400, ".",
])
def test_rejects_unsafe_paths(tmp_path, bad):
    with pytest.raises(PathPolicyError):
        validate_relative_path(tmp_path, bad)


@pytest.mark.parametrize("good", ["a.py", "pkg/mod.py", "./pkg/mod.py", "pkg\\mod.py", ".env.example", "docs/readme.md"])
def test_accepts_normal_paths(tmp_path, good):
    path = validate_relative_path(tmp_path, good)
    assert path.is_relative_to(tmp_path.resolve())


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "link").symlink_to(outside)
    with pytest.raises(PathPolicyError):
        validate_relative_path(repo, "link/file.txt")
    (repo / "flink").symlink_to(outside / "f.txt")
    with pytest.raises(PathPolicyError):
        validate_relative_path(repo, "flink")


def test_allow_protected_flag(tmp_path):
    assert validate_relative_path(tmp_path, ".env", allow_protected=True).name == ".env"


def test_sensitive_and_test_path_detection():
    assert is_sensitive_file(".env") and is_sensitive_file("a/b/server.key")
    assert not is_sensitive_file(".env.example") and not is_sensitive_file("src/app.py")
    assert is_test_path("tests/test_x.py") and is_test_path("pkg/test_y.py") and is_test_path("a/b.spec.ts")
    assert not is_test_path("src/app.py")


def test_redaction_of_registered_and_pattern_secrets():
    register_secret("super-secret-value-123")
    text = "key=super-secret-value-123 token=ghp_" + "a" * 30 + " sk-ant-api03-abcdefghijkl Authorization: Bearer abcdefgh12345"
    out = redact(text)
    assert "super-secret-value-123" not in out
    assert "ghp_" not in out and "sk-ant-api03" not in out and "abcdefgh12345" not in out
    assert out.count("[REDACTED]") >= 4
    clear_registered_secrets()
    assert redact("nothing here") == "nothing here"


@pytest.mark.parametrize("name", ["", "-x", "a b", "a..b", "x/", "a@{b", "a;rm", "$(x)"])
def test_invalid_branch_names(name):
    with pytest.raises(PathPolicyError):
        validate_branch_name(name)


def test_protected_branches():
    for name in ("main", "MASTER", "develop", "release/1.0", "hotfix/x", "production"):
        assert is_protected_branch(name)
    assert not is_protected_branch("agent/issue-1-fix")
    assert validate_branch_name("agent/issue-1-fix") == "agent/issue-1-fix"


def test_validate_test_command():
    assert validate_test_command(["python", "-m", "pytest"])[0] == sys.executable
    assert validate_test_command(["py", "-3", "-m", "pytest"])[1:] == ["-m", "pytest"]
    assert validate_test_command(["pytest", "-q"]) == ["pytest", "-q"]
    for bad in (["bash", "-c", "x"], ["rm", "-rf", "/"], [], ["python", "a\nb"], ["npm", "test"]):
        with pytest.raises(PathPolicyError):
            validate_test_command(bad)
