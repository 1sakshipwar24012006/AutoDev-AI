"""Run the project's test suite in a locked-down subprocess."""

from __future__ import annotations

import sys
from pathlib import Path

from workflow_agent.models import TestRunResult
from workflow_agent.security import validate_test_command
from workflow_agent.shell import CommandError, run_command

DEFAULT_TEST_COMMAND = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
PYTEST_NO_TESTS_COLLECTED = 5


class TestExecutor:
    __test__ = False  # not a pytest test class

    def __init__(self, command: list[str] | None = None, timeout: int = 300, max_output: int = 12000) -> None:
        self.command = validate_test_command(command or DEFAULT_TEST_COMMAND)
        self.timeout = timeout
        self.max_output = max_output

    def run(self, root: Path) -> TestRunResult:
        display = " ".join([Path(self.command[0]).name if self.command[0] == sys.executable else self.command[0],
                            *self.command[1:]])
        try:
            result = run_command(
                self.command,
                cwd=root,
                timeout=self.timeout,
                pass_secrets=False,  # AI-generated code runs here: no credentials in its environment
                extra_env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"},
                max_output=self.max_output,
            )
        except CommandError as exc:
            return TestRunResult(command=display, returncode=None, passed=False, output=str(exc), note="could not run")

        note = None
        if result.timed_out:
            note = f"timed out after {self.timeout}s"
        elif result.returncode == PYTEST_NO_TESTS_COLLECTED:
            note = "no tests were collected"
        return TestRunResult(
            command=display,
            returncode=result.returncode,
            passed=result.ok,
            output=result.output.strip(),
            duration_seconds=result.duration,
            timed_out=result.timed_out,
            note=note,
        )
