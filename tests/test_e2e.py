"""End-to-end tests: staged changes → slice → harness → final commits."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from git_curate import run
from git_curate.cli import app
from git_curate.common import (
    CURATE_AUTHOR_EMAIL,
    git,
    list_commits,
    load_failed_attempt,
    resolve_base,
    save_failed_attempt,
)
from git_curate.harness import BaseHarness, build_prompt
from git_curate.harness.claude import ClaudeHarness
from git_curate.harness.pi import PiHarness
from git_curate.slice import slice_hunks

runner = CliRunner()


class StubHarness(BaseHarness):
    """Groups all temp commits into a single final commit without invoking an AI."""

    def _run(self, base_sha: str, repo_root: str, temp_dir: str, spec_path: str) -> None:
        commits = list_commits(base_sha)
        spec = [
            {
                "message": "test: grouped by stub harness",
                "commits": [c.message for c in commits],
            }
        ]
        Path(spec_path).write_text(json.dumps(spec))
        result = runner.invoke(app, ["group", "--spec", spec_path, "--keep-spec"])
        if result.exit_code != 0:
            raise RuntimeError(f"git-curate group failed:\n{result.output}")


@pytest.fixture()
def staged_repo(git_repo: Path) -> Path:
    """Extend git_repo with a modified file and a new file, both staged."""
    (git_repo / "README.md").write_text("# repo\n\nExpanded content.\n")
    (git_repo / "main.py").write_text("def hello():\n    print('hello')\n")
    git.add(".")
    return git_repo


def _assert_session_complete(initial_commits: int = 1) -> None:
    assert resolve_base() is None, "Session still active after grouping"
    log = str(git.log("--oneline")).strip().splitlines()
    assert len(log) > initial_commits, "No new commits produced; log:\n" + "\n".join(log)
    temp_authors = [a for a in str(git.log("--format=%ae")).strip().splitlines() if a.strip() == CURATE_AUTHOR_EMAIL]
    assert not temp_authors, f"Temp commits still present after grouping ({len(temp_authors)} found)"


def test_stub_harness(staged_repo: Path) -> None:
    n = slice_hunks(paths=[])
    assert n > 0

    base_sha = resolve_base()
    assert base_sha is not None

    StubHarness().run(base_sha)

    _assert_session_complete()
    log = str(git.log("--oneline")).strip().splitlines()
    assert len(log) == 2
    assert "test: grouped by stub harness" in log[0]


@pytest.mark.claude
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_claude_harness(staged_repo: Path) -> None:
    n = slice_hunks(paths=[])
    assert n > 0

    base_sha = resolve_base()
    assert base_sha is not None

    ClaudeHarness().run(base_sha)

    _assert_session_complete()


@pytest.mark.pi
@pytest.mark.skipif(shutil.which("pi") is None, reason="pi CLI not installed")
def test_pi_harness(staged_repo: Path) -> None:
    n = slice_hunks(paths=[])
    assert n > 0

    base_sha = resolve_base()
    assert base_sha is not None

    PiHarness().run(base_sha)

    _assert_session_complete()


# ---------------------------------------------------------------------------
# Reusing a failed grouping attempt
# ---------------------------------------------------------------------------


class SpySeedHarness(BaseHarness):
    """Records what the spec path held when the agent would have started."""

    seen: str | None = None

    def _run(self, base_sha: str, repo_root: str, temp_dir: str, spec_path: str) -> None:
        SpySeedHarness.seen = Path(spec_path).read_text() if Path(spec_path).exists() else None


def _failed_session(staged_repo: Path) -> str:
    slice_hunks(paths=[])
    base_sha = resolve_base()
    assert base_sha is not None
    save_failed_attempt(base_sha, '[{"message": "m", "commits": []}]', "CONFLICT (content): Merge conflict in x.py")
    return base_sha


def test_harness_starts_from_failed_spec(staged_repo: Path) -> None:
    base_sha = _failed_session(staged_repo)
    SpySeedHarness().run(base_sha)
    assert SpySeedHarness.seen == '[{"message": "m", "commits": []}]'


def test_prompt_includes_failure_after_static_prefix(staged_repo: Path) -> None:
    base_sha = _failed_session(staged_repo)
    prompt = build_prompt(base_sha, "/tmp/spec.json")
    assert "CONFLICT (content): Merge conflict in x.py" in prompt
    assert prompt.index("Base: ") < prompt.index("A previous grouping attempt failed")


@pytest.mark.parametrize(
    "choice, retry, kept",
    [("r", True, True), ("k", False, True), ("d", False, False)],
    ids=["retry", "keep", "discard"],
)
def test_failed_attempt_prompt(
    staged_repo: Path, monkeypatch: pytest.MonkeyPatch, choice: str, retry: bool, kept: bool
) -> None:
    base_sha = _failed_session(staged_repo)
    # Pretend to be interactive and answer the prompt with *choice*.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("typer.prompt", lambda *a, **kw: choice)

    assert run._handle_failed_attempt(base_sha, yes=False) is retry
    assert (load_failed_attempt(base_sha) is not None) is kept


def test_failed_attempt_kept_without_prompt_under_yes(staged_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base_sha = _failed_session(staged_repo)

    def _no_prompt(*a: object, **kw: object) -> str:
        raise AssertionError("prompted under --yes")

    monkeypatch.setattr("typer.prompt", _no_prompt)
    assert run._handle_failed_attempt(base_sha, yes=True) is False
    assert load_failed_attempt(base_sha) is not None


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], 1),
        (["--split-new-files"], 2),
        (["--split-new-files", "--no-split-on-blank-lines"], 1),
        (["--hunk-per-line"], 4),
    ],
)
def test_bare_command_passes_slice_options(git_repo: Path, args: list[str], expected: int) -> None:
    (git_repo / "n.py").write_text("def f():\n    pass\n\ndef g():\n    pass\n")
    git.add("n.py")
    base = str(git("rev-parse", "HEAD")).strip()

    result = runner.invoke(app, ["--dry-run", *args])

    assert result.exit_code == 0, result.output
    assert len(list_commits(base)) == expected


def _commit_same_line_twice(git_repo: Path) -> str:
    """Change one line of README.md in two commits. Returns the first commit."""
    first = ""
    for text, message in (("# repo v2\n", "Bump to v2"), ("# repo v3\n", "Bump to v3")):
        (git_repo / "README.md").write_text(text)
        git.commit("--no-verify", "-am", message)
        first = first or str(git("rev-parse", "HEAD")).strip()
    return first


@pytest.mark.parametrize(("args", "expected"), [([], 2), (["--squash-first"], 1)])
def test_rewrite_slices_each_commit(git_repo: Path, args: list[str], expected: int) -> None:
    first = _commit_same_line_twice(git_repo)
    base = str(git("rev-parse", f"{first}^")).strip()

    result = runner.invoke(app, ["--rewrite-from", first, "--yes", "--dry-run", *args])

    assert result.exit_code == 0, result.output
    assert len(list_commits(base)) == expected
    assert (git_repo / "README.md").read_text() == "# repo v3\n"


