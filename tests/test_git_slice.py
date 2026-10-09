"""Tests for git_slice.py."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from git_curate import slice as slice_mod
from git_curate.cli import app
from git_curate.common import RebaseInProgressError, SliceError, git
from git_curate.slice import (
    _dry_run_remaining,
    _is_diff_context,
    _make_hunk_header,
    _rebuild_sub_hunk_headers,
    _split_at_blank_lines,
    _split_at_lines,
    _split_hunk,
    apply_hunk,
    hunk_sides,
    parse_all_hunks,
    parse_hunk_header,
    slice_command,
    slice_hunks,
)

# ---------------------------------------------------------------------------
# Sample diff strings reused across tests
# ---------------------------------------------------------------------------

SINGLE_HUNK_DIFF = """\
diff --git a/foo.py b/foo.py
index aaa..bbb 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
 context
-old line
+new line
 context
"""

TWO_HUNK_DIFF = """\
diff --git a/foo.py b/foo.py
index aaa..bbb 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
 context
-old line 1
+new line 1
 context
@@ -10,3 +10,3 @@
 context
-old line 2
+new line 2
 context
"""

TWO_FILE_DIFF = """\
diff --git a/a.py b/a.py
index aaa..bbb 100644
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-old
+new
diff --git a/b.py b/b.py
index ccc..ddd 100644
--- a/b.py
+++ b/b.py
@@ -5 +5 @@
-x
+y
"""

BINARY_THEN_TEXT_DIFF = """\
diff --git a/data.bin b/data.bin
index aaa..bbb 100644
Binary files a/data.bin and b/data.bin differ
diff --git a/foo.py b/foo.py
index aaa..bbb 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
 context
-old line
+new line
 context
"""


# ---------------------------------------------------------------------------
# _is_diff_context
# ---------------------------------------------------------------------------


class TestIsCtx:
    def test_space_prefix(self) -> None:
        assert _is_diff_context(" context line\n") is True

    def test_backslash_prefix(self) -> None:
        assert _is_diff_context("\\ No newline at end of file\n") is True

    def test_bare_newline(self) -> None:
        assert _is_diff_context("\n") is True

    def test_crlf_bare(self) -> None:
        assert _is_diff_context("\r\n") is True

    def test_added_line(self) -> None:
        assert _is_diff_context("+new line\n") is False

    def test_removed_line(self) -> None:
        assert _is_diff_context("-old line\n") is False

    def test_hunk_header(self) -> None:
        assert _is_diff_context("@@ -1,3 +1,3 @@\n") is False

    def test_empty_string(self) -> None:
        assert _is_diff_context("") is False


# ---------------------------------------------------------------------------
# _make_hunk_header
# ---------------------------------------------------------------------------


class TestMakeHunkHeader:
    def test_basic(self) -> None:
        assert _make_hunk_header(1, 3, 1, 3) == "@@ -1,3 +1,3 @@\n"

    def test_with_label(self) -> None:
        result = _make_hunk_header(10, 5, 10, 6, " def foo")
        assert result == "@@ -10,5 +10,6 @@ def foo\n"

    def test_zero_count(self) -> None:
        assert _make_hunk_header(5, 0, 5, 3) == "@@ -5,0 +5,3 @@\n"


# ---------------------------------------------------------------------------
# _split_hunk
# ---------------------------------------------------------------------------


class TestSplitHunk:
    def _make_hunk(self, lines: list[str]) -> list[str]:
        return ["@@ -1,20 +1,20 @@\n"] + lines

    def test_no_split_single_change(self) -> None:
        lines = self._make_hunk([" ctx\n", "+add\n", " ctx\n"])
        assert _split_hunk(lines, min_context=1) is None

    def test_no_split_context_run_too_short(self) -> None:
        lines = self._make_hunk(["+change1\n", " ctx1\n", " ctx2\n", "+change2\n"])
        assert _split_hunk(lines, min_context=3) is None

    def test_no_split_at_no_newline_marker(self) -> None:
        # Editing a last line that lacks a newline puts a marker between - and +.
        lines = self._make_hunk(
            [
                " k\n",
                "-l\n",
                "\\ No newline at end of file\n",
                "+L\n",
                "\\ No newline at end of file\n",
            ]
        )
        assert _split_hunk(lines, min_context=1) is None

    def test_splits_at_sufficient_context_run(self) -> None:
        lines = self._make_hunk(
            [
                "+change1\n",
                " ctx1\n",
                " ctx2\n",
                " ctx3\n",
                "+change2\n",
            ]
        )
        result = _split_hunk(lines, min_context=3)
        assert result is not None
        assert len(result) == 2
        assert result[0][-1] == " ctx3\n"
        assert result[1][0] == " ctx1\n"

    def test_splits_into_three_sub_hunks(self) -> None:
        ctx = [" c\n"] * 3
        lines = self._make_hunk(["+a\n"] + ctx + ["+b\n"] + ctx + ["+c\n"])
        result = _split_hunk(lines, min_context=3)
        assert result is not None
        assert len(result) == 3

    def test_empty_body_returns_none(self) -> None:
        lines = ["@@ -1,0 +1,0 @@\n"]
        assert _split_hunk(lines, min_context=1) is None

    def test_only_context_no_split(self) -> None:
        lines = self._make_hunk([" ctx\n"] * 10)
        assert _split_hunk(lines, min_context=1) is None

    def test_removal_line_triggers_split(self) -> None:
        ctx = [" c\n"] * 4
        lines = self._make_hunk(["-removed\n"] + ctx + ["+added\n"])
        result = _split_hunk(lines, min_context=4)
        assert result is not None
        assert len(result) == 2

    def test_exact_min_context_boundary(self) -> None:
        # Exactly min_context=2 context lines → should split
        lines = self._make_hunk(["+a\n", " c1\n", " c2\n", "+b\n"])
        assert _split_hunk(lines, min_context=2) is not None
        # One fewer → should not split
        assert _split_hunk(lines, min_context=3) is None


# ---------------------------------------------------------------------------
# _rebuild_sub_hunk_headers
# ---------------------------------------------------------------------------


class TestRebuildSubHunkHeaders:
    HUNK_RE = re.compile(r"^@@ -(\d+),(\d+) \+(\d+),(\d+) @@")

    def test_two_sub_hunks_have_correct_structure(self) -> None:
        original_header = "@@ -1,8 +1,8 @@\n"
        sub_hunk_bodies = [
            ["+change1\n", " ctx1\n", " ctx2\n", " ctx3\n"],
            [" ctx1\n", " ctx2\n", " ctx3\n", "+change2\n"],
        ]
        result = _rebuild_sub_hunk_headers(original_header, sub_hunk_bodies)
        assert len(result) == 2
        for sub_hunk in result:
            assert self.HUNK_RE.match(sub_hunk[0]) is not None

    def test_first_sub_hunk_starts_at_original_position(self) -> None:
        original_header = "@@ -5,6 +10,6 @@\n"
        bodies = [["+x\n", " c\n"], [" c\n", "-y\n"]]
        result = _rebuild_sub_hunk_headers(original_header, bodies)
        m = self.HUNK_RE.match(result[0][0])
        assert m is not None
        assert m.group(1) == "5"
        assert m.group(3) == "10"

    def test_second_sub_hunk_position_advances(self) -> None:
        original_header = "@@ -1,6 +1,6 @@\n"
        # First body: 1 add + 3 ctx = old_count=3, new_count=4
        bodies = [
            ["+a\n", " c\n", " c\n", " c\n"],
            [" c\n", " c\n", " c\n", "-b\n"],
        ]
        result = _rebuild_sub_hunk_headers(original_header, bodies)
        m2 = self.HUNK_RE.match(result[1][0])
        assert m2 is not None
        # The shared context run starts right after "+a": old line 1, new line 2.
        assert int(m2.group(1)) == 1
        assert int(m2.group(3)) == 2

    def test_no_newline_marker_not_counted(self) -> None:
        original_header = "@@ -1,9 +1,9 @@\n"
        bodies = [
            ["-a\n", "+A\n", " c\n", " c\n", " c\n", " c\n"],
            [" c\n", " c\n", " c\n", " c\n", "-z\n", "\\ No newline at end of file\n", "+Z\n"],
        ]
        result = _rebuild_sub_hunk_headers(original_header, bodies)
        m2 = self.HUNK_RE.match(result[1][0])
        assert m2 is not None
        assert (int(m2.group(1)), int(m2.group(2)), int(m2.group(3)), int(m2.group(4))) == (2, 5, 2, 5)

    def test_label_preserved_on_all_sub_hunks(self) -> None:
        original_header = "@@ -10,5 +10,5 @@ def my_func\n"
        bodies = [["+x\n"], ["-y\n"]]
        result = _rebuild_sub_hunk_headers(original_header, bodies)
        for sub_hunk in result:
            assert " def my_func" in sub_hunk[0]


# ---------------------------------------------------------------------------
# parse_all_hunks
# ---------------------------------------------------------------------------

THREE_HUNK_DIFF = """\
diff --git a/foo.py b/foo.py
index aaa..bbb 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
 a
