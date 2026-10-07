"""Tests for harness/__init__.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from git_curate.common import git
from git_curate.harness import resolve_harness_name


def test_explicit_name_wins(git_repo: Path) -> None:
    git("config", "git-curate.harness", "pi")
    assert resolve_harness_name("claude") == "claude"


def test_name_from_git_config(git_repo: Path) -> None:
    git("config", "git-curate.harness", "pi")
    assert resolve_harness_name(None) == "pi"


def test_defaults_to_claude_when_unset(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep a harness set in the user's own git config from leaking in.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    assert resolve_harness_name(None) == "claude"
