"""Tests for the environment git runs with."""

from __future__ import annotations

from pathlib import Path

import pytest
import sh

from git_curate.common import CURATE_AUTHOR_EMAIL, curate_git, git


def _printenv(cmd: sh.Command, name: str) -> str:
    """Return *name* as seen in the environment *cmd* runs git with."""
    # A "!" alias runs in a shell that inherits git's environment.
    return str(cmd("-c", f"alias.printenv=!printenv {name}", "printenv")).strip()


def test_git_runs_in_c_locale(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    assert _printenv(git, "LC_ALL") == "C"


def test_curate_git_runs_in_c_locale_with_curate_author(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    assert _printenv(curate_git, "LC_ALL") == "C"
    assert _printenv(curate_git, "GIT_AUTHOR_EMAIL") == CURATE_AUTHOR_EMAIL


@pytest.mark.parametrize("cmd", [git, curate_git], ids=["git", "curate_git"])
def test_env_changes_after_import_reach_git(git_repo: Path, monkeypatch: pytest.MonkeyPatch, cmd: sh.Command) -> None:
    monkeypatch.setenv("GIT_CURATE_TEST_VAR", "set-late")
    assert _printenv(cmd, "GIT_CURATE_TEST_VAR") == "set-late"