+inserted
 b
 c
@@ -20,4 +21,3 @@
 t
-removed
 u
 v
@@ -40,3 +40,3 @@
 x
-old
+new
 y
"""

RENAME_DIFF = """\
diff --git a/old.py b/new.py
similarity index 90%
rename from old.py
rename to new.py
index aaa..bbb 100644
--- a/old.py
+++ b/new.py
@@ -1,3 +1,3 @@
 a
-b
+B
 c
@@ -30,3 +30,3 @@
 x
-y
+Y
 z
"""


class TestParseAllHunks:
    def test_returns_empty_on_no_diff(self) -> None:
        assert parse_all_hunks("") == []
        assert parse_all_hunks("   \n") == []
        assert parse_all_hunks("not a diff\n") == []

    def test_single_hunk_single_file(self) -> None:
        (hunk,) = parse_all_hunks(SINGLE_HUNK_DIFF)
        assert hunk.file_path == "foo.py"
        assert hunk.first_in_file
        assert hunk.file.header_lines[0] == "diff --git a/foo.py b/foo.py\n"
        assert hunk.lines == ["@@ -1,3 +1,3 @@\n", " context\n", "-old line\n", "+new line\n", " context\n"]

    def test_two_hunk_diff_returns_both_hunks(self) -> None:
        first, second = parse_all_hunks(TWO_HUNK_DIFF)
        assert first.first_in_file and not second.first_in_file
        assert first.file is second.file

    def test_two_file_diff(self) -> None:
        hunks = parse_all_hunks(TWO_FILE_DIFF)
        assert [h.file_path for h in hunks] == ["a.py", "b.py"]
        assert all(h.first_in_file for h in hunks)

    def test_line_desc_format(self) -> None:
        (hunk,) = parse_all_hunks(SINGLE_HUNK_DIFF)
        assert re.match(r"L\d+-\d+$", hunk.line_desc)

    def test_min_context_zero_disables_split(self) -> None:
        ctx = " c\n" * 5
        diff = (
            "diff --git a/f.py b/f.py\n"
            "index aaa..bbb 100644\n"
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1,5 +1,7 @@\n"
            "+change1\n" + ctx + "+change2\n"
        )
        assert len(parse_all_hunks(diff, min_context=3)) == 2
        assert len(parse_all_hunks(diff, min_context=0)) == 1

    def test_skips_binary_file(self) -> None:
        hunks = parse_all_hunks(BINARY_THEN_TEXT_DIFF)
        assert [h.file_path for h in hunks] == ["foo.py"]

    def test_all_binary_returns_empty(self) -> None:
        diff = """\
