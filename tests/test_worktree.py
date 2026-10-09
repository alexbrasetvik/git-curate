"""Tests that git-curate works correctly inside git linked worktrees.

Concurrent agents typically each operate from a separate linked worktree of
the same repository.  With author-based session detection,
worktrees are naturally isolated: each has its own HEAD, so find_slice_base()
walks the correct branch without touching any shared state.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest
import sh

import git_curate.group as _group_mod
import git_curate.slice as _slice_mod
from git_curate.common import CURATE_AUTHOR_EMAIL, CURATE_AUTHOR_NAME, find_slice_base, git
from git_curate.group import Group

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(path: Path) -> None:
    """Initialize a git repo with an initial commit at *path*."""
    path.mkdir(parents=True, exist_ok=True)
    _git = git.bake(_cwd=str(path))
    _git.init("-b", "main")
    _git.config("user.email", "test@example.com")
    _git.config("user.name", "Test User")
    (path / "README.md").write_text("# repo\n")
    _git.add(".")
    _git.commit("--no-verify", "-m", "init")


def _add_worktree(main: Path, wt_path: Path, branch: str) -> None:
    git("worktree", "add", str(wt_path), "-b", branch, _cwd=str(main))


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def git_worktree(tmp_path: Path) -> Generator[tuple[Path, Path], None, None]:
    """Create a repo with a single linked worktree; chdir into the worktree."""
    original_cwd = os.getcwd()
    try:
        main = tmp_path / "main"
        _make_repo(main)
        wt = tmp_path / "feature-wt"
        _add_worktree(main, wt, "feature")
        os.chdir(wt)
        yield main, wt
    finally:
        os.chdir(original_cwd)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWorktree:
    def test_slice_works_in_linked_worktree(self, git_worktree: tuple[Path, Path]) -> None:
        """slice_hunks should create curate-authored temp commits from a linked worktree."""
        _, wt = git_worktree
        (wt / "feature.py").write_text("x = 1\n")
        git.add(".", _cwd=str(wt))
        git.commit("--no-verify", "-m", "add feature.py", _cwd=str(wt))
        (wt / "feature.py").write_text("x = 2\n")
        git.add("feature.py", _cwd=str(wt))

        n = _slice_mod.slice_hunks([])
        assert n == 1

        # Temp commit must be curate-authored
        head_author = str(git.log("-1", "--format=%ae")).strip()
        assert head_author == CURATE_AUTHOR_EMAIL

        # find_slice_base() must return the pre-slice commit
        log = str(git.log("--oneline"))
        assert "temp: feature.py:" in log

    def test_find_slice_base_correct_in_linked_worktree(self, git_worktree: tuple[Path, Path]) -> None:
        """find_slice_base() in a linked worktree returns the correct branch base."""
        _, wt = git_worktree

        (wt / "feature.py").write_text("x = 1\n")
        git.add(".", _cwd=str(wt))
        git.commit("--no-verify", "-m", "add feature.py", _cwd=str(wt))
        (wt / "feature.py").write_text("x = 2\n")
        git.add("feature.py", _cwd=str(wt))

        _slice_mod.slice_hunks([])

        assert find_slice_base() == str(git("rev-parse", "HEAD~1")).strip()

    def test_group_works_in_linked_worktree(self, git_worktree: tuple[Path, Path]) -> None:
        """execute_rebase should squash commits normally from a linked worktree."""
        _, wt = git_worktree
        base = str(git("rev-parse", "HEAD")).strip()

        import os as _os

        env = {
            **_os.environ,
            "GIT_AUTHOR_NAME": CURATE_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": CURATE_AUTHOR_EMAIL,
        }
        _git_curate = sh.git.bake("--no-pager", "-c", "color.ui=false", _tty_out=False, _env=env)
        for i in range(2):
            (wt / f"temp{i}.py").write_text(f"x = {i}\n")
            _git_curate.add(".", _cwd=str(wt))
            _git_curate.commit("--no-verify", "-m", f"temp: temp{i}.py:L1-1", _cwd=str(wt))

        commits = _group_mod.list_commits(base)
        assert len(commits) == 2

        groups = [Group(message="feat: combined", commits=[c.message for c in commits])]
        plan = _group_mod.build_rebase_plan(commits, groups)
        _group_mod.execute_rebase(base, plan)

        log = str(git.log("--oneline", f"{base}..HEAD")).strip().splitlines()
        assert len(log) == 1
        assert "feat: combined" in log[0]

    def test_concurrent_worktrees_do_not_interfere(self, tmp_path: Path) -> None:
        """Two linked worktrees slice independently with no shared state.

        Author-based detection eliminates the shared-ref collision risk: each
        worktree's HEAD is independent, so find_slice_base() always walks the
        correct branch.
        """
        main = tmp_path / "main"
        _make_repo(main)

        wt1 = tmp_path / "wt1"
        wt2 = tmp_path / "wt2"
        _add_worktree(main, wt1, "feat1")
        _add_worktree(main, wt2, "feat2")

        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": CURATE_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": CURATE_AUTHOR_EMAIL,
        }
        _git_curate = sh.git.bake("--no-pager", "-c", "color.ui=false", _tty_out=False, _env=env)

        # Slice in wt1
        (wt1 / "f1.py").write_text("x = 1\n")
        _git_curate.add(".", _cwd=str(wt1))
        _git_curate.commit("--no-verify", "-m", "temp: f1.py:L1-1", _cwd=str(wt1))

        # Slice in wt2
        (wt2 / "f2.py").write_text("y = 2\n")
        _git_curate.add(".", _cwd=str(wt2))
        _git_curate.commit("--no-verify", "-m", "temp: f2.py:L1-1", _cwd=str(wt2))

        original = os.getcwd()
        try:
            # find_slice_base() in wt1 must see only wt1's temp commit
            os.chdir(wt1)
            base1 = find_slice_base()
            wt1_authors = str(git.log("--format=%ae", f"{base1}..HEAD")).strip()
            wt1_subjects = str(git.log("--format=%s", f"{base1}..HEAD")).strip()
            assert all(e == CURATE_AUTHOR_EMAIL for e in wt1_authors.splitlines())

            # find_slice_base() in wt2 must see only wt2's temp commit
            os.chdir(wt2)
            base2 = find_slice_base()
            wt2_authors = str(git.log("--format=%ae", f"{base2}..HEAD")).strip()
            wt2_subjects = str(git.log("--format=%s", f"{base2}..HEAD")).strip()
            assert all(e == CURATE_AUTHOR_EMAIL for e in wt2_authors.splitlines())

            # Each worktree sees only its own curate commits above the base.
            assert "f1.py" in wt1_subjects
            assert "f2.py" not in wt1_subjects
            assert "f2.py" in wt2_subjects
            assert "f1.py" not in wt2_subjects
        finally:
            os.chdir(original)
