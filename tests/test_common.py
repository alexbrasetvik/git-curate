"""Tests for common.py: the environment git runs with, and finding the session base."""

from __future__ import annotations

from pathlib import Path

import pytest
import sh

from git_curate import common
from git_curate.common import CURATE_AUTHOR_EMAIL, curate_git, find_slice_base, git


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


class TestFindSliceBase:
    def _curate_commits(self, n: int) -> None:
        for i in range(n):
            curate_git.commit("--no-verify", "--allow-empty", "-m", f"temp: {i}")

    def test_base_on_a_later_page(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(common, "SLICE_BASE_PAGE", 2)
        base = str(git("rev-parse", "HEAD")).strip()
        self._curate_commits(5)
        assert find_slice_base() == base

    def test_all_curate_falls_back_to_root(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(common, "SLICE_BASE_PAGE", 2)
        git.checkout("-q", "--orphan", "all-curate")
        self._curate_commits(4)
        assert find_slice_base() == str(git("rev-list", "--max-parents=0", "HEAD")).strip()

    def test_reads_only_the_first_page(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for i in range(5):
            git.commit("--no-verify", "--allow-empty", "-m", f"history {i}")
        base = str(git("rev-parse", "HEAD")).strip()
        self._curate_commits(1)
        monkeypatch.setattr(common, "SLICE_BASE_PAGE", 2)
        calls: list[tuple[str, ...]] = []
        real_log = git.log

        class Spy:
            def log(self, *args: str) -> str:
                calls.append(args)
                return str(real_log(*args))

        monkeypatch.setattr(common, "git", Spy())
        assert find_slice_base() == base
        assert calls == [("-n2", "--skip=0", "--format=%H %ae", "HEAD")]


class TestGitVersion:
    @pytest.mark.parametrize(
        ("output", "expected"),
        [
            ("git version 2.43.0", (2, 43)),
            ("git version 2.50.1 (Apple Git-155)", (2, 50)),
            ("git version 2.43.0.windows.1", (2, 43)),
            ("git version 3.0.0-rc1", (3, 0)),
            ("not git", None),
        ],
    )
    def test_parse(self, output: str, expected: tuple[int, int] | None) -> None:
        assert common.parse_git_version(output) == expected

    def _fake_git_version(self, monkeypatch: pytest.MonkeyPatch, output: str) -> None:
        monkeypatch.setattr(common, "git", lambda *args: f"{output}\n")

    @pytest.mark.parametrize("output", ["git version 2.43.0", "git version 2.100.0", "git version 3.0.0", "custom"])
    def test_accepts_new_enough_or_unparseable(self, monkeypatch: pytest.MonkeyPatch, output: str) -> None:
        self._fake_git_version(monkeypatch, output)
        common.check_git_version()

    def test_rejects_older_than_2_43(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        self._fake_git_version(monkeypatch, "git version 2.42.2")
        with pytest.raises(common.GitTooOldError):
            common.check_git_version()
        assert capsys.readouterr().err == "fatal: git-curate needs git 2.43 or newer, found: git version 2.42.2\n"

    def test_cli_rejects_older_git_before_subcommand(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from typer.testing import CliRunner

        from git_curate.cli import app

        self._fake_git_version(monkeypatch, "git version 2.39.5")
        result = CliRunner().invoke(app, ["status"])

        assert result.exit_code == 1
        assert "needs git 2.43 or newer" in result.output

    def test_real_git_passes(self) -> None:
        common.check_git_version()
