"""Safe subprocess execution: argv lists only, scrubbed env, timeouts, bounded output."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from workflow_agent.security import redact


class CommandError(Exception):
    """A command could not be run or exited non-zero when ``check=True``."""


@dataclass
class CommandResult:
    args: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        parts = [p for p in (self.stdout, self.stderr) if p]
        return "\n".join(parts)

    def display(self) -> str:
        return redact(" ".join(self.args))


_SECRET_ENV_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.IGNORECASE)
# Never forwarded to any child process, trusted or not.
_NEVER_FORWARD = frozenset({"ANTHROPIC_API_KEY"})


def build_env(*, pass_secrets: bool, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for a child process.

    ``pass_secrets=False`` (used for AI-generated code such as tests) strips anything that
    looks like a credential. ``pass_secrets=True`` (git / gh) keeps credentials such as
    GH_TOKEN or SSH_AUTH_SOCK but still never forwards ANTHROPIC_API_KEY.
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _NEVER_FORWARD:
            continue
        if not pass_secrets and _SECRET_ENV_RE.search(key):
            continue
        env[key] = value
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_PROMPT_DISABLED"] = "1"
    if extra:
        env.update(extra)
    return env


def truncate_middle(text: str, limit: int) -> str:
    """Keep the head and (mostly) the tail of long output; failures are usually at the end."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit // 4
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n... [{omitted} characters truncated] ...\n{text[-tail:]}"


def _resolve_executable(name: str) -> str:
    if os.path.isabs(name) and os.path.exists(name):
        return name
    found = shutil.which(name)
    if not found:
        raise CommandError(f"executable not found on PATH: {name!r}")
    return found


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover - Windows only
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_command(
    args: Sequence[str],
    *,
    cwd: str | Path,
    timeout: float = 120.0,
    pass_secrets: bool = False,
    extra_env: Mapping[str, str] | None = None,
    stdin_text: str | None = None,
    max_output: int = 20000,
    check: bool = False,
) -> CommandResult:
    """Run ``args`` (never through a shell) and capture bounded, redacted output."""
    if isinstance(args, (str, bytes)) or not args or not all(isinstance(a, str) for a in args):
        raise CommandError("command must be a non-empty list of strings (shell strings are not allowed)")
    if any("\x00" in a for a in args):
        raise CommandError("NUL byte in command argument")
    exe = _resolve_executable(args[0])
    argv = [exe, *args[1:]]

    kwargs: dict = dict(
        cwd=str(cwd),
        env=build_env(pass_secrets=pass_secrets, extra=extra_env),
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:  # pragma: no cover - Windows only
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    start = time.monotonic()
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except OSError as exc:
        raise CommandError(f"failed to start {args[0]!r}: {exc}") from exc

    timed_out = False
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        out, err = proc.communicate()
    duration = time.monotonic() - start

    result = CommandResult(
        args=list(args),
        returncode=None if timed_out else proc.returncode,
        stdout=redact(truncate_middle(out or "", max_output)),
        stderr=redact(truncate_middle(err or "", max_output)),
        duration=duration,
        timed_out=timed_out,
    )
    if check and not result.ok:
        reason = "timed out" if timed_out else f"exited with {result.returncode}"
        raise CommandError(f"`{result.display()}` {reason}:\n{result.output.strip()}")
    return result
