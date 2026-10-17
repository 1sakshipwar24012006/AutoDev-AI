import os
import sys

import pytest

from workflow_agent.security import register_secret
from workflow_agent.shell import CommandError, build_env, run_command, truncate_middle


def test_runs_and_captures_output(tmp_path):
    r = run_command([sys.executable, "-c", "print('hi')"], cwd=tmp_path)
    assert r.ok and r.stdout.strip() == "hi"


def test_rejects_string_commands(tmp_path):
    with pytest.raises(CommandError):
        run_command("echo hi", cwd=tmp_path)  # type: ignore[arg-type]
    with pytest.raises(CommandError):
        run_command([], cwd=tmp_path)


def test_no_shell_interpretation(tmp_path):
    r = run_command([sys.executable, "-c", "import sys; print(sys.argv[1])", "a; echo injected && true"], cwd=tmp_path)
    assert r.stdout.strip() == "a; echo injected && true"


def test_missing_executable(tmp_path):
    with pytest.raises(CommandError, match="not found"):
        run_command(["definitely-not-a-real-binary-xyz"], cwd=tmp_path)


def test_timeout_kills_process(tmp_path):
    r = run_command([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, timeout=1)
    assert r.timed_out and not r.ok and r.returncode is None


def test_check_raises_with_output(tmp_path):
    with pytest.raises(CommandError, match="boom"):
        run_command([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"], cwd=tmp_path, check=True)


def test_secret_env_stripped_for_untrusted_code(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-leak")
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "x" * 30)
    monkeypatch.setenv("MY_PASSWORD", "hunter2hunter2")
    monkeypatch.setenv("HARMLESS", "ok")
    script = "import os; print(sorted(k for k in ('ANTHROPIC_API_KEY','GH_TOKEN','MY_PASSWORD','HARMLESS') if k in os.environ))"
    untrusted = run_command([sys.executable, "-c", script], cwd=tmp_path, pass_secrets=False)
    assert untrusted.stdout.strip() == "['HARMLESS']"
    trusted = run_command([sys.executable, "-c", script], cwd=tmp_path, pass_secrets=True)
    assert "GH_TOKEN" in trusted.stdout and "ANTHROPIC_API_KEY" not in trusted.stdout
    assert "ANTHROPIC_API_KEY" not in build_env(pass_secrets=True)


def test_output_is_redacted_and_truncated(tmp_path):
    register_secret("topsecretvalue99")
    r = run_command([sys.executable, "-c", "print('topsecretvalue99'); print('x'*5000)"], cwd=tmp_path, max_output=400)
    assert "topsecretvalue99" not in r.stdout
    assert "truncated" in r.stdout and len(r.stdout) < 700


def test_truncate_middle_keeps_tail():
    text = "A" * 1000 + "THE-END"
    assert truncate_middle(text, 100).endswith("THE-END")
    assert truncate_middle("short", 100) == "short"
