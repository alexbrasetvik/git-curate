"""Tests for harness/__init__.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from git_curate.common import Exit, git
from git_curate.harness import get_harness, resolve_harness_name, resolve_model
from git_curate.harness.claude import ClaudeHarness
from git_curate.harness.pi import PiHarness


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


def test_explicit_model_wins(git_repo: Path) -> None:
    git("config", "git-curate.model", "sonnet")
    assert resolve_model("opus") == "opus"


def test_model_from_git_config(git_repo: Path) -> None:
    git("config", "git-curate.model", "sonnet")
    assert resolve_model(None) == "sonnet"


def test_model_unset_leaves_harness_default(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    assert resolve_model(None) is None


def test_get_harness_passes_model() -> None:
    assert get_harness("claude", "opus").model == "opus"


def test_claude_passes_model() -> None:
    args = ClaudeHarness("opus").build_args("prompt", "/tmp/x")
    assert args[args.index("--model") + 1] == "opus"
    # The prompt must stay the value of -p.
    assert args[args.index("-p") + 1] == "prompt"


def test_pi_passes_model() -> None:
    args = PiHarness("sonnet").build_args("prompt")
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("-p") + 1] == "prompt"


def test_no_model_arg_by_default() -> None:
    assert "--model" not in ClaudeHarness().build_args("prompt", "/tmp/x")
    assert "--model" not in PiHarness().build_args("prompt")


