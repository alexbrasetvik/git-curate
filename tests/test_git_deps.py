"""Tests for hunk-overlap dependency detection (deps.py)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from git_curate.cli import app
from git_curate.common import git, list_commits
from git_curate.deps import Dependency, compute_dependencies, parse_hunk_range
from git_curate.slice import slice_hunks

# ---------------------------------------------------------------------------
# parse_hunk_range
# ---------------------------------------------------------------------------


class TestParseHunkRange:
    def test_valid_message(self) -> None:
        assert parse_hunk_range("temp: src/foo.py:L10-20 #abc123-1") == ("src/foo.py", 10, 20)

    def test_nested_path(self) -> None:
        assert parse_hunk_range("temp: src/auth/models.py:L5-30 #def456-2") == ("src/auth/models.py", 5, 30)

    def test_single_line_range(self) -> None:
        assert parse_hunk_range("temp: foo.py:L7-7 #abc-3") == ("foo.py", 7, 7)

    def test_non_temp_message(self) -> None:
        assert parse_hunk_range("feat: add feature") is None

    def test_temp_without_hash(self) -> None:
        # Manually crafted temp messages used in some tests lack the #hash suffix.
        assert parse_hunk_range("temp: foo.py:L1-1") is None

    def test_empty_string(self) -> None:
        assert parse_hunk_range("") is None


# ---------------------------------------------------------------------------
# compute_dependencies (integration — requires a real git repo)
# ---------------------------------------------------------------------------


def _write_lines(path: Path, n: int = 30) -> None:
    path.write_text("\n".join(f"line{i:02d} = {i}" for i in range(1, n + 1)) + "\n")


def _modify_line(path: Path, index: int, value: str) -> None:
    """Replace the line at *index* (0-based) in *path*."""
    lines = path.read_text().splitlines()
    lines[index] = value
    path.write_text("\n".join(lines) + "\n")


class TestComputeDependencies:
    def test_overlapping_sessions_produce_dependency(self, git_repo: Path) -> None:
        f = git_repo / "target.py"
        _write_lines(f)
        git.add(".", _cwd=git_repo)
        git.commit("--no-verify", "-m", "add target.py", _cwd=git_repo)
        base = str(git("rev-parse", "HEAD")).strip()

        # Session 1: change line 10
        _modify_line(f, 9, "line10 = SESSION_A")
        git.add("--", "target.py", _cwd=git_repo)
        assert slice_hunks([]) == 1

        # Session 2: change the same line (overlapping!)
        _modify_line(f, 9, "line10 = SESSION_B")
        git.add("--", "target.py", _cwd=git_repo)
        assert slice_hunks([]) == 1

        deps = compute_dependencies(base)
        assert len(deps) == 1
        d = deps[0]
        assert isinstance(d, Dependency)
        assert "target.py" in d.earlier_msg
        assert "target.py" in d.later_msg
        # earlier commit was created first; later was created second
        commits = list_commits(base)
        assert d.earlier_msg == commits[0].message
        assert d.later_msg == commits[1].message

    def test_non_overlapping_lines_no_dependency(self, git_repo: Path) -> None:
        f = git_repo / "spread.py"
        _write_lines(f)
        git.add(".", _cwd=git_repo)
        git.commit("--no-verify", "-m", "add spread.py", _cwd=git_repo)
        base = str(git("rev-parse", "HEAD")).strip()

        # Session 1: change line 1
        _modify_line(f, 0, "line01 = TOP")
        git.add("--", "spread.py", _cwd=git_repo)
        slice_hunks([])

        # Session 2: change line 28 (far away, no overlap)
        _modify_line(f, 27, "line28 = BOTTOM")
        git.add("--", "spread.py", _cwd=git_repo)
        slice_hunks([])

        assert compute_dependencies(base) == []

    def test_different_files_no_dependency(self, git_repo: Path) -> None:
        fa = git_repo / "file_a.py"
        fb = git_repo / "file_b.py"
        fa.write_text("x = 1\n")
        fb.write_text("y = 2\n")
        git.add(".", _cwd=git_repo)
        git.commit("--no-verify", "-m", "add files", _cwd=git_repo)
        base = str(git("rev-parse", "HEAD")).strip()

        # Session 1: modify file_a
        fa.write_text("x = CHANGED\n")
        git.add("--", "file_a.py", _cwd=git_repo)
        slice_hunks([])

        # Session 2: modify file_b at the same line number
        fb.write_text("y = CHANGED\n")
        git.add("--", "file_b.py", _cwd=git_repo)
        slice_hunks([])

        assert compute_dependencies(base) == []

    def test_empty_session_no_dependency(self, git_repo: Path) -> None:
        git_repo / "empty.py"
        (git_repo / "empty.py").write_text("x = 1\n")
        git.add(".", _cwd=git_repo)
        git.commit("--no-verify", "-m", "add empty.py", _cwd=git_repo)
        base = str(git("rev-parse", "HEAD")).strip()

        # One session, no second session
        _modify = git_repo / "empty.py"
        _modify.write_text("x = 99\n")
        git.add("--", "empty.py", _cwd=git_repo)
        slice_hunks([])

        assert compute_dependencies(base) == []


# ---------------------------------------------------------------------------
# Ordering constraint enforcement via the group CLI
# ---------------------------------------------------------------------------


class TestOrderingConstraintEnforcement:
    _runner = CliRunner()

    def _setup_overlapping_session(self, git_repo: Path) -> tuple[str, list]:
        """Create two sessions that touch the same line; return (base, commits)."""
        f = git_repo / "constrained.py"
        _write_lines(f)
        git.add(".", _cwd=git_repo)
        git.commit("--no-verify", "-m", "add constrained.py", _cwd=git_repo)
        base = str(git("rev-parse", "HEAD")).strip()

        _modify_line(f, 9, "line10 = SESSION_1")
        git.add("--", "constrained.py", _cwd=git_repo)
        slice_hunks([])

        _modify_line(f, 9, "line10 = SESSION_2")
        git.add("--", "constrained.py", _cwd=git_repo)
        slice_hunks([])

        commits = list_commits(base)
        return base, commits

    def test_reversed_order_rejected(self, git_repo: Path, tmp_path: Path) -> None:
        base, commits = self._setup_overlapping_session(git_repo)
        assert len(commits) == 2

        # Spec deliberately reverses the order: later commit first.
        spec = [{"message": "feat: combined", "commits": [commits[1].message, commits[0].message]}]
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(spec))

        result = self._runner.invoke(app, ["group", "--spec", str(spec_path), base])
        assert result.exit_code != 0
        assert "must precede" in result.output

    def test_correct_order_accepted(self, git_repo: Path, tmp_path: Path) -> None:
        base, commits = self._setup_overlapping_session(git_repo)
        assert len(commits) == 2

        # Spec keeps the original chronological order: earlier commit first.
        spec = [{"message": "feat: combined", "commits": [commits[0].message, commits[1].message]}]
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(spec))

        result = self._runner.invoke(app, ["group", "--spec", str(spec_path), base])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Blocks split at a blank line
# ---------------------------------------------------------------------------


class TestBlankLineSplitDependencies:
    _runner = CliRunner()

    def _slice_two_adjacent_functions(self, git_repo: Path) -> tuple[str, list]:
        """Add two functions in one run of lines, as in one diff hunk; return (base, commits)."""
        # Commit the existing code to add to.
        f = git_repo / "funcs.py"
        f.write_text("import os\n\n\ndef main():\n    pass\n")
        git.add(".", _cwd=git_repo)
        git.commit("--no-verify", "-m", "add funcs.py", _cwd=git_repo)
        base = str(git("rev-parse", "HEAD")).strip()

        # Both functions go in with no unchanged line between them.
        f.write_text(
            "import os\n\n\ndef first():\n    return 1\n\n\ndef second():\n    return 2\n\n\ndef main():\n    pass\n"
        )
        git.add("--", "funcs.py", _cwd=git_repo)
        slice_hunks([])
        return base, list_commits(base)

    def test_pieces_are_ordered(self, git_repo: Path) -> None:
        base, commits = self._slice_two_adjacent_functions(git_repo)
        assert len(commits) == 2
        assert compute_dependencies(base) == [Dependency(earlier_msg=commits[0].message, later_msg=commits[1].message)]

    def test_pieces_regroup_into_separate_commits(self, git_repo: Path, tmp_path: Path) -> None:
        # The case that used to force both functions into one final commit.
        base, commits = self._slice_two_adjacent_functions(git_repo)
        spec = [
            {"message": "feat: first", "commits": [commits[0].message]},
            {"message": "feat: second", "commits": [commits[1].message]},
        ]
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(spec))

        result = self._runner.invoke(app, ["group", "--spec", str(spec_path), base])

        assert result.exit_code == 0, result.output

        # The first commit adds only the first function.
        assert str(git.log("--format=%s", f"{base}..HEAD")).split("\n")[:2] == ["feat: second", "feat: first"]
        assert "def second" not in str(git.show("HEAD~1:funcs.py"))


# ---------------------------------------------------------------------------
# Slicing by line
# ---------------------------------------------------------------------------


class TestByLineDependencies:
    _runner = CliRunner()

    def test_adjacent_imports_regroup_in_file_order(self, git_repo: Path, tmp_path: Path) -> None:
        f = git_repo / "mod.py"
        f.write_text("import os\n\n\ndef main():\n    pass\n")
        git.add(".", _cwd=git_repo)
        git.commit("--no-verify", "-m", "add mod.py", _cwd=git_repo)
        base = str(git("rev-parse", "HEAD")).strip()

        f.write_text("import os\nimport re\nimport sys\n\n\ndef main():\n    pass\n")
        git.add("--", "mod.py", _cwd=git_repo)
        slice_hunks([], by_line=True)
        commits = list_commits(base)

        # Adjacent pieces conflict if reordered, so they are chained.
        assert len(commits) == 2
        assert compute_dependencies(base) == [Dependency(earlier_msg=commits[0].message, later_msg=commits[1].message)]

        spec = [
            {"message": "feat: re", "commits": [commits[0].message]},
            {"message": "feat: sys", "commits": [commits[1].message]},
        ]
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(spec))

        result = self._runner.invoke(app, ["group", "--spec", str(spec_path), base])

        assert result.exit_code == 0, result.output
        assert str(git.show("HEAD~1:mod.py")).startswith("import os\nimport re\n\n")
