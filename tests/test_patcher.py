import os
import subprocess

import pytest

from workflow_agent.models import ChangeSet, FileEdit
from workflow_agent.patcher import PatchError, PatchPolicy, apply_patch


def cs(*edits):
    return ChangeSet(explanation="x", commit_message="msg", edits=list(edits))


def replace(path, old, new):
    return FileEdit(path=path, operation="replace_text", old_text=old, new_text=new)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    return tmp_path


def test_replace_unique_text(repo):
    applied = apply_patch(repo, cs(replace("a.py", "x = 1", "x = 10")))
    assert (repo / "a.py").read_text() == "x = 10\ny = 2\n"
    assert applied.touched == ["a.py"]
    assert "-x = 1" in applied.diff and "+x = 10" in applied.diff


def test_replace_missing_and_ambiguous(repo):
    (repo / "dup.py").write_text("a\na\n")
    with pytest.raises(PatchError, match="not found"):
        apply_patch(repo, cs(replace("a.py", "nope", "z")))
    with pytest.raises(PatchError, match="2 times"):
        apply_patch(repo, cs(replace("dup.py", "a", "b")))
    assert (repo / "dup.py").read_text() == "a\na\n"


def test_create_and_overwrite_and_rollback(repo):
    applied = apply_patch(repo, cs(
        FileEdit(path="new/dir/n.py", operation="create", content="print(1)\n"),
        FileEdit(path="a.py", operation="overwrite", content="z = 3\n"),
    ))
    assert (repo / "new/dir/n.py").exists() and (repo / "a.py").read_text() == "z = 3\n"
    assert "new file mode" in applied.diff and "--- /dev/null" in applied.diff
    applied.rollback()
    assert not (repo / "new").exists()
    assert (repo / "a.py").read_text() == "x = 1\ny = 2\n"


def test_create_existing_file_fails(repo):
    with pytest.raises(PatchError, match="already exists"):
        apply_patch(repo, cs(FileEdit(path="a.py", operation="create", content="")))


def test_failed_edit_leaves_nothing_written(repo):
    with pytest.raises(PatchError):
        apply_patch(repo, cs(
            FileEdit(path="ok.py", operation="create", content="1\n"),
            replace("a.py", "missing", "z"),
        ))
    assert not (repo / "ok.py").exists()


@pytest.mark.parametrize("path", ["../evil.py", "/tmp/evil.py", ".git/config", ".env", ".github/workflows/ci.yml"])
def test_policy_blocks_dangerous_paths(repo, path):
    with pytest.raises(PatchError):
        apply_patch(repo, cs(FileEdit(path=path, operation="create", content="x")))


def test_existing_tests_protected_but_new_tests_allowed(repo):
    with pytest.raises(PatchError, match="existing test file"):
        apply_patch(repo, cs(replace("tests/test_a.py", "True", "False")))
    with pytest.raises(PatchError, match="existing test file"):
        apply_patch(repo, cs(FileEdit(path="tests/test_a.py", operation="overwrite", content="")))
    apply_patch(repo, cs(FileEdit(path="tests/test_b.py", operation="create", content="def test_b(): pass\n")))
    assert (repo / "tests/test_b.py").exists()
    apply_patch(repo, cs(replace("tests/test_a.py", "True", "1 == 1")), PatchPolicy(allow_edit_existing_tests=True))
    assert "1 == 1" in (repo / "tests/test_a.py").read_text()


def test_limits(repo):
    many = [FileEdit(path=f"f{i}.py", operation="create", content="1\n") for i in range(4)]
    with pytest.raises(PatchError, match="more than 3 files"):
        apply_patch(repo, cs(*many), PatchPolicy(max_files=3))
    with pytest.raises(PatchError, match="limit"):
        apply_patch(repo, cs(FileEdit(path="big.py", operation="create", content="x" * 50)), PatchPolicy(max_bytes_per_file=10))


def test_noop_and_binary(repo):
    with pytest.raises(PatchError, match="no effective changes"):
        apply_patch(repo, cs(replace("a.py", "x = 1", "x = 1")))
    (repo / "b.bin").write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(PatchError, match="binary"):
        apply_patch(repo, cs(replace("b.bin", "a", "b")))


def test_crlf_files_keep_line_endings(repo):
    (repo / "w.txt").write_bytes(b"one\r\ntwo\r\n")
    apply_patch(repo, cs(replace("w.txt", "one\ntwo", "uno\ndos")))
    assert (repo / "w.txt").read_bytes() == b"uno\r\ndos\r\n"


def test_multiple_edits_same_file_apply_sequentially(repo):
    apply_patch(repo, cs(replace("a.py", "x = 1", "x = 5"), replace("a.py", "y = 2", "y = 6")))
    assert (repo / "a.py").read_text() == "x = 5\ny = 6\n"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_refuses_to_write_through_symlink(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("keep")
    (repo / "link.txt").symlink_to(outside)
    with pytest.raises(PatchError):
        apply_patch(repo, cs(FileEdit(path="link.txt", operation="overwrite", content="pwn")))
    assert outside.read_text() == "keep"


def test_diff_is_a_valid_git_patch(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("x = 1")  # no trailing newline
    applied = apply_patch(tmp_path, cs(
        replace("a.py", "x = 1", "x = 2"),
        FileEdit(path="n.py", operation="create", content="n = 1\n"),
    ))
    applied.rollback()
    check = subprocess.run(["git", "apply", "--check", "-"], input=applied.diff, text=True, cwd=tmp_path, capture_output=True)
    assert check.returncode == 0, check.stderr
