"""Tests for stack.py."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from git_curate.cli import app
from git_curate.common import git

runner = CliRunner()

CommitFn = Callable[..., str]


@pytest.fixture()
def commit(git_repo: Path) -> CommitFn:
    """Write files and commit them on the current branch; return the full SHA."""

    def _commit(message: str, *, author: str | None = None, date: str | None = None, **files: str) -> str:
        for name, content in files.items():
            (git_repo / name).write_text(content)
        git.add(".")
        extra = []
        if author:
            extra += ["--author", author]
        if date:
            extra += ["--date", date]
        git.commit("--no-verify", "-m", message, *extra)
        return str(git("rev-parse", "HEAD")).strip()

    return _commit


@pytest.fixture()
def feature(git_repo: Path) -> Path:
    git.checkout("-q", "-b", "feat")
    return git_repo


@pytest.fixture()
def mixed_branch(feature: Path, commit: CommitFn) -> dict[str, str]:
    """feat: two features on separate files, interleaved, with an unrelated fix in between."""
    return {
        "a1": commit("Add a", a="a1\na2\na3\n"),
        "b1": commit("Add b", b="b1\n"),
        "fix": commit("Fix readme", **{"README.md": "# fixed\n"}),
        "a2": commit("Tweak a", a="a1\nA2\na3\n"),
        "b2": commit("Extend b", b="b1\nb2\n"),
    }


def _run(*args: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["stack", *args])
    try:
        return result.exit_code, json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AssertionError(result.output) from None


def _short(sha: str) -> str:
    return sha[:12]


def _write_spec(path: Path, stacks: list[list[tuple[str, list[str]]]], trunk: str = "main") -> str:
    spec = {
        "trunk": trunk,
        "stacks": [{"layers": [{"name": n, "commits": c} for n, c in stack]} for stack in stacks],
    }
    path.write_text(json.dumps(spec))
    return str(path)


def _three_stacks(c: dict[str, str]) -> list[list[tuple[str, list[str]]]]:
    return [
        [("a/add", [c["a1"][:7]]), ("a/tweak", [c["a2"][:7]])],
        [("b", [c["b1"][:7], c["b2"][:7]])],
        [("fix/readme", [c["fix"][:7]])],
    ]


def _patch_id(sha: str) -> str:
    return str(git("patch-id", "--stable", _in=str(git.show(sha, "--")))).split()[0]


def commit_removal(message: str, path: str) -> str:
    git.rm("-q", path)
    git.commit("--no-verify", "-m", message)
    return str(git("rev-parse", "HEAD")).strip()


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


class TestAnalyze:
    def test_disjoint_changes_form_separate_components(self, mixed_branch: dict[str, str]) -> None:
        code, out = _run("analyze", "--trunk", "main")
        assert code == 0
        c = mixed_branch
        assert out["components"] == [
            [_short(c["a1"]), _short(c["a2"])],
            [_short(c["b1"]), _short(c["b2"])],
            [_short(c["fix"])],
        ]

    def test_same_lines_give_requires_edge(self, mixed_branch: dict[str, str]) -> None:
        _, out = _run("analyze", "--trunk", "main")
        requires = {entry["subject"]: entry["requires"] for entry in out["commits"]}
        assert requires["Tweak a"] == [_short(mixed_branch["a1"])]
        assert requires["Fix readme"] == []

    def test_reports_files(self, mixed_branch: dict[str, str]) -> None:
        _, out = _run("analyze", "--trunk", "main")
        assert {e["subject"]: e["files"] for e in out["commits"]}["Fix readme"] == ["README.md"]

    def test_requires_is_transitive(self, feature: Path, commit: CommitFn) -> None:
        a = commit("A", f="1\n2\n3\n")
        b = commit("B", f="1\nB\n3\n")
        commit("C", f="1\nC\n3\n")
        _, out = _run("analyze", "--trunk", "main")
        assert out["commits"][2]["requires"] == [_short(a), _short(b)]

    def test_file_replaced_by_directory_requires_the_removal(self, feature: Path, commit: CommitFn) -> None:
        commit("Add d", d="file\n")
        rm = commit_removal("Remove d", "d")
        (feature / "d").mkdir()
        commit("Add d/x", **{"d/x": "x\n"})
        _, out = _run("analyze", "--trunk", "main")
        assert out["commits"][2]["requires"] == [_short(rm)]

    def test_rejects_merge_commits(self, feature: Path, commit: CommitFn) -> None:
        commit("A", a="a\n")
        git.checkout("-q", "-b", "side", "main")
        commit("B", b="b\n")
        git.checkout("-q", "feat")
        git.merge("--no-edit", "side")
        code, out = _run("analyze", "--trunk", "main")
        assert code == 1
        assert "merge commits" in out["errors"][0]


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


class TestCheck:
    def test_valid_reorder_passes(self, mixed_branch: dict[str, str], tmp_path: Path) -> None:
        spec = _write_spec(tmp_path / "spec.json", _three_stacks(mixed_branch))
        code, out = _run("check", "--spec", spec)
        assert code == 0, out
        assert out["ok"] is True
        assert [b["name"] for b in out["stacks"][0]["layers"]] == ["a/add", "a/tweak"]

    def test_single_stack_in_new_order(self, mixed_branch: dict[str, str], tmp_path: Path) -> None:
        c = mixed_branch
        order = [c["fix"], c["a1"], c["a2"], c["b1"], c["b2"]]
        spec = _write_spec(tmp_path / "spec.json", [[("fix", order[:1]), ("a", order[1:3]), ("b", order[3:])]])
        code, out = _run("check", "--spec", spec)
        assert code == 0, out

    def test_marks_rewritten_commits(self, mixed_branch: dict[str, str], tmp_path: Path) -> None:
        spec = _write_spec(tmp_path / "spec.json", _three_stacks(mixed_branch))
        _, out = _run("check", "--spec", spec)
        a_commits = [e for b in out["stacks"][0]["layers"] for e in b["commits"]]
        # "Add a" is already first on trunk; "Tweak a" moved onto it.
        assert [e["rewritten"] for e in a_commits] == [False, True]

    def test_conflicting_reorder_reports_commit_and_paths(self, mixed_branch: dict[str, str], tmp_path: Path) -> None:
        c = mixed_branch
        stacks = _three_stacks(c)
        stacks[0] = [("a/tweak", [c["a2"]]), ("a/add", [c["a1"]])]
        spec = _write_spec(tmp_path / "spec.json", stacks)
        code, out = _run("check", "--spec", spec)
        assert code == 1
        assert out["ok"] is False
        assert out["conflict"] == {
            "layer": "a/tweak",
            "commit": _short(c["a2"]),
            "subject": "Tweak a",
            "paths": ["a"],
            "blocked_by": [_short(c["a1"])],
        }

    def test_writes_no_refs(self, mixed_branch: dict[str, str], tmp_path: Path) -> None:
        refs_before = str(git("for-each-ref"))
        spec = _write_spec(tmp_path / "spec.json", _three_stacks(mixed_branch))
        _run("check", "--spec", spec)
        assert str(git("for-each-ref")) == refs_before

    def test_spec_errors_are_all_reported(self, mixed_branch: dict[str, str], tmp_path: Path) -> None:
        c = mixed_branch
        stacks = [
            [("a", [c["a1"], c["a2"]]), ("a", [c["a1"]])],  # duplicate layer and commit
            [("b..bad", [c["b1"], "deadbeef"])],  # invalid name, unknown commit
        ]  # b2 and fix are missing
        spec = _write_spec(tmp_path / "spec.json", stacks)
        code, out = _run("check", "--spec", spec)
        assert code == 1
        errors = "\n".join(out["errors"])
        assert "layer 'a' appears more than once" in errors
        assert f"{_short(c['a1'])} is in both 'a' and 'a'" in errors
        assert "layer 'b..bad' is not a valid branch name" in errors
        assert "'deadbeef' is not a commit in main..HEAD" in errors
        assert f"{_short(c['b2'])} is not in any layer" in errors
        assert f"{_short(c['fix'])} is not in any layer" in errors

    def test_commit_outside_range_is_rejected(self, mixed_branch: dict[str, str], tmp_path: Path) -> None:
        trunk_sha = str(git("rev-parse", "main")).strip()
        stacks = _three_stacks(mixed_branch)
        stacks[2] = [("fix/readme", [mixed_branch["fix"], trunk_sha])]
        spec = _write_spec(tmp_path / "spec.json", stacks)
        code, out = _run("check", "--spec", spec)
        assert code == 1
        assert f"{trunk_sha!r} is not a commit in main..HEAD" in out["errors"]

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("not json", "invalid JSON"),
            ("[]", "JSON object"),
            ('{"trunk": "main", "stacks": []}', "'stacks' must be a non-empty list"),
            ('{"trunk": "main", "stacks": [{"layers": [{"name": "x", "commits": []}]}]}', "has no commits"),
        ],
    )
    def test_malformed_spec(self, mixed_branch: dict[str, str], tmp_path: Path, text: str, message: str) -> None:
        path = tmp_path / "spec.json"
        path.write_text(text)
        code, out = _run("check", "--spec", str(path))
        assert code == 1
        assert message in "\n".join(out["errors"])


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


class TestApply:
    def test_creates_branches_and_prints_gh_commands(self, mixed_branch: dict[str, str], tmp_path: Path) -> None:
        spec = _write_spec(tmp_path / "spec.json", _three_stacks(mixed_branch))
        code, out = _run("apply", "--spec", spec)
        assert code == 0, out
        assert out["created"] == ["a/add", "a/tweak", "b", "fix/readme"]
        assert out["gh_stack_init"] == [
            "gh stack init --base main a/add a/tweak",
            "gh stack init --base main b",
            "gh stack init --base main fix/readme",
        ]
        # Each layer is built on the one below it, the bottom one on trunk.
        assert str(git("rev-parse", "a/tweak^")).strip() == str(git("rev-parse", "a/add")).strip()
        assert str(git("rev-parse", "b~2")).strip() == str(git("rev-parse", "main")).strip()
        assert str(git("rev-parse", "fix/readme^")).strip() == str(git("rev-parse", "main")).strip()

    def test_preserves_patches(self, mixed_branch: dict[str, str], tmp_path: Path) -> None:
        spec = _write_spec(tmp_path / "spec.json", _three_stacks(mixed_branch))
        _run("apply", "--spec", spec)
        assert _patch_id("b~1") == _patch_id(mixed_branch["b1"])
        assert _patch_id("b") == _patch_id(mixed_branch["b2"])
        assert _patch_id("fix/readme") == _patch_id(mixed_branch["fix"])

    def test_preserves_message_and_author(self, feature: Path, commit: CommitFn, tmp_path: Path) -> None:
        a = commit("Add a\n\nWith a body.\n", a="a\n")
        b = commit(
            "Add b",
            author="Other Person <other@example.com>",
            date="2001-02-03T04:05:06+0100",
            b="b\n",
        )
        spec = _write_spec(tmp_path / "spec.json", [[("b", [b])], [("a", [a])]])
        _run("apply", "--spec", spec)
        fmt = "%an <%ae> %ad"
        assert str(git.log("-1", f"--format={fmt}", "--date=raw", "b", "--")) == str(
            git.log("-1", f"--format={fmt}", "--date=raw", b, "--")
        )
        assert str(git.log("-1", "--format=%B", "a", "--")) == str(git.log("-1", "--format=%B", a, "--"))

    def test_unchanged_prefix_keeps_shas(self, mixed_branch: dict[str, str], tmp_path: Path) -> None:
        c = mixed_branch
        spec = _write_spec(
            tmp_path / "spec.json",
            [[("one", [c["a1"], c["b1"]]), ("two", [c["fix"], c["a2"], c["b2"]])]],
        )
        _run("apply", "--spec", spec)
        assert str(git("rev-parse", "one")).strip() == c["b1"]
        assert str(git("rev-parse", "two")).strip() == c["b2"]

    def test_leaves_head_and_working_tree_alone(self, mixed_branch: dict[str, str], tmp_path: Path) -> None:
        (Path.cwd() / "untracked.txt").write_text("keep me\n")
        (Path.cwd() / "a").write_text("dirty\n")
        head = str(git("rev-parse", "HEAD")).strip()
        spec = _write_spec(tmp_path / "spec.json", _three_stacks(mixed_branch))
        code, _ = _run("apply", "--spec", spec)
        assert code == 0
        assert str(git("rev-parse", "HEAD")).strip() == head
        assert str(git("branch", "--show-current")).strip() == "feat"
        assert (Path.cwd() / "a").read_text() == "dirty\n"
        assert (Path.cwd() / "untracked.txt").read_text() == "keep me\n"

    def test_refuses_existing_branch_and_writes_nothing(self, mixed_branch: dict[str, str], tmp_path: Path) -> None:
        git.branch("b", "main")
        spec = _write_spec(tmp_path / "spec.json", _three_stacks(mixed_branch))
        code, out = _run("apply", "--spec", spec)
        assert code == 1
        assert out["errors"] == [f"branch 'b' already exists at {_short(str(git('rev-parse', 'main')).strip())}"]
        assert str(git("branch", "--list", "a/add")).strip() == ""

    def test_is_idempotent(self, mixed_branch: dict[str, str], tmp_path: Path) -> None:
        spec = _write_spec(tmp_path / "spec.json", _three_stacks(mixed_branch))
        _run("apply", "--spec", spec)
        tips = str(git("for-each-ref", "refs/heads"))
        code, out = _run("apply", "--spec", spec)
        assert code == 0
        assert out["created"] == []
        assert str(git("for-each-ref", "refs/heads")) == tips

    def test_reports_ref_conflict(self, mixed_branch: dict[str, str], tmp_path: Path) -> None:
        c = mixed_branch
        stacks = _three_stacks(c)
        stacks[1] = [("a", [c["b1"], c["b2"]])]  # "a" can't coexist with "a/add"
        spec = _write_spec(tmp_path / "spec.json", stacks)
        code, out = _run("apply", "--spec", spec)
        assert code == 1
        assert out["errors"]
        assert str(git("branch", "--list", "a/add")).strip() == ""

    def test_refuses_conflicting_spec(self, mixed_branch: dict[str, str], tmp_path: Path) -> None:
        c = mixed_branch
        stacks = _three_stacks(c)
        stacks[0] = [("a/tweak", [c["a2"]]), ("a/add", [c["a1"]])]
        spec = _write_spec(tmp_path / "spec.json", stacks)
        code, out = _run("apply", "--spec", spec)
        assert code == 1
        assert "conflict" in out
        assert str(git("branch", "--list", "b")).strip() == ""


# ---------------------------------------------------------------------------
# Worktrees
# ---------------------------------------------------------------------------


def test_works_in_linked_worktree(git_repo: Path, tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    git("worktree", "add", "-q", str(wt), "-b", "feat")
    os.chdir(wt)
    (wt / "a").write_text("a\n")
    git.add(".")
    git.commit("--no-verify", "-m", "Add a")
    sha = str(git("rev-parse", "HEAD")).strip()

    _, out = _run("analyze", "--trunk", "main")
    assert out["components"] == [[_short(sha)]]

    spec = _write_spec(tmp_path / "spec.json", [[("a", [sha])]])
    code, _ = _run("apply", "--spec", spec)
    assert code == 0
    assert str(git("rev-parse", "a", _cwd=str(git_repo))).strip() == sha
