"""Tests for keeping the original authors when grouping rewritten commits."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from git_curate import provenance
from git_curate.authors import Ident, add_coauthor_trailers, parse_ident, plan_authorship
from git_curate.cli import app
from git_curate.common import git, list_commits, resolve_base
from git_curate.group import AmendEntry, _amend_command
from git_curate.provenance import SquashSource, _line_ranges
from git_curate.slice import _apply_from_squash, slice_commits, slice_hunks

runner = CliRunner()

ALICE = "Alice <alice@example.com>"
BOB = "Bob <bob@example.com>"
ME = "Test User <test@example.com>"


def _commit(path: Path, content: str, message: str, author: str | None = None, date: str | None = None) -> str:
    """Write *content* to *path* and commit it, optionally as *author* at *date*."""
    path.write_text(content)
    git.add(str(path))
    args = ["--no-verify", "-m", message]
    if author is not None:
        args.append(f"--author={author}")
    if date is not None:
        args.append(f"--date={date}")
    git.commit(*args)
    return str(git("rev-parse", "HEAD")).strip()


def _group(groups: list[tuple[str, list[str]]]) -> None:
    spec = json.dumps([{"message": message, "commits": commits} for message, commits in groups])
    result = runner.invoke(app, ["group"], input=spec)
    assert result.exit_code == 0, result.output


def _temp_subjects() -> list[str]:
    base = resolve_base()
    assert base is not None
    return [c.message for c in list_commits(base)]


def _final(rev: str) -> tuple[str, str, str]:
    """Return the author ident, ISO author date and body of *rev*."""
    out = str(git.log("-1", "--format=%an <%ae>%x00%aI%x00%b", rev))
    author, date, body = out.split("\x00")
    return author, date, body.strip()


def _temp_shas(base: str) -> list[str]:
    return str(git.log("--reverse", "--format=%H", f"{base}..HEAD")).split()


def _sources(rev: str = "HEAD") -> list[str]:
    """Return the Curate-Source values of *rev*."""
    out = str(git.log("-1", "--format=%(trailers:key=Curate-Source,valueonly)", rev))
    return [line.strip() for line in out.splitlines() if line.strip()]


def _squash(from_commit: str) -> None:
    """Squash from_commit..HEAD into the index and slice it, as slice --from --squash-first does."""
    squash_source = _apply_from_squash(from_commit)
    slice_hunks(paths=[], squash_source=squash_source)


@pytest.fixture()
def two_author_repo(git_repo: Path) -> tuple[str, str]:
    """Alice adds a.py, then Bob adds a shorter b.py. Returns (Alice's sha, Bob's sha)."""
    alice = _commit(
        git_repo / "a.py", "a = 1\nb = 2\nc = 3\n", "Add a.py", author=ALICE, date="2026-01-02T03:04:05+01:00"
    )
    bob = _commit(git_repo / "b.py", "x = 1\n", "Add b.py", author=BOB, date="2026-02-03T04:05:06+01:00")
    return alice, bob


class TestParseIdent:
    def test_ignores_git_var_timestamp(self) -> None:
        assert parse_ident("Alice <alice@example.com> 1700000000 +0100") == Ident("Alice", "alice@example.com")

    def test_not_an_ident(self) -> None:
        assert parse_ident("just a name") is None

    def test_key_ignores_name_and_case(self) -> None:
        assert Ident("A", "Alice@Example.com").key == Ident("Alice", "alice@example.com").key


class TestAddCoauthorTrailers:
    def test_appends_after_a_blank_line(self, git_repo: Path) -> None:
        assert add_coauthor_trailers("Add a\n", [BOB]) == f"Add a\n\nCo-authored-by: {BOB}\n"

    def test_skips_a_coauthor_the_message_names(self, git_repo: Path) -> None:
        message = "Add a\n\nCo-authored-by: Robert <BOB@example.com>\n"
        assert add_coauthor_trailers(message, [BOB]) == message

    def test_no_coauthors_leaves_message_unchanged(self, git_repo: Path) -> None:
        assert add_coauthor_trailers("Add a", []) == "Add a"


class TestAmendCommand:
    def test_resets_author_without_one(self) -> None:
        assert _amend_command(AmendEntry("m"), "/tmp/m 1.txt") == (
            "exec git commit --amend -F '/tmp/m 1.txt' --reset-author"
        )

    def test_sets_author_and_date(self) -> None:
        entry = AmendEntry("m", author=Ident("O'Brien", "ob@example.com"), date="2026-01-02T03:04:05+01:00")
        assert _amend_command(entry, "/tmp/m.txt") == (
            "exec git commit --amend -F /tmp/m.txt"
            " '--author=O'\"'\"'Brien <ob@example.com>' --date=2026-01-02T03:04:05+01:00"
        )


class TestPlanAuthorship:
    def test_staged_work_has_no_authorship(self, git_repo: Path) -> None:
        (git_repo / "a.py").write_text("a = 1\n")
        git.add("a.py")
        slice_hunks(paths=[])

        assert plan_authorship([[str(git("rev-parse", "HEAD")).strip()]]) == [None]

    def test_last_change_wins_and_others_coauthor(self, two_author_repo: tuple[str, str]) -> None:
        alice, _ = two_author_repo
        slice_commits(f"{alice}^")

        [authorship] = plan_authorship([_temp_shas(f"{alice}^")])

        # Alice changed more lines, but Bob's change came last.
        assert authorship is not None
        assert authorship.author == Ident("Bob", "bob@example.com")
        assert authorship.date == "2026-02-03T04:05:06+01:00"
        assert authorship.coauthors == [ALICE]

    def test_one_call_per_plan(self, two_author_repo: tuple[str, str]) -> None:
        alice, _ = two_author_repo
        slice_commits(f"{alice}^")
        a, b = _temp_shas(f"{alice}^")

        authorships = plan_authorship([[a], [b]])

        assert [x.author.name if x and x.author else None for x in authorships] == ["Alice", "Bob"]

    def test_carries_original_coauthor_trailers(self, git_repo: Path) -> None:
        first = _commit(git_repo / "a.py", "a = 1\n", f"Add a.py\n\nCo-authored-by: {BOB}", author=ALICE)
        slice_commits(f"{first}^")

        [authorship] = plan_authorship([_temp_shas(f"{first}^")])

        assert authorship is not None
        assert authorship.coauthors == [BOB]

    def test_own_commit_keeps_its_date(self, git_repo: Path) -> None:
        first = _commit(git_repo / "a.py", "a = 1\n", "Add a.py", date="2026-03-04T05:06:07+01:00")
        slice_commits(f"{first}^")

        [authorship] = plan_authorship([_temp_shas(f"{first}^")])

        assert authorship is not None
        assert authorship.author == Ident("Test User", "test@example.com")
        assert authorship.date == "2026-03-04T05:06:07+01:00"

    def test_staged_work_last_resets_author(self, git_repo: Path) -> None:
        first = _commit(git_repo / "a.py", "a = 1\n", "Add a.py", author=ALICE)
        slice_commits(f"{first}^")
        (git_repo / "b.py").write_text("x = 1\n")
        git.add("b.py")
        slice_hunks(paths=[])

        [authorship] = plan_authorship([_temp_shas(f"{first}^")])

        assert authorship is not None
        assert authorship.author is None
        assert authorship.coauthors == [ALICE]


class TestGroupKeepsAuthors:
    def test_separate_groups_keep_their_authors(self, two_author_repo: tuple[str, str]) -> None:
        alice, _ = two_author_repo
        slice_commits(f"{alice}^")
        a_temp, b_temp = _temp_subjects()

        _group([("Add a", [a_temp]), ("Add b", [b_temp])])

        assert _final("HEAD~1") == (ALICE, "2026-01-02T03:04:05+01:00", "")
        assert _final("HEAD") == (BOB, "2026-02-03T04:05:06+01:00", "")
        # The committer is whoever ran git-curate.
        assert str(git.log("-1", "--format=%cn <%ce>")).strip() == ME

    def test_mixed_group_credits_coauthor(self, two_author_repo: tuple[str, str]) -> None:
        alice, _ = two_author_repo
        slice_commits(f"{alice}^")

        _group([("Add a and b", _temp_subjects())])

        assert _final("HEAD") == (BOB, "2026-02-03T04:05:06+01:00", f"Co-authored-by: {ALICE}")

    def test_staged_work_still_resets_author(self, git_repo: Path) -> None:
        (git_repo / "a.py").write_text("a = 1\n")
        git.add("a.py")
        slice_hunks(paths=[])

        _group([("Add a", _temp_subjects())])

        author, _, body = _final("HEAD")
        assert (author, body) == (ME, "")


class TestLineRanges:
    def test_collapses_runs(self) -> None:
        assert _line_ranges([5, 1, 2, 3, 7]) == ["-L", "1,3", "-L", "5,5", "-L", "7,7"]

    def test_empty(self) -> None:
        assert _line_ranges([]) == []


class TestSquashProvenance:
    @pytest.fixture()
    def no_blame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(*args: object, **kwargs: object) -> None:
            raise AssertionError("blame ran on the fast path")

        monkeypatch.setattr(provenance, "_blame_hunks", fail)

    def test_single_author_takes_the_fast_path(self, git_repo: Path, no_blame: None) -> None:
        first = _commit(git_repo / "a.py", "a = 1\n", "Add a.py", author=ALICE)
        last = _commit(git_repo / "b.py", "x = 1\n", "Add b.py", author=ALICE)

        _squash(first)

        for sha in _temp_shas(f"{first}^"):
            assert _sources(sha) == [f"{last[:12]} Add b.py"]

    def test_coauthor_trailer_needs_blame(self, git_repo: Path) -> None:
        first = _commit(git_repo / "a.py", "a = 1\n", f"Add a.py\n\nCo-authored-by: {BOB}", author=ALICE)
        _commit(git_repo / "b.py", "x = 1\n", "Add b.py", author=ALICE)

        _squash(first)

        a_temp, b_temp = _temp_shas(f"{first}^")
        assert _sources(a_temp) == [f"{first[:12]} Add a.py"]

    def test_credits_each_file_to_its_author(self, two_author_repo: tuple[str, str]) -> None:
        alice, bob = two_author_repo

        _squash(alice)

        a_temp, b_temp = _temp_shas(f"{alice}^")
        assert _sources(a_temp) == [f"{alice[:12]} Add a.py"]
        assert _sources(b_temp) == [f"{bob[:12]} Add b.py"]

    def test_hunks_in_one_file(self, git_repo: Path) -> None:
        body = "".join(f"line{n} = {n}\n" for n in range(1, 21))
        base = _commit(git_repo / "f.py", body, "Add f.py")
        alice = _commit(git_repo / "f.py", body.replace("line2 = 2", "line2 = 'a'"), "Alice edits", author=ALICE)
        bob = _commit(
            git_repo / "f.py",
            body.replace("line2 = 2", "line2 = 'a'").replace("line18 = 18", "line18 = 'b'"),
            "Bob edits",
            author=BOB,
        )

        _squash(alice)

        top, bottom = _temp_shas(base)
        assert _sources(top) == [f"{alice[:12]} Alice edits"]
        assert _sources(bottom) == [f"{bob[:12]} Bob edits"]

    def test_line_changed_twice_credits_both(self, git_repo: Path) -> None:
        base = _commit(git_repo / "f.py", "a = 1\nb = 1\nc = 1\n", "Add f.py")
        alice = _commit(git_repo / "f.py", "a = 1\nb = 2\nc = 1\n", "Alice changes b", author=ALICE)
        bob = _commit(git_repo / "f.py", "a = 1\nb = 3\nc = 1\n", "Bob changes b", author=BOB)

        _squash(alice)

        [temp] = _temp_shas(base)
        # Bob wrote the surviving line; Alice deleted the original one.
        assert _sources(temp) == [f"{alice[:12]} Alice changes b", f"{bob[:12]} Bob changes b"]

    def test_deleted_lines_go_to_the_deleter(self, git_repo: Path) -> None:
        base = _commit(git_repo / "f.py", "a = 1\nb = 1\nc = 1\n", "Add f.py")
        alice = _commit(git_repo / "g.py", "x = 1\n", "Add g.py", author=ALICE)
        bob = _commit(git_repo / "f.py", "a = 1\nc = 1\n", "Bob drops b", author=BOB)

        _squash(alice)

        f_temp, g_temp = _temp_shas(base)
        assert _sources(f_temp) == [f"{bob[:12]} Bob drops b"]
        assert _sources(g_temp) == [f"{alice[:12]} Add g.py"]

    def test_deleted_file(self, git_repo: Path) -> None:
        base = _commit(git_repo / "f.py", "a = 1\n", "Add f.py")
        alice = _commit(git_repo / "g.py", "x = 1\n", "Add g.py", author=ALICE)
        git.rm("-q", "f.py")
        git.commit("--no-verify", "-m", "Bob removes f.py", f"--author={BOB}")
        bob = str(git("rev-parse", "HEAD")).strip()

        _squash(alice)

        f_temp, g_temp = _temp_shas(base)
        assert _sources(f_temp) == [f"{bob[:12]} Bob removes f.py"]

    def test_staged_beyond_the_range(self, git_repo: Path) -> None:
        first = _commit(git_repo / "a.py", "a = 1\nb = 1\n", "Add a.py", author=ALICE)
        (git_repo / "a.py").write_text("a = 1\nb = 1\nc = 1\n")
        git.add("a.py")

        _squash(first)

        [temp] = _temp_shas(f"{first}^")
        assert _sources(temp) == [f"{first[:12]} Add a.py", "staged"]

    def test_squash_then_group(self, two_author_repo: tuple[str, str]) -> None:
        alice, _ = two_author_repo
        _squash(alice)

        _group([("Add a and b", _temp_subjects())])

        assert _final("HEAD") == (BOB, "2026-02-03T04:05:06+01:00", f"Co-authored-by: {ALICE}")

    def test_squash_source_records_head(self, two_author_repo: tuple[str, str]) -> None:
        alice, bob = two_author_repo
        assert SquashSource.before_reset(f"{alice}^") == SquashSource(str(git("rev-parse", f"{alice}^")).strip(), bob)
