"""LLM abstraction: Claude (tool-use structured output) and a fixture-driven mock."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from workflow_agent.security import redact

log = logging.getLogger("workflow_agent")
T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The model call failed or returned unusable output."""


class LLMClient(Protocol):
    usage: dict[str, int]

    def structured(self, *, stage: str, system: str, user: str, schema: type[T]) -> T: ...


class ClaudeClient:
    """Calls the Anthropic Messages API, forcing a tool call so output matches a Pydantic schema."""

    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int = 8000,
        timeout: float = 120.0,
        validation_retries: int = 1,
        client: Any = None,
    ) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=3)
        self._client = client
        self.model = model
        self.max_tokens = max_tokens
        self.validation_retries = validation_retries
        self.usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def structured(self, *, stage: str, system: str, user: str, schema: type[T]) -> T:
        tool_name = f"submit_{stage}"
        tool = {
            "name": tool_name,
            "description": f"Submit the structured result for the '{stage}' stage.",
            "input_schema": schema.model_json_schema(),
        }
        prompt = user
        last_error = ""
        for attempt in range(self.validation_retries + 1):
            try:
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                    tools=[tool],
                    tool_choice={"type": "tool", "name": tool_name},
                )
            except Exception as exc:  # SDK raises various APIError subclasses
                raise LLMError(f"Claude API call failed ({type(exc).__name__}): {redact(str(exc))}") from exc

            usage = getattr(response, "usage", None)
            if usage is not None:
                self.usage["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
                self.usage["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
            self.usage["calls"] += 1

            if getattr(response, "stop_reason", None) == "max_tokens":
                raise LLMError(
                    f"model output was truncated at max_tokens={self.max_tokens} during '{stage}'; "
                    "raise AGENT_MAX_TOKENS or reduce the task scope"
                )
            payload = None
            for block in getattr(response, "content", []) or []:
                if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
                    payload = block.input
                    break
            if payload is None:
                raise LLMError(f"model did not call {tool_name}")
            try:
                return schema.model_validate(payload)
            except ValidationError as exc:
                last_error = str(exc)
                log.warning("stage %s: output failed validation (attempt %d)", stage, attempt + 1)
                prompt = (
                    f"{user}\n\nYour previous answer failed schema validation:\n{last_error}\n"
                    "Return a corrected answer."
                )
        raise LLMError(f"model output failed validation for '{stage}': {last_error}")


class MockLLMClient:
    """Deterministic client that replays canned responses per stage (no network, no cost)."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses: dict[str, list[dict]] = {
            stage: (value if isinstance(value, list) else [value]) for stage, value in responses.items()
        }
        self._counters: dict[str, int] = defaultdict(int)
        self.calls: list[str] = []
        self.usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    @classmethod
    def from_file(cls, path: str | Path) -> "MockLLMClient":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LLMError(f"cannot load mock fixture {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise LLMError("mock fixture must be a JSON object mapping stage -> response(s)")
        return cls(data)

    def structured(self, *, stage: str, system: str, user: str, schema: type[T]) -> T:
        sequence = self._responses.get(stage)
        if not sequence:
            raise LLMError(f"mock fixture has no responses for stage '{stage}'")
        index = min(self._counters[stage], len(sequence) - 1)
        self._counters[stage] += 1
        self.calls.append(stage)
        self.usage["calls"] += 1
        try:
            return schema.model_validate(sequence[index])
        except ValidationError as exc:
            raise LLMError(f"mock fixture response for '{stage}' is invalid: {exc}") from exc


def default_fixture_path() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "sample_app_fix.json"
