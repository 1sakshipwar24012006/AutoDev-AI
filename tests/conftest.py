import shutil
from pathlib import Path

import pytest

from workflow_agent.config import Settings
from workflow_agent.demo import create_demo_repo, find_sample_app
from workflow_agent.security import clear_registered_secrets


@pytest.fixture(autouse=True)
def _clean_secrets():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(output_dir=tmp_path / "runs", test_timeout=120)


@pytest.fixture
def sample_copy(tmp_path) -> Path:
    """Plain (non-git) copy of the buggy sample app."""
    dest = tmp_path / "sample"
    shutil.copytree(find_sample_app(), dest, ignore=shutil.ignore_patterns("__pycache__", "issue.md"))
    return dest


@pytest.fixture
def demo_repo(tmp_path) -> Path:
    """Git repo (branch main, one commit) containing the buggy sample app."""
    return create_demo_repo(tmp_path / "repo")
