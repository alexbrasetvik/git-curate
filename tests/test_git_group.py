"""Tests for git_group.py."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import sh
from typer.testing import CliRunner

from git_curate.cli import app
from git_curate.common import (
    SHA_DISPLAY_LEN,
    Commit,
    RebaseFailedError,
    abort_session,
    git,
    load_failed_attempt,
    rebase_in_progress,
    save_failed_attempt,
)
from git_curate.group import (
    AmendEntry,
    Group,
    build_rebase_plan,
    execute_rebase,
    list_commits,
    resolve_message_to_sha,
    write_sequence_editor_script,
)
from git_curate.slice import slice_hunks

# ---------------------------------------------------------------------------
# resolve_message_to_sha
# ---------------------------------------------------------------------------


class TestResolveMessageToSha:
    def test_found(self) -> None:
        commits = [
            Commit(sha="aaa", message="temp: a.py:L1-5"),
            Commit(sha="bbb", message="temp: b.py:L1-3"),
        ]
        assert resolve_message_to_sha(commits, "temp: a.py:L1-5") == "aaa"

    def test_not_found_returns_none(self) -> None:
        commits = [Commit(sha="aaa", message="temp: a.py:L1-5")]
        assert resolve_message_to_sha(commits, "nonexistent") is None

    def test_empty_list_returns_none(self) -> None:
        assert resolve_message_to_sha([], "temp: a.py:L1-5") is None

    def test_returns_first_match(self) -> None:
        commits = [Commit(sha="aaa", message="dup"), Commit(sha="bbb", message="dup")]
        assert resolve_message_to_sha(commits, "dup") == "aaa"


# ---------------------------------------------------------------------------
# build_rebase_plan
# ---------------------------------------------------------------------------


class TestBuildRebasePlan:
    def _commits(self, *messages: str) -> list[Commit]:
        return [Commit(sha=f"sha{i:04d}", message=msg) for i, msg in enumerate(messages)]

    def test_all_grouped_into_one_group(self) -> None:
        commits = self._commits("temp: a.py:L1", "temp: a.py:L2")
        groups = [Group(message="feat: new thing", commits=["temp: a.py:L1", "temp: a.py:L2"])]
        plan = build_rebase_plan(commits, groups)
        assert plan[0] == "pick sha0000"
        assert plan[1] == AmendEntry("feat: new thing")
        assert plan[2] == "fixup sha0001"
        assert len(plan) == 3

    def test_ungrouped_commits_appended_as_pick(self) -> None:
        commits = self._commits("temp: a.py:L1", "temp: b.py:L1")
        groups = [Group(message="feat: a", commits=["temp: a.py:L1"])]
        plan = build_rebase_plan(commits, groups)
        assert "pick sha0001" in plan

    def test_empty_groups_all_ungrouped(self) -> None:
        commits = self._commits("temp: a.py:L1", "temp: b.py:L1")
        plan = build_rebase_plan(commits, [])
        assert plan == ["pick sha0000", "pick sha0001"]

    def test_missing_commit_message_raises_systemexit(self) -> None:
        commits = self._commits("temp: a.py:L1")
        groups = [Group(message="feat: x", commits=["DOES_NOT_EXIST"])]
        with pytest.raises(SystemExit) as exc_info:
            build_rebase_plan(commits, groups)
        assert exc_info.value.code == 1

    def test_commit_claimed_by_two_groups_raises_systemexit(self) -> None:
        commits = self._commits("temp: a.py:L1")
        groups = [
            Group(message="feat: x", commits=["temp: a.py:L1"]),
            Group(message="feat: y", commits=["temp: a.py:L1"]),
        ]
        with pytest.raises(SystemExit) as exc_info:
            build_rebase_plan(commits, groups)
        assert exc_info.value.code == 1

    def test_empty_commits_list_in_group_raises_systemexit(self) -> None:
        commits = self._commits("temp: a.py:L1")
        groups = [Group(message="feat: x", commits=[])]
        with pytest.raises(SystemExit):
            build_rebase_plan(commits, groups)

    def test_multiple_groups_correct_order(self) -> None:
        commits = self._commits("temp: a.py:L1", "temp: b.py:L1", "temp: c.py:L1")
        groups = [
            Group(message="feat: a+c", commits=["temp: a.py:L1", "temp: c.py:L1"]),
            Group(message="feat: b", commits=["temp: b.py:L1"]),
        ]
        plan = build_rebase_plan(commits, groups)
        assert plan[0] == "pick sha0000"
        assert plan[1] == AmendEntry("feat: a+c")
        assert plan[2] == "fixup sha0002"
        assert plan[3] == "pick sha0001"
        assert plan[4] == AmendEntry("feat: b")

    def test_group_commits_keep_their_original_order(self) -> None:
        commits = self._commits("temp: a.py:L1", "temp: b.py:L1", "temp: a.py:L1 again")
        groups = [Group(message="feat: a", commits=["temp: a.py:L1 again", "temp: b.py:L1", "temp: a.py:L1"])]
        plan = build_rebase_plan(commits, groups)
        assert plan == ["pick sha0000", AmendEntry("feat: a"), "fixup sha0001", "fixup sha0002"]

    def test_duplicate_commit_message_uses_last_sha(self) -> None:
        commits = [Commit(sha="sha_old", message="dup"), Commit(sha="sha_new", message="dup")]
        groups = [Group(message="feat: x", commits=["dup"])]
        plan = build_rebase_plan(commits, groups)
        assert "pick sha_new" in plan


# ---------------------------------------------------------------------------
# write_sequence_editor_script
# ---------------------------------------------------------------------------


class TestWriteSequenceEditorScript:
    def test_creates_executable_script(self, tmp_path: Path) -> None:
        script_path = str(tmp_path / "seq-editor.sh")
        write_sequence_editor_script(["pick abc123"], script_path)
        assert os.path.exists(script_path)
        assert os.access(script_path, os.X_OK)

    def test_plan_file_contains_todo_lines(self, tmp_path: Path) -> None:
        script_path = str(tmp_path / "seq-editor.sh")
        write_sequence_editor_script(["pick abc123", "fixup def456"], script_path)
        with open(script_path + ".plan") as f:
            content = f.read()
        assert "pick abc123" in content
        assert "fixup def456" in content

    def test_script_has_shebang(self, tmp_path: Path) -> None:
        script_path = str(tmp_path / "seq-editor.sh")
        write_sequence_editor_script(["pick abc"], script_path)
        with open(script_path) as f:
            content = f.read()
        assert content.startswith("#!/bin/sh")

    def test_script_works_as_sequence_editor(self, tmp_path: Path) -> None:
        script_path = str(tmp_path / "seq-editor.sh")
        todo_file = str(tmp_path / "git-rebase-todo")
        with open(todo_file, "w") as f:
            f.write("pick old_sha old message\n")
        write_sequence_editor_script(["pick new_sha new message"], script_path)
        sh.Command(script_path)(todo_file)
        with open(todo_file) as f:
            assert "pick new_sha new message" in f.read()


# ---------------------------------------------------------------------------
# list_commits (integration — requires git_repo fixture)
# ---------------------------------------------------------------------------


class TestListCommits:
    def test_returns_empty_when_no_commits_above_base(self, git_repo: Path) -> None:
        base = str(git("rev-parse", "HEAD")).strip()
        assert list_commits(base) == []

    def test_lists_commits_oldest_first(self, git_repo: Path) -> None:
        base = str(git("rev-parse", "HEAD")).strip()
        (git_repo / "f1.py").write_text("a=1\n")
        git.add(".", _cwd=git_repo)
        git.commit("--no-verify", "-m", "first", _cwd=git_repo)
        (git_repo / "f2.py").write_text("b=2\n")
        git.add(".", _cwd=git_repo)
        git.commit("--no-verify", "-m", "second", _cwd=git_repo)

        result = list_commits(base)
        assert len(result) == 2
        assert result[0].message == "first"
        assert result[1].message == "second"
        assert all(isinstance(c, Commit) for c in result)
        assert all(len(c.sha) == 40 for c in result)


# ---------------------------------------------------------------------------
# execute_rebase (integration — requires git_repo fixture)
# ---------------------------------------------------------------------------


class TestExecuteRebase:
    def _setup_temp_commits(self, git_repo: Path, n: int = 2) -> str:
        base = str(git("rev-parse", "HEAD")).strip()
        for i in range(n):
            (git_repo / f"temp{i}.py").write_text(f"x = {i}\n")
            git.add(".", _cwd=git_repo)
            git.commit("--no-verify", "-m", f"temp: temp{i}.py:L1-1", _cwd=git_repo)
        return base

    def test_squashes_two_commits_into_one(self, git_repo: Path) -> None:
        base = self._setup_temp_commits(git_repo, n=2)
        commits = list_commits(base)
        assert len(commits) == 2
        groups = [Group(message="feat: combined", commits=[c.message for c in commits])]
        plan = build_rebase_plan(commits, groups)
        execute_rebase(base, plan)

        log = str(git.log("--oneline", f"{base}..HEAD")).strip().splitlines()
        assert len(log) == 1
        assert "feat: combined" in log[0]

    def test_squash_all_slice_commits_to_one(self, git_repo: Path) -> None:
        # Two hunks sliced from a single staged diff, then grouped into one commit.
        # Models "we made several atomic changes; the result is one logical commit".
        content = "\n".join(f"line{i} = {i}" for i in range(1, 31)) + "\n"
        f = git_repo / "work.py"
        f.write_text(content)
        git.add(".", _cwd=git_repo)
        git.commit("--no-verify", "-m", "add work.py", _cwd=git_repo)
        base = str(git("rev-parse", "HEAD")).strip()

        lines = content.splitlines()
        lines[0] = "line1 = 999"
        lines[27] = "line28 = 999"
        f.write_text("\n".join(lines) + "\n")
        git.add("--", "work.py", _cwd=git_repo)
        assert slice_hunks([]) == 2

        commits = list_commits(base)
        assert len(commits) == 2

        groups = [Group(message="feat: final result", commits=[c.message for c in commits])]
        plan = build_rebase_plan(commits, groups)
        execute_rebase(base, plan)

        log = str(git.log("--oneline", f"{base}..HEAD")).strip().splitlines()
        assert len(log) == 1
        assert "feat: final result" in log[0]
        assert "line1 = 999" in f.read_text()
        assert "line28 = 999" in f.read_text()

    def test_reordering_past_a_move_ignores_directory_renames(self, git_repo: Path) -> None:
        # Moving old/a.py to new/a.py, then adding new/lib.py while old/lib.py
        # exists.  Picking the add before the move makes the merge base
        # (original parent) have new/a.py and HEAD have old/a.py, which git's
        # directory-rename detection reads as new/ -> old/ and so tries to put
        # new/lib.py at old/lib.py — an "implicit dir rename" conflict.
        (git_repo / "old").mkdir()
        (git_repo / "old" / "a.py").write_text("".join(f"a{i} = {i}\n" for i in range(20)))
        (git_repo / "old" / "lib.py").write_text("old_lib = True\n")
        git.add(".", _cwd=git_repo)
        git.commit("--no-verify", "-m", "add old/", _cwd=git_repo)
        base = str(git("rev-parse", "HEAD")).strip()

        (git_repo / "new").mkdir()
        git.mv("old/a.py", "new/a.py", _cwd=git_repo)
        git.commit("--no-verify", "-m", "temp: move a.py", _cwd=git_repo)
        (git_repo / "new" / "lib.py").write_text("new_lib = True\n")
        git.add(".", _cwd=git_repo)
        git.commit("--no-verify", "-m", "temp: add new/lib.py", _cwd=git_repo)

        commits = list_commits(base)
        groups = [
            Group(message="feat: new lib", commits=["temp: add new/lib.py"]),
            Group(message="refactor: move a.py", commits=["temp: move a.py"]),
        ]
        execute_rebase(base, build_rebase_plan(commits, groups))

        log = str(git.log("--format=%s", f"{base}..HEAD")).strip().splitlines()
        assert log == ["refactor: move a.py", "feat: new lib"]
        assert (git_repo / "new" / "lib.py").read_text() == "new_lib = True\n"
        assert (git_repo / "old" / "lib.py").read_text() == "old_lib = True\n"

    def test_stopped_rebase_is_aborted(self, git_repo: Path) -> None:
        # A paused rebase would lose the tool's -c settings on a manual
        # --continue, so a stop must leave HEAD back on the temp commits.
        base = self._setup_temp_commits(git_repo, n=2)
        tip = str(git("rev-parse", "HEAD")).strip()
        commits = list_commits(base)
        groups = [
            Group(message="feat: first", commits=[commits[0].message]),
            Group(message="feat: second", commits=[commits[1].message]),
        ]
        plan = build_rebase_plan(commits, groups)
        plan.insert(1, "exec echo stopping-here && false")
        with pytest.raises(RebaseFailedError) as exc_info:
            execute_rebase(base, plan)

        assert "stopping-here" in exc_info.value.output
        assert not rebase_in_progress()
        assert str(git("rev-parse", "HEAD")).strip() == tip
        assert not os.path.exists(str(git("rev-parse", "--git-path", "git-curate-group")).strip())

    def test_ungrouped_commits_preserved(self, git_repo: Path) -> None:
        base = self._setup_temp_commits(git_repo, n=2)
        commits = list_commits(base)
        groups = [Group(message="feat: first only", commits=[commits[0].message])]
        plan = build_rebase_plan(commits, groups)
        execute_rebase(base, plan)

        log = str(git.log("--oneline", f"{base}..HEAD")).strip().splitlines()
        assert len(log) == 2
        messages = "\n".join(log)
        assert "feat: first only" in messages
        assert "temp: temp1.py:L1-1" in messages


# ---------------------------------------------------------------------------
# group_command spec file handling
# ---------------------------------------------------------------------------


class TestGroupCommandSpecFile:
    _runner = CliRunner()

    def _setup_temp_commits(self, git_repo: Path, n: int = 2) -> str:
        base = str(git("rev-parse", "HEAD")).strip()
        for i in range(n):
            (git_repo / f"temp{i}.py").write_text(f"x = {i}\n")
            git.add(".", _cwd=git_repo)
            git.commit("--no-verify", "-m", f"temp: temp{i}.py:L1-1", _cwd=git_repo)
        return base

    def _write_spec(self, path: Path, base: str) -> None:
        commits = list_commits(base)
        groups = [{"message": "feat: combined", "commits": [c.message for c in commits]}]
        path.write_text(json.dumps(groups))

    @pytest.mark.parametrize(
        "extra_args, expect_exists",
        [
            ([], False),
            (["--keep-spec"], True),
        ],
        ids=["default-deletes", "keep-spec-preserves"],
    )
    def test_spec_file_handling(
        self,
        git_repo: Path,
        tmp_path: Path,
        extra_args: list[str],
        expect_exists: bool,
    ) -> None:
        base = self._setup_temp_commits(git_repo)
        spec_file = tmp_path / "groups.json"
        self._write_spec(spec_file, base)

        result = self._runner.invoke(app, ["group", base, "--spec", str(spec_file), *extra_args])
        assert result.exit_code == 0, result.output
        assert spec_file.exists() == expect_exists

    def test_success_points_to_curate_log(self, git_repo: Path, tmp_path: Path) -> None:
        base = self._setup_temp_commits(git_repo)
        spec_file = tmp_path / "groups.json"
        self._write_spec(spec_file, base)

        result = self._runner.invoke(app, ["group", base, "--spec", str(spec_file)])
        assert result.exit_code == 0, result.output
        assert f"git-curate log {base[:SHA_DISPLAY_LEN]}" in result.output


# ---------------------------------------------------------------------------
# Failed group attempts
# ---------------------------------------------------------------------------


class TestFailedAttempt:
    _runner = CliRunner()

    def _setup(self, git_repo: Path, tmp_path: Path) -> tuple[str, Path]:
        base = TestGroupCommandSpecFile()._setup_temp_commits(git_repo)
        spec_file = tmp_path / "groups.json"
        TestGroupCommandSpecFile()._write_spec(spec_file, base)
        return base, spec_file

    def _fail_commit_msg_hook(self, git_repo: Path) -> Path:
        # The amend steps run hooks, so a failing commit-msg hook stops the rebase.
        hook = git_repo / ".git" / "hooks" / "commit-msg"
        hook.write_text("#!/bin/sh\necho hook-says-no\nexit 1\n")
        hook.chmod(0o755)
        return hook

    def test_failed_group_saves_spec_and_output(self, git_repo: Path, tmp_path: Path) -> None:
        base, spec_file = self._setup(git_repo, tmp_path)
        self._fail_commit_msg_hook(git_repo)

        result = self._runner.invoke(app, ["group", base, "--spec", str(spec_file)])

        assert result.exit_code != 0
        assert not rebase_in_progress()
        attempt = load_failed_attempt(base)
        assert attempt is not None
        assert attempt.spec_text == spec_file.read_text()
        assert "hook-says-no" in attempt.output

    def test_successful_group_clears_saved_attempt(self, git_repo: Path, tmp_path: Path) -> None:
        base, spec_file = self._setup(git_repo, tmp_path)
        hook = self._fail_commit_msg_hook(git_repo)
        self._runner.invoke(app, ["group", base, "--spec", str(spec_file)])
        assert load_failed_attempt(base) is not None

        hook.unlink()
        result = self._runner.invoke(app, ["group", base, "--spec", str(spec_file)])

        assert result.exit_code == 0, result.output
        assert load_failed_attempt(base) is None

    def test_attempt_for_another_base_is_dropped(self, git_repo: Path) -> None:
        save_failed_attempt("0" * 40, "[]", "boom")
        assert load_failed_attempt("1" * 40) is None
        assert load_failed_attempt("0" * 40) is None

    def test_abort_session_clears_saved_attempt(self, git_repo: Path) -> None:
        base = str(git("rev-parse", "HEAD")).strip()
        save_failed_attempt(base, "[]", "boom")
        abort_session(base)
        assert load_failed_attempt(base) is None

    def test_rebase_that_never_started_reports_its_own_error(self, git_repo: Path) -> None:
        with pytest.raises(RebaseFailedError) as exc_info:
            execute_rebase("no-such-ref", ["pick 0000000"])
        assert "no-such-ref" in exc_info.value.output
