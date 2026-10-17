"""Configuration loading. The API key lives in a SecretStr and is never logged."""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Mapping

from dotenv import find_dotenv, load_dotenv
from pydantic import BaseModel, Field, SecretStr

from workflow_agent.security import register_secret

DEFAULT_MODEL = "claude-sonnet-5-5"


class ConfigError(Exception):
    """Invalid or missing configuration."""


class Settings(BaseModel):
    anthropic_api_key: SecretStr | None = None
    model: str = DEFAULT_MODEL
    max_tokens: int = Field(8000, ge=256, le=64000)
    llm_timeout: float = Field(120.0, gt=0)
    max_attempts: int = Field(3, ge=1, le=10)
    test_command: list[str] | None = None
    test_timeout: int = Field(300, ge=5, le=3600)
    max_context_chars: int = Field(60000, ge=2000, le=400000)
    max_file_bytes: int = Field(50000, ge=1000, le=1_000_000)
    github_repo: str | None = None
    output_dir: Path = Path("agent_runs")

    @classmethod
    def from_env(
        cls,
        env_file: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "Settings":
        """Build settings from the environment.

        ``.env`` is loaded only from an explicit ``env_file`` or the *launch directory*
        (never from the target repository, which is untrusted input).
        """
        if environ is None:
            if env_file is not None:
                path = Path(env_file)
                if not path.is_file():
                    raise ConfigError(f"env file not found: {path}")
                load_dotenv(path, override=False)
            else:
                found = find_dotenv(usecwd=True)
                if found:
                    load_dotenv(found, override=False)
            env: Mapping[str, str] = os.environ
        else:
            env = environ

        def get_int(name: str, default: int) -> int:
            raw = env.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be an integer") from exc

        key = (env.get("ANTHROPIC_API_KEY") or "").strip()
        if key:
            register_secret(key)

        test_cmd_raw = (env.get("AGENT_TEST_COMMAND") or "").strip()
        test_command = shlex.split(test_cmd_raw, posix=(os.name != "nt")) if test_cmd_raw else None

        try:
            return cls(
                anthropic_api_key=SecretStr(key) if key else None,
                model=(env.get("AGENT_MODEL") or DEFAULT_MODEL).strip(),
                max_tokens=get_int("AGENT_MAX_TOKENS", 8000),
                max_attempts=get_int("AGENT_MAX_ATTEMPTS", 3),
                test_command=test_command,
                test_timeout=get_int("AGENT_TEST_TIMEOUT", 300),
                max_context_chars=get_int("AGENT_MAX_CONTEXT_CHARS", 60000),
                github_repo=(env.get("AGENT_GITHUB_REPO") or "").strip() or None,
                output_dir=Path(env.get("AGENT_OUTPUT_DIR") or "agent_runs"),
            )
        except ValueError as exc:  # pydantic ValidationError subclasses ValueError
            raise ConfigError(f"invalid configuration: {exc}") from exc

    def require_api_key(self) -> str:
        if self.anthropic_api_key is None or not self.anthropic_api_key.get_secret_value():
            raise ConfigError(
                "ANTHROPIC_API_KEY is not set. Put it in your environment or a local .env file "
                "(see .env.example), or use --mock to run without the API."
            )
        return self.anthropic_api_key.get_secret_value()
