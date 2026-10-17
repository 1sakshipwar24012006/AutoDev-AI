from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from workflow_agent.llm import ClaudeClient, LLMError, MockLLMClient, default_fixture_path
from workflow_agent.models import ChangeSet, FileEdit, Plan, Review, ReviewConcern


def test_file_edit_validation():
    FileEdit(path="a", operation="replace_text", old_text="x", new_text="")
    FileEdit(path="a", operation="create", content="")
    for kwargs in (
        dict(operation="replace_text", old_text="", new_text="y"),
        dict(operation="replace_text", old_text="x"),
        dict(operation="replace_text", old_text="x", new_text="y", content="z"),
        dict(operation="create"),
        dict(operation="overwrite", content="c", old_text="x"),
    ):
        with pytest.raises(ValidationError):
            FileEdit(path="a", **kwargs)


def test_changeset_needs_edits():
    with pytest.raises(ValidationError):
        ChangeSet(explanation="x", commit_message="m", edits=[])


def test_blocker_forces_rejection():
    r = Review(approved=True, summary="s", concerns=[ReviewConcern(severity="major", message="bad")])
    assert r.approved is False
    assert Review(approved=True, summary="s", concerns=[ReviewConcern(severity="nit", message="meh")]).approved


def test_mock_client_sequences_and_repeats_last():
    mock = MockLLMClient({"plan": {"summary": "s", "root_cause": "r", "steps": ["a"], "test_strategy": "t"},
                          "review": [{"approved": False, "summary": "no"}, {"approved": True, "summary": "ok"}]})
    kw = dict(system="", user="")
    assert mock.structured(stage="plan", schema=Plan, **kw).summary == "s"
    assert not mock.structured(stage="review", schema=Review, **kw).approved
    assert mock.structured(stage="review", schema=Review, **kw).approved
    assert mock.structured(stage="review", schema=Review, **kw).approved  # last repeats
    with pytest.raises(LLMError, match="no responses"):
        mock.structured(stage="codegen", schema=ChangeSet, **kw)
    assert mock.calls == ["plan", "review", "review", "review"]


def test_bundled_fixture_is_valid():
    mock = MockLLMClient.from_file(default_fixture_path())
    kw = dict(system="", user="")
    mock.structured(stage="plan", schema=Plan, **kw)
    mock.structured(stage="codegen", schema=ChangeSet, **kw)
    mock.structured(stage="review", schema=Review, **kw)


def test_mock_from_missing_file(tmp_path):
    with pytest.raises(LLMError):
        MockLLMClient.from_file(tmp_path / "nope.json")


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def tool_response(payload, stop_reason="tool_use", name="submit_plan"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="..."), SimpleNamespace(type="tool_use", name=name, input=payload)],
        stop_reason=stop_reason, usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


GOOD_PLAN = {"summary": "s", "root_cause": "r", "steps": ["a"], "test_strategy": "t"}


def make_client(responses):
    fake = SimpleNamespace(messages=FakeMessages(responses))
    return ClaudeClient("sk-ant-unused", "test-model", client=fake), fake


def test_claude_client_forces_tool_and_tracks_usage():
    client, fake = make_client([tool_response(GOOD_PLAN)])
    plan = client.structured(stage="plan", system="sys", user="usr", schema=Plan)
    assert plan.summary == "s"
    call = fake.messages.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "submit_plan"}
    assert call["tools"][0]["input_schema"]["properties"]["summary"]
    assert call["model"] == "test-model" and call["system"] == "sys"
    assert client.usage == {"input_tokens": 10, "output_tokens": 5, "calls": 1}


def test_claude_client_retries_on_validation_error():
    client, fake = make_client([tool_response({"summary": "only"}), tool_response(GOOD_PLAN)])
    assert client.structured(stage="plan", system="", user="usr", schema=Plan).root_cause == "r"
    assert "failed schema validation" in fake.messages.calls[1]["messages"][0]["content"]


def test_claude_client_gives_up_after_retries():
    client, _ = make_client([tool_response({"summary": "x"}), tool_response({"summary": "x"})])
    with pytest.raises(LLMError, match="failed validation"):
        client.structured(stage="plan", system="", user="", schema=Plan)


def test_claude_client_error_paths_and_no_key_leak():
    client, _ = make_client([RuntimeError("401 bad key sk-ant-api03-LEAKYLEAKYLEAKY")])
    with pytest.raises(LLMError) as err:
        client.structured(stage="plan", system="", user="", schema=Plan)
    assert "LEAKYLEAKY" not in str(err.value)
    client, _ = make_client([tool_response(GOOD_PLAN, stop_reason="max_tokens")])
    with pytest.raises(LLMError, match="truncated"):
        client.structured(stage="plan", system="", user="", schema=Plan)
    client, _ = make_client([SimpleNamespace(content=[], stop_reason="end_turn", usage=None)])
    with pytest.raises(LLMError, match="did not call"):
        client.structured(stage="plan", system="", user="", schema=Plan)
