"""Tests for harness/__init__.py."""

from __future__ import annotations

import os
import threading
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


@pytest.mark.parametrize("harness_cls", [ClaudeHarness, PiHarness])
def test_failing_cli_exits_without_thread_traceback(
    harness_cls: type[ClaudeHarness | PiHarness], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stand in for the real CLI with one that fails the way pi does without credentials.
    name = "claude" if harness_cls is ClaudeHarness else "pi"
    fake = tmp_path / "bin" / name
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho 'No API key found' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(f"git_curate.harness.{name}.build_prompt", lambda base_sha, spec_path: "prompt")

    thread_errors: list[BaseException | None] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: thread_errors.append(args.exc_value))

    with pytest.raises(Exit) as exc_info:
        harness_cls()._run("base", str(tmp_path), str(tmp_path), str(tmp_path / "spec.json"))
    assert exc_info.value.code == 1
    assert thread_errors == []