diff --git a/a.bin b/a.bin
index aaa..bbb 100644
Binary files a/a.bin and b/a.bin differ
diff --git a/b.bin b/b.bin
index ccc..ddd 100644
Binary files a/b.bin and b/b.bin differ
"""
        assert parse_all_hunks(diff) == []

    def test_old_offsets_account_for_earlier_hunks(self) -> None:
        hunks = parse_all_hunks(THREE_HUNK_DIFF)
        assert [parse_hunk_header(h.lines[0])[:2] for h in hunks] == [(1, 3), (21, 4), (40, 3)]
        # line_desc is in final-file coordinates.
        assert [h.line_desc for h in hunks] == ["L1-4", "L21-23", "L40-42"]

    def test_rename_belongs_to_every_hunk_of_the_file(self) -> None:
        first, second = parse_all_hunks(RENAME_DIFF)
        assert first.file_path == second.file_path == "new.py"
        assert (first.file.old_path, first.file.new_path) == ("old.py", "new.py")
        assert first.first_in_file and not second.first_in_file

    def test_split_sub_hunks_trim_leading_context(self) -> None:
        body = ["+a\n"] + [f" c{i}\n" for i in range(5)] + ["+b\n"]
        diff = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,5 +1,7 @@\n" + "".join(body)
        first, second = parse_all_hunks(diff, min_context=4)
        # First sub-hunk carries the whole context run as trailing context.
        assert first.lines[1:] == body[:6]
        # Second keeps DIFF_CONTEXT lines of leading context, positioned after the first sub-hunk.
        assert second.lines[1:] == [" c2\n", " c3\n", " c4\n", "+b\n"]
        # c2 is old line 3, shifted by the first sub-hunk's "+a" to 4; it is new line 4.
        assert parse_hunk_header(second.lines[0])[:4] == (4, 3, 4, 4)

    def test_quoted_path_is_skipped(self) -> None:
        diff = (
            'diff --git "a/tab\\there" "b/tab\\there"\n'
            'index aaa..bbb 100644\n--- "a/tab\\there"\n+++ "b/tab\\there"\n'
            "@@ -1 +1 @@\n-x\n+y\n" + THREE_HUNK_DIFF
        )
        hunks = parse_all_hunks(diff)
        assert {h.file_path for h in hunks} == {"foo.py"}
        assert len(hunks) == 3

    def test_form_feed_does_not_split_lines(self) -> None:
        diff = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-a\x0cb\n+c\x0cd\n"
        (hunk,) = parse_all_hunks(diff)
        assert hunk.lines[1:] == ["-a\x0cb\n", "+c\x0cd\n"]


# ---------------------------------------------------------------------------
# _split_at_blank_lines
# ---------------------------------------------------------------------------


def _replay(old: list[str], hunks: list[list[str]]) -> list[str]:
    lines = list(old)
    for hunk in hunks:
        apply_hunk(lines, hunk)
    return lines


class TestSplitAtBlankLines:
    # Two sibling blocks added between "a b" and "c d". The first has a blank line inside its body.
    OLD = ["a\n", "b\n", "c\n", "d\n"]
    ADDED = ["def f():\n", "    x = 1\n", "\n", "    return x\n", "\n", "def g():\n", "    pass\n", "\n"]
    HUNK = ["@@ -1,4 +1,12 @@\n", " a\n", " b\n", *("+" + line for line in ADDED), " c\n", " d\n"]

    def test_cuts_between_sibling_blocks_only(self) -> None:
        first, second = _split_at_blank_lines(self.HUNK)

        # The blank inside f is followed by a deeper line, so f stays whole.
        assert first == ["@@ -1,4 +1,9 @@\n", *self.HUNK[1:8], " c\n", " d\n"]

        # g's leading context is the end of f, which is in the file by the time g applies.
        assert second == ["@@ -5,5 +5,8 @@\n", " \n", "     return x\n", " \n", *self.HUNK[8:]]

        assert _replay(self.OLD, [first, second]) == ["a\n", "b\n", *self.ADDED, "c\n", "d\n"]

    def test_whitespace_only_line_counts_as_blank(self) -> None:
        hunk = ["@@ -1,1 +1,4 @@\n", " a\n", "+one\n", "+  \t\n", "+two\n"]
        first, second = _split_at_blank_lines(hunk)
        assert first == ["@@ -1,1 +1,3 @@\n", " a\n", "+one\n", "+  \t\n"]
        assert second == ["@@ -1,3 +1,4 @@\n", " a\n", " one\n", "   \t\n", "+two\n"]
        assert _replay(["a\n"], [first, second]) == ["a\n", "one\n", "  \t\n", "two\n"]

    def test_leading_and_trailing_blanks_do_not_cut(self) -> None:
        hunk = ["@@ -1,1 +1,5 @@\n", " a\n", "+\n", "+one\n", "+two\n", "+\n"]
        assert _split_at_blank_lines(hunk) == [hunk]

    def test_removed_blocks_split_with_old_side_context(self) -> None:
        old = ["a\n", "one\n", "\n", "two\n", "b\n"]
        hunk = ["@@ -1,5 +1,2 @@\n", " a\n", "-one\n", "-\n", "-two\n", " b\n"]
        first, second = _split_at_blank_lines(hunk)

        # "two" isn't removed yet when the first piece applies, so it is context there.
        assert first == ["@@ -1,5 +1,3 @@\n", " a\n", "-one\n", "-\n", " two\n", " b\n"]
        assert second == ["@@ -1,3 +1,2 @@\n", " a\n", "-two\n", " b\n"]

        assert _replay(old, [first, second]) == ["a\n", "b\n"]

    def test_replacement_stays_whole(self) -> None:
        hunk = ["@@ -1,3 +1,5 @@\n", " a\n", "-old\n", "+one\n", "+\n", "+two\n", " b\n"]
        assert _split_at_blank_lines(hunk) == [hunk]

    def test_no_newline_marker_stays_with_last_piece(self) -> None:
        hunk = ["@@ -1 +1,4 @@\n", " a\n", "+one\n", "+\n", "+two\n", "\\ No newline at end of file\n"]
        pieces = _split_at_blank_lines(hunk)
        assert len(pieces) == 2
        assert pieces[1][-1] == "\\ No newline at end of file\n"
        assert _replay(["a\n"], pieces) == ["a\n", "one\n", "\n", "two"]


class TestParseAllHunksBlankLines:
    def test_blank_line_cut_after_context_split(self) -> None:
        old = ["x\n", *(f"c{i}\n" for i in range(5)), "y\n"]
        body = ["-x\n", "+X\n", *(f" c{i}\n" for i in range(5)), "+one\n", "+\n", "+two\n", " y\n"]
        diff = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,7 +1,10 @@\n" + "".join(body)

        hunks = parse_all_hunks(diff, min_context=4)

        # One cut at the c0..c4 context run, one at the blank line.
        assert len(hunks) == 3
        new = ["X\n", *(f"c{i}\n" for i in range(5)), "one\n", "\n", "two\n", "y\n"]
        assert _replay(old, [h.lines for h in hunks]) == new

    def test_new_file(self) -> None:
        diff = (
            "diff --git a/n b/n\nnew file mode 100644\n--- /dev/null\n+++ b/n\n@@ -0,0 +1,5 @@\n+one\n+1\n+\n+two\n+2\n"
        )
        hunks = parse_all_hunks(diff)
        assert [h.line_desc for h in hunks] == ["L1-3", "L1-5"]
        assert _replay([], [h.lines for h in hunks]) == ["one\n", "1\n", "\n", "two\n", "2\n"]

    def test_no_split_on_blank_lines_keeps_new_code_whole(self) -> None:
        diff = (
            "diff --git a/n b/n\nnew file mode 100644\n--- /dev/null\n+++ b/n\n@@ -0,0 +1,5 @@\n+one\n+1\n+\n+two\n+2\n"
        )
        (hunk,) = parse_all_hunks(diff, split_on_blank_lines=False)
        assert hunk.line_desc == "L1-5"

    def test_no_split_on_blank_lines_still_splits_at_context(self) -> None:
        body = ["-x\n", "+X\n", *(f" c{i}\n" for i in range(5)), "+one\n", "+\n", "+two\n", " y\n"]
        diff = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,7 +1,10 @@\n" + "".join(body)
        assert len(parse_all_hunks(diff, min_context=4, split_on_blank_lines=False)) == 2

    def test_deleted_file_stays_whole(self) -> None:
        diff = "diff --git a/n b/n\ndeleted file mode 100644\n--- a/n\n+++ /dev/null\n@@ -1,3 +0,0 @@\n-one\n-\n-two\n"
        assert len(parse_all_hunks(diff)) == 1


# ---------------------------------------------------------------------------
# _split_at_lines
# ---------------------------------------------------------------------------


class TestSplitAtLines:
    def _bodies(self, pieces: list[list[str]]) -> list[list[str]]:
        return [[line for line in piece[1:] if line.startswith(("+", "-"))] for piece in pieces]

    def test_added_lines_split_one_per_piece(self) -> None:
        old = ["import os\n", "\n", "x\n"]
        hunk = ["@@ -1,3 +1,5 @@\n", " import os\n", "+import a\n", "+import b\n", " \n", " x\n"]
        pieces = _split_at_lines(hunk)

        assert self._bodies(pieces) == [["+import a\n"], ["+import b\n"]]
        assert _replay(old, pieces) == ["import os\n", "import a\n", "import b\n", "\n", "x\n"]

    def test_removed_lines_split_one_per_piece(self) -> None:
        old = ["a\n", "b\n", "c\n", "d\n"]
        hunk = ["@@ -1,4 +1,2 @@\n", " a\n", "-b\n", "-c\n", " d\n"]
        pieces = _split_at_lines(hunk)

        assert self._bodies(pieces) == [["-b\n"], ["-c\n"]]
        assert _replay(old, pieces) == ["a\n", "d\n"]

    def test_edited_lines_pair_removed_with_added(self) -> None:
        old = ["from x import a\n", "from y import b\n", "z\n"]
        hunk = [
            "@@ -1,3 +1,4 @@\n",
            "-from x import a\n",
            "-from y import b\n",
            "+from x import a, c\n",
            "+from y import b, d\n",
            "+from w import e\n",
            " z\n",
        ]
        pieces = _split_at_lines(hunk)

        assert self._bodies(pieces) == [
            ["-from x import a\n", "+from x import a, c\n"],
            ["-from y import b\n", "+from y import b, d\n"],
            ["+from w import e\n"],
        ]
        assert _replay(old, pieces) == ["from x import a, c\n", "from y import b, d\n", "from w import e\n", "z\n"]

    def test_regions_split_across_short_context(self) -> None:
        old = ["a\n", "b\n", "c\n"]
        hunk = ["@@ -1,3 +1,3 @@\n", "-a\n", "+A\n", " b\n", "-c\n", "+C\n"]
        pieces = _split_at_lines(hunk)

        assert self._bodies(pieces) == [["-a\n", "+A\n"], ["-c\n", "+C\n"]]
        assert _replay(old, pieces) == ["A\n", "b\n", "C\n"]

    def test_blank_line_joins_the_line_before(self) -> None:
        hunk = ["@@ -1,1 +1,4 @@\n", " a\n", "+\n", "+one\n", "+\n", "+two\n"]
        pieces = _split_at_lines(hunk)

        assert self._bodies(pieces) == [["+\n", "+one\n", "+\n"], ["+two\n"]]
        assert _replay(["a\n"], pieces) == ["a\n", "\n", "one\n", "\n", "two\n"]

    def test_no_newline_marker_stays_with_its_line(self) -> None:
        hunk = ["@@ -1 +1,3 @@\n", " a\n", "+one\n", "+two\n", "\\ No newline at end of file\n"]
        pieces = _split_at_lines(hunk)

        assert pieces[1][-1] == "\\ No newline at end of file\n"
        assert _replay(["a\n"], pieces) == ["a\n", "one\n", "two"]

    def test_edit_with_no_newline_marker_stays_whole(self) -> None:
        hunk = ["@@ -1,2 +1,2 @@\n", "-a\n", "-b\n", "\\ No newline at end of file\n", "+A\n", "+B\n"]
        assert _split_at_lines(hunk) == [hunk]

    def test_single_line_comes_back_unchanged(self) -> None:
        hunk = ["@@ -1,2 +1,3 @@\n", " a\n", "+b\n", " c\n"]
        assert _split_at_lines(hunk) == [hunk]


class TestParseAllHunksHunkPerLine:
    def test_new_file(self) -> None:
        diff = "diff --git a/n b/n\nnew file mode 100644\n--- /dev/null\n+++ b/n\n@@ -0,0 +1,3 @@\n+one\n+two\n+three\n"
        hunks = parse_all_hunks(diff, hunk_per_line=True)

        assert len(hunks) == 3
        assert _replay([], [h.lines for h in hunks]) == ["one\n", "two\n", "three\n"]

    def test_later_hunks_in_file_are_offset(self) -> None:
        old = [f"l{i}\n" for i in range(12)]
        body = [" l0\n", "+a\n", "+b\n", *(f" l{i}\n" for i in range(1, 11)), "+c\n", "+d\n", " l11\n"]
        diff = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,12 +1,16 @@\n" + "".join(body)
        hunks = parse_all_hunks(diff, hunk_per_line=True)

        assert len(hunks) == 4
        new = ["l0\n", "a\n", "b\n", *(f"l{i}\n" for i in range(1, 11)), "c\n", "d\n", "l11\n"]
        assert _replay(old, [h.lines for h in hunks]) == new

    def test_deleted_file_stays_whole(self) -> None:
        diff = "diff --git a/n b/n\ndeleted file mode 100644\n--- a/n\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-one\n-two\n"
        assert len(parse_all_hunks(diff, hunk_per_line=True)) == 1


# ---------------------------------------------------------------------------
# hunk_sides / apply_hunk
# ---------------------------------------------------------------------------


class TestApplyHunk:
    def test_no_newline_marker_applies_to_previous_line(self) -> None:
        old, new = hunk_sides([" a\n", "-b\n", "\\ No newline at end of file\n", "+B\n"])
        assert old == ["a\n", "b"]
        assert new == ["a\n", "B\n"]

    def test_marker_after_context_applies_to_both_sides(self) -> None:
        old, new = hunk_sides(["-a\n", "+A\n", " z\n", "\\ No newline at end of file\n"])
        assert old == ["a\n", "z"]
        assert new == ["A\n", "z"]

    def test_rejects_mismatched_context(self) -> None:
        lines = ["a\n", "b\n", "c\n"]
        with pytest.raises(SliceError):
            apply_hunk(lines, ["@@ -1,2 +1,2 @@\n", " a\n", "-x\n", "+y\n"])

    def test_pure_insertion(self) -> None:
        lines = ["a\n", "b\n"]
        apply_hunk(lines, ["@@ -1,0 +2,1 @@\n", "+new\n"])
        assert lines == ["a\n", "new\n", "b\n"]


# ---------------------------------------------------------------------------
# _dry_run_remaining
# ---------------------------------------------------------------------------


class TestDryRunRemaining:
    def test_counts_and_labels_all_hunks(self) -> None:
        output = _dry_run_remaining(TWO_HUNK_DIFF)
        lines = [ln for ln in output.splitlines() if ln.strip()]
        assert len(lines) == 2
        assert "[1]" in lines[0]
        assert "[2]" in lines[1]
        assert "foo.py" in lines[0]
        assert "foo.py" in lines[1]

    def test_empty_diff(self) -> None:
        assert _dry_run_remaining("") == ""

    def test_two_file_diff_all_hunks_labeled(self) -> None:
        output = _dry_run_remaining(TWO_FILE_DIFF)
        lines = output.strip().splitlines()
        assert len(lines) == 2
        assert "a.py" in lines[0]
        assert "b.py" in lines[1]


# ---------------------------------------------------------------------------
# slice_hunks (integration — requires git_repo fixture)
# ---------------------------------------------------------------------------


class TestSliceHunks:
    def test_empty_staged_returns_zero(self, git_repo: Path) -> None:
        result = slice_hunks([])
        assert result == 0

    def _stage(self, git_repo: Path, *paths: str) -> None:
        """Stage one or more files."""
        git.add("--", *paths, _cwd=git_repo)

    def test_single_file_creates_one_commit(self, git_repo: Path, commit_test_file: Callable[[str, str], None]) -> None:
        commit_test_file("example.py", "x = 1\n")
        (git_repo / "example.py").write_text("x = 2\n")
        self._stage(git_repo, "example.py")
        result = slice_hunks([])
        assert result == 1
        log = str(git.log("--oneline"))
        assert "temp: example.py:" in log

    def test_temp_message_includes_diff_hash(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        commit_test_file("example.py", "x = 1\n")
        (git_repo / "example.py").write_text("x = 2\n")
        self._stage(git_repo, "example.py")
        slice_hunks([])
        log = str(git.log("--oneline"))
        assert re.search(r"temp: example\.py:L\d+-\d+ #[0-9a-f]{8}-\d+", log)

    def test_default_splits_at_single_context_line(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        # Two edits with one unchanged line between them, like git add -p 's'.
        commit_test_file("near.py", "a = 1\nb = 2\nc = 3\n")
        (git_repo / "near.py").write_text("a = 10\nb = 2\nc = 30\n")
        self._stage(git_repo, "near.py")
        base = str(git("rev-parse", "HEAD")).strip()

        slice_command(paths=[], dry_run=False, all_changes=False, from_commit=None)

        assert str(git("rev-list", "--count", f"{base}..HEAD")).strip() == "2"

    def test_commit_count_suffix_makes_messages_unique(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        # Two hunks far apart in one file, sliced in a single call.
        # Even if hashes collided, the -N suffix would keep messages distinct.
        content = "\n".join(f"line{i} = {i}" for i in range(1, 31)) + "\n"
        f = git_repo / "multi.py"
        commit_test_file("multi.py", content)
        lines = content.splitlines()
        lines[0] = "line1 = 999"
        lines[27] = "line28 = 999"
        f.write_text("\n".join(lines) + "\n")
        self._stage(git_repo, "multi.py")
        assert slice_hunks([]) == 2

        subjects = [line for line in str(git.log("--format=%s")).splitlines() if line.startswith("temp:")]
        assert len(subjects) == 2
        for s in subjects:
            assert re.search(r"temp: multi\.py:L\d+-\d+ #[0-9a-f]{8}-\d+", s)
        # suffixes must be unique (1 and 2)
        suffixes = [m.group(1) for s in subjects if (m := re.search(r"-(\d+)$", s))]
        assert len(set(suffixes)) == 2

    def test_two_separate_hunks_create_two_commits(self, git_repo: Path) -> None:
        # Commit a file with 30 lines, then modify lines 1 and 28 (far apart)
        content = "\n".join(f"line{i} = {i}" for i in range(1, 31)) + "\n"
        f = git_repo / "multi.py"
        f.write_text(content)
        git.add(".", _cwd=git_repo)
        git.commit("--no-verify", "-m", "add multi", _cwd=git_repo)
        lines = content.splitlines()
        lines[0] = "line1 = 999"
        lines[27] = "line28 = 999"
        f.write_text("\n".join(lines) + "\n")
        self._stage(git_repo, "multi.py")
        result = slice_hunks([])
        assert result == 2

    def test_binary_file_does_not_block_slicing(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        # Commit a binary file alongside a text file
        binary_path = git_repo / "data.bin"
        binary_path.write_bytes(bytes(range(256)))
        git.add("data.bin", _cwd=git_repo)
        git.commit("--no-verify", "-m", "add binary", _cwd=git_repo)
        commit_test_file("code.py", "x = 1\n")

        # Modify both — binary change sorts first alphabetically
        binary_path.write_bytes(bytes(reversed(range(256))))
        (git_repo / "code.py").write_text("x = 2\n")
        # Stage both; binary will be skipped, text hunk committed
        git.add(".", _cwd=git_repo)

        result = slice_hunks([])
        # The text hunk should be committed; binary stays staged
        assert result == 1
        log = str(git.log("--oneline"))
        assert "temp: code.py:" in log

    def test_path_filter_limits_slicing(self, git_repo: Path, commit_test_file: Callable[[str, str], None]) -> None:
        commit_test_file("a.py", "a = 1\n")
        commit_test_file("b.py", "b = 1\n")
        (git_repo / "a.py").write_text("a = 2\n")
        (git_repo / "b.py").write_text("b = 2\n")
        # Stage both files
        git.add(".", _cwd=git_repo)
        result = slice_hunks(["a.py"])
        assert result == 1
        log = str(git.log("--oneline"))
        assert "temp: a.py:" in log
        # b.py should still be staged (real index untouched)
        staged = str(git.diff("--cached", "--name-only"))
        assert "b.py" in staged

    def test_staged_new_file_creates_commit(self, git_repo: Path) -> None:
        # Stage a brand-new file (never committed before)
        (git_repo / "new.py").write_text("x = 1\ny = 2\n")
        self._stage(git_repo, "new.py")
        result = slice_hunks([])
        assert result == 1
        log = str(git.log("--oneline"))
        assert "temp: new.py:" in log
        # New file should now be committed into HEAD
        show = str(git.show("HEAD:new.py"))
        assert "x = 1" in show

    def test_staged_deleted_file_creates_commit(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        commit_test_file("gone.py", "x = 1\n")
        git.rm("gone.py", _cwd=git_repo)
        result = slice_hunks([])
        assert result == 1
        log = str(git.log("--oneline"))
        assert "temp: gone.py:" in log
        # File should be absent from HEAD
        ls = str(git("ls-files", "gone.py"))
        assert ls.strip() == ""


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("".join(lines))


def _stage_mixed_changes(repo: Path) -> None:
    """Commit a varied set of files, then stage changes that exercise every hunk shape (18 hunks)."""
    _write_lines(repo / "multi.txt", [f"line {n}\n" for n in range(120)])
    (repo / "noeol.txt").write_text("a\nb\nc\nd\ne\nf\ng\nh\ni\nj\nk\nl")
    (repo / "crlf.txt").write_bytes(b"".join(b"crlf %d\r\n" % n for n in range(60)))
    _write_lines(repo / "ren_src.txt", [f"rename me {n}\n" for n in range(80)])
    _write_lines(repo / "mode.sh", [f"echo {n}\n" for n in range(60)])
    (repo / "gone.txt").write_text("bye\nbye\n")
    _write_lines(repo / "weird name.txt", [f"w {n}\n" for n in range(60)])
    (repo / "latin1.txt").write_bytes(b"caf\xe9\n" * 3)
    git.add("-A")
    git.commit("--no-verify", "-m", "base")

    # Edits within one file: a change, a removal, an insertion, and two
    # changes close enough to share a hunk.
    multi = [f"line {n}\n" for n in range(120)]
    multi[2] = "changed 2\n"
    multi[30:31] = []
    multi.insert(60, "inserted 60\n")
    multi[90] = "near-a\n"
    multi[95] = "near-b\n"  # 4 context lines apart: one hunk, split in two
    _write_lines(repo / "multi.txt", multi)

    # Line-ending edge cases: no newline at end of file, and CRLF.
    (repo / "noeol.txt").write_text("A\nb\nc\nd\ne\nf\ng\nh\ni\nj\nk\nL")
    crlf = (repo / "crlf.txt").read_bytes()
    (repo / "crlf.txt").write_bytes(crlf.replace(b"crlf 3\r\n", b"CRLF 3\r\n").replace(b"crlf 50\r\n", b"CRLF 50\r\n"))

    # File-level changes: rename with edits, mode change with edits, delete, create.
    renamed = [f"rename me {n}\n" for n in range(80)]
    renamed[2] = "edited 2\n"
    renamed[70] = "edited 70\n"
    (repo / "ren_src.txt").unlink()
    _write_lines(repo / "ren_dst.txt", renamed)
    mode = [f"echo {n}\n" for n in range(60)]
    mode[1] = "echo one\n"
    mode[50] = "echo fifty\n"
    _write_lines(repo / "mode.sh", mode)
    os.chmod(repo / "mode.sh", 0o755)
    (repo / "gone.txt").unlink()
    (repo / "new.txt").write_text("brand\nnew\n")

    # A path with a space, and content that isn't UTF-8.
    weird = [f"w {n}\n" for n in range(60)]
    weird[0] = "W 0\n"
    weird[40] = "W 40\n"
    _write_lines(repo / "weird name.txt", weird)
    (repo / "latin1.txt").write_bytes(b"caf\xe9\nCAF\xc9\ncaf\xe9\n")
    git.add("-A")


class TestSliceMixedChanges:
    def test_reproduces_index(self, git_repo: Path) -> None:
        _stage_mixed_changes(git_repo)
        expected_tree = str(git("write-tree")).strip()

        n = slice_hunks([])

        assert n == 18
        assert str(git("rev-parse", "HEAD^{tree}")).strip() == expected_tree
        assert str(git.diff("--cached", "--name-only")).strip() == ""
        authors = set(str(git.log("--format=%an <%ae>", f"-{n}")).splitlines())
        assert authors == {"Git Curate <git-curate@local>"}

    def test_each_commit_applies_one_hunk(self, git_repo: Path) -> None:
        _stage_mixed_changes(git_repo)
        n = slice_hunks([])
        for i in range(n):
            diff: bytes = git.diff(f"HEAD~{i + 1}", f"HEAD~{i}", "-U3", _return_cmd=True).stdout
            assert diff.count(b"\n@@ ") == 1

    def test_path_filter(self, git_repo: Path) -> None:
        _stage_mixed_changes(git_repo)
        assert slice_hunks(["multi.txt", "mode.sh"]) == 7
        staged = set(str(git.diff("--cached", "--name-only")).split())
        assert "multi.txt" not in staged and "mode.sh" not in staged
        assert "noeol.txt" in staged


class TestSliceErrors:
    def _assert_untouched(self, head: str) -> None:
        assert str(git("rev-parse", "HEAD")).strip() == head
        assert str(git("for-each-ref", "refs/git-curate/")).strip() == ""

    def test_submodule_is_rejected(self, git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (git_repo / "a.txt").write_text("a\n")
        git.add("a.txt")
        head = str(git("rev-parse", "HEAD")).strip()
        git("update-index", "--add", "--cacheinfo", f"160000,{head},sub")

        with pytest.raises(SliceError):
            slice_hunks([])

        self._assert_untouched(head)
        assert "error: cannot slice: sub: unsupported mode 160000" in capsys.readouterr().err

    def test_result_differing_from_index_is_rejected(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _stage_mixed_changes(git_repo)
        head = str(git("rev-parse", "HEAD")).strip()
        real_ls_index = slice_mod._ls_index

        def tampered_index() -> dict[str, tuple[str, str]]:
            index = real_ls_index()
            index["multi.txt"] = ("100644", "0" * 40)
            return index

        monkeypatch.setattr(slice_mod, "_ls_index", tampered_index)
        with pytest.raises(SliceError, match="multi.txt: result differs from the index"):
            slice_hunks([])
        self._assert_untouched(head)

    def test_hunk_not_matching_head_is_rejected(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _stage_mixed_changes(git_repo)
        head = str(git("rev-parse", "HEAD")).strip()
        hunks = slice_mod.parse_all_hunks(slice_mod._staged_diff([]))
        # A later hunk (mode.sh's second) removes a line HEAD doesn't have.
        hunks[5].lines = [line.replace("-echo 50\n", "-echo 5000\n") for line in hunks[5].lines]
        monkeypatch.setattr(slice_mod, "parse_all_hunks", lambda *args, **kwargs: hunks)

        with pytest.raises(SliceError, match="does not match"):
            slice_hunks([])
        self._assert_untouched(head)

    def test_fast_import_failure_is_reported(self, git_repo: Path) -> None:
        with pytest.raises(SliceError, match="git fast-import failed: .*[Uu]nsupported command: bogus"):
            slice_mod._run_fast_import(iter([b"bogus\n"]))

    def test_stream_error_ends_fast_import_without_done(self, git_repo: Path) -> None:
        def stream() -> Iterator[bytes]:
            yield b"commit refs/git-curate/test\n"
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            slice_mod._run_fast_import(stream())
        assert str(git("for-each-ref", "refs/git-curate/")).strip() == ""


# ---------------------------------------------------------------------------
# slice_command (integration — requires git_repo fixture)
# ---------------------------------------------------------------------------


class TestSliceCommand:
    def test_dry_run_does_not_commit(self, git_repo: Path, commit_test_file: Callable[[str, str], None]) -> None:
        commit_test_file("example.py", "x = 1\n")
        (git_repo / "example.py").write_text("x = 2\n")
        git.add("example.py", _cwd=git_repo)
        slice_command(
            paths=[],
            dry_run=True,
            all_changes=False,
            split_context=4,
            from_commit=None,
        )
        log = str(git.log("--oneline", _cwd=git_repo))
        assert "temp:" not in log

    def test_no_split_on_blank_lines_commits_new_file_whole(self, git_repo: Path) -> None:
        (git_repo / "n.py").write_text("def f():\n    pass\n\n\ndef g():\n    pass\n")
        git.add("n.py", _cwd=git_repo)
        base = str(git("rev-parse", "HEAD", _cwd=git_repo)).strip()

        slice_command(
            paths=[], dry_run=False, all_changes=False, split_context=4, split_on_blank_lines=False, from_commit=None
        )

        assert str(git("rev-list", "--count", f"{base}..HEAD", _cwd=git_repo)).strip() == "1"

    def test_hunk_per_line_commits_each_line(self, git_repo: Path) -> None:
        (git_repo / "n.py").write_text("a = 1\nb = 2\nc = 3\n")
        git.add("n.py", _cwd=git_repo)
        base = str(git("rev-parse", "HEAD", _cwd=git_repo)).strip()

        result = CliRunner().invoke(app, ["slice", "--hunk-per-line"])

        assert result.exit_code == 0, result.output
        assert str(git("rev-list", "--count", f"{base}..HEAD", _cwd=git_repo)).strip() == "3"

    def test_next_step_names_the_commands(self, git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (git_repo / "n.py").write_text("x = 1\n")
        git.add("n.py", _cwd=git_repo)

        slice_command(paths=[], dry_run=False, all_changes=False, split_context=4, from_commit=None)

        out = capsys.readouterr().out
        assert "git-curate diff --tmp" in out
        assert "git-curate group --spec" in out

    def test_partial_staging_allowed(self, git_repo: Path, commit_test_file: Callable[[str, str], None]) -> None:
        # File with both staged and unstaged changes — the slicer should commit
        # only the staged portion and leave the working tree untouched.
        commit_test_file("f.py", "a = 1\nb = 2\n")
        # Stage one change …
        (git_repo / "f.py").write_text("a = 99\nb = 2\n")
        git.add("f.py", _cwd=git_repo)
        # … then make an additional unstaged change on top
        (git_repo / "f.py").write_text("a = 99\nb = 99\n")

        slice_command(
            paths=[],
            dry_run=False,
            all_changes=False,
            split_context=4,
            from_commit=None,
        )

        # The staged hunk (a = 99) should be in HEAD
        show = str(git("show", "HEAD:f.py", _cwd=git_repo))
        assert "a = 99" in show
        # The unstaged hunk (b = 99) must still only be in the working tree
        assert "b = 2" in show  # HEAD still has b = 2
        working = (git_repo / "f.py").read_text()
        assert "b = 99" in working

    def test_no_staged_no_all_flag_exits_nonzero(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        # Nothing staged, no --all — should exit with an error.
        commit_test_file("f.py", "a = 1\n")
        (git_repo / "f.py").write_text("a = 99\n")  # unstaged only

        with pytest.raises(SystemExit) as exc_info:
            slice_command(
                paths=[],
                dry_run=False,
                all_changes=False,
                split_context=4,
                from_commit=None,
            )
        assert exc_info.value.code == 1

    def test_all_flag_stages_and_slices_unstaged_changes(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        # --all with only unstaged changes should stage everything and create temp commits.
        commit_test_file("f.py", "a = 1\n")
        (git_repo / "f.py").write_text("a = 99\n")  # unstaged

        slice_command(
            paths=[],
            dry_run=False,
            all_changes=True,
            split_context=4,
            from_commit=None,
        )

        log = str(git.log("--oneline", _cwd=git_repo))
        assert "temp: f.py:" in log

    def test_all_flag_does_not_stage_untracked_files(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        # --all must not stage untracked files; only tracked modifications should be sliced.
        commit_test_file("tracked.py", "a = 1\n")
        (git_repo / "tracked.py").write_text("a = 99\n")  # unstaged modification
        (git_repo / "untracked.py").write_text("new file\n")  # never committed

        slice_command(
            paths=[],
            dry_run=False,
            all_changes=True,
            split_context=4,
            from_commit=None,
        )

        # tracked.py change must be committed
        log = str(git.log("--oneline", _cwd=git_repo))
        assert "temp: tracked.py:" in log
        # untracked.py must remain untracked
        untracked = str(git("ls-files", "--others", "--exclude-standard", _cwd=git_repo)).strip()
        assert "untracked.py" in untracked

    def test_all_flag_with_paths_stages_only_those_paths(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        # --all with explicit paths should only stage (and slice) those paths.
        commit_test_file("a.py", "a = 1\n")
        commit_test_file("b.py", "b = 1\n")
        (git_repo / "a.py").write_text("a = 99\n")
        (git_repo / "b.py").write_text("b = 99\n")

        slice_command(
            paths=["a.py"],
            dry_run=False,
            all_changes=True,
            split_context=4,
            from_commit=None,
        )

        log = str(git.log("--oneline", _cwd=git_repo))
        assert "temp: a.py:" in log
        # b.py must remain unstaged in the working tree
        unstaged = str(git.diff("--name-only", _cwd=git_repo))
        assert "b.py" in unstaged

    def test_rebase_in_progress_exits_nonzero(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        commit_test_file("f.py", "a = 1\n")
        (git_repo / "f.py").write_text("a = 99\n")
        git.add("f.py", _cwd=git_repo)

        # Simulate a rebase in progress by creating the state directory git checks for.
        (git_repo / ".git" / "rebase-merge").mkdir()

        with pytest.raises(RebaseInProgressError):
            slice_command(
                paths=[],
                dry_run=False,
                all_changes=False,
                split_context=4,
                from_commit=None,
            )

    def test_from_option_resets_to_parent_and_slices(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        # Create two commits on top of init; --from will squash them back into staging.
        commit_test_file("a.py", "a = 1\n")
        commit_test_file("b.py", "b = 1\n")

        log = str(git.log("--reverse", "--format=%H", "HEAD~2..HEAD", _cwd=git_repo)).strip().splitlines()
        first_sha = log[0]
        parent_sha = str(git("rev-parse", f"{first_sha}^", _cwd=git_repo)).strip()

        # No explicit staging needed: --from squashes first_sha..HEAD into the index.
        slice_command(
            paths=[],
            dry_run=False,
            all_changes=False,
            split_context=4,
            from_commit=first_sha,
        )

        from git_curate.common import find_slice_base

        # find_slice_base() must return the parent of the from commit
        assert find_slice_base() == parent_sha
        # Every commit above parent_sha must be curate-authored
        author_emails = str(git.log("--format=%ae", f"{parent_sha}..HEAD", _cwd=git_repo)).strip()
        for email in author_emails.splitlines():
            assert email.strip() == "git-curate@local"
