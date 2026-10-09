"""
Step 1: Hunk-level commit decomposition
========================================

Problem
-------
AI agents can stage whole files (git add) but cannot drive git add -p.
They produce large file-level commits where you want many small, logical ones.

Three-step approach
-------------------
This script is Step 1 of three:

  Step 1 (this slice tool, mechanical):
    Decomposes all staged changes into one atomic commit per diff hunk.

  Step 2 (AI agent):
    Reads the N temp commits and decides which hunks belong together
    (e.g. "commits 1-5 → feat: auth, 6-12 → refactor: schema,
    13-15 → test: auth"). Outputs a grouping spec (JSON).

  Step 3 (group tool, mechanical):
    Executes the rebase plan non-interactively via GIT_SEQUENCE_EDITOR,
    collapsing the temp commits into clean final commits.

Algorithm (Step 1)
------------------
  1. Run `git diff --cached -U3` on the index (or specified files).
  2. Parse every hunk, splitting hunks like `git add -p` 's', then (unless
     --no-split-on-blank-lines) at blank lines between sibling blocks of
     added or removed lines. New files skip the blank-line split unless
     --split-new-files. Each hunk's old-side offset accounts for the
     earlier hunks in its file, so hunk k
     applies on top of hunks 1..k-1. Any rename or mode change rides along
     with a file's first hunk.
  3. Load HEAD's version of every touched file, apply the hunks to them in
     Python, and stream one commit per hunk through a single
     `git fast-import` (no hooks). Temp commits carry the
     Git Curate <git-curate@local> author so group and abort can detect them.
  4. Check that every touched path in the last temp commit matches the
     real index, then advance HEAD to it with update-ref.

Replaying them in order reconstructs the original staged state.

Key properties
--------------
- Non-destructive: the real staged index stays untouched, and HEAD only
  moves once every temp commit exists and matches the index.
- Fast: one diff and a handful of git processes, however many hunks.
- Conflict-free: only repackages state that already exists.
- Works for new files, deletions, and modified files alike.
- Pager-safe: uses --no-pager and color.ui=false to avoid delta/less
  and ANSI escape codes corrupting the diff output.

Usage
-----
    # Slice all staged changes:
    uvx git-curate slice

    # Slice only specific files:
    uvx git-curate slice src/auth.py src/schema.py

    # Dry-run — list the staged hunks (before splitting) without committing:
    uvx git-curate slice --dry-run

    # Stage all unstaged changes then slice:
    uvx git-curate slice --all

    # Keep blocks of new code separated only by blank lines together:
    uvx git-curate slice --no-split-on-blank-lines src/auth.py

    # One temp commit per changed line, e.g. for an import block:
    uvx git-curate slice --hunk-per-line src/auth.py

    # Rewrite history from an earlier commit (inclusive):
    uvx git-curate slice --from abc1234

    # Slice again after making more changes (iterative workflow):
    uvx git-curate slice
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import re
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

import sh
import typer

from .common import SHA_DISPLAY_LEN, Exit, SliceError, SubApp, curate_git, git, resolve_rewrite_from

app = SubApp()

# Context lines in the diff that slicing parses (`git diff -U<n>`).
DIFF_CONTEXT = 3

# Default --split-context: split at any unchanged line between changes, like git add -p 's'.
SPLIT_CONTEXT = 1


@dataclass
class FileDiff:
    """The per-file part of a diff: paths, modes, and the extended header lines."""

    header_lines: list[str]
    old_path: str | None  # None for a new file
    new_path: str | None  # None for a deleted file
    new_mode: str | None = None  # set when the diff creates the file or changes its mode

    @property
    def is_new(self) -> bool:
        return self.old_path is None

    @property
    def is_deleted(self) -> bool:
        return self.new_path is None


@dataclass
class Hunk:
    """One temp commit's change, positioned to apply on top of the earlier hunks in its file."""

    file_path: str
    line_desc: str  # the hunk's line range in the staged file, e.g. "L10-14"
    file: FileDiff
    # The @@ header (offset-adjusted) followed by the hunk body.
    lines: list[str]
    first_in_file: bool  # this hunk's commit also creates, renames or re-modes the file


# ---------------------------------------------------------------------------
# Patch parsing
# ---------------------------------------------------------------------------

# Matches the start of a per-file diff block: "diff --git a/foo b/foo"
FILE_HEADER = re.compile(r"^diff --git ", re.MULTILINE)

# Matches a hunk header: "@@ -start,count +start,count @@ optional context"
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def split_lines(text: str) -> list[str]:
    """Split on "\\n" only, keeping line endings.

    Unlike str.splitlines, this leaves "\\r", form feeds and other Unicode line
    breaks inside a line, as git does.
    """
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def parse_hunk_header(header: str) -> tuple[int, int, int, int, str]:
    """Return (old_start, old_count, new_start, new_count, label) from an @@ line."""
    match = HUNK_HEADER.match(header.rstrip("\n"))
    assert match is not None
    old_count = int(match.group(2)) if match.group(2) is not None else 1
    new_count = int(match.group(4)) if match.group(4) is not None else 1
    return int(match.group(1)), old_count, int(match.group(3)), new_count, match.group(5) or ""


def _is_diff_context(line: str) -> bool:
    """Return True if line is a context line (space-prefixed or bare blank) or a "\\" marker.

    Git normally prefixes context lines with a space, but diff.suppressBlankEmpty
    emits bare newlines instead. Treat both as context to keep gap-counting correct.
    Callers that count lines must skip "\\ No newline at end of file" markers first.
    """
    return line.startswith((" ", "\\")) or line in ("\n", "\r\n")


def _split_hunk(hunk_lines: list[str], min_context: int) -> list[list[str]] | None:
    """Try to split a single hunk into smaller sub-hunks.

    Replicates git add -p 's' (split): finds a run of context-only lines
    inside the hunk and cuts there, producing two or more independent sub-hunks.

    min_context: minimum context-line run between change regions needed to split.

    Returns sub-hunk bodies (without @@ headers), or None if the hunk can't split.
    """
    # The first line is the @@ header — work with the body only
    body = hunk_lines[1:]

    if not body:
        return None

    # Find split points: interior positions where we transition from a
    # changed line (+/-) to a context line (" ") and there's a run of
    # min_context or more context lines, followed by more changes.
    # We split at the beginning of each such context run.
    regions: list[list[str]] = []
    current: list[str] = []
    i = 0

    # Walk the body a whole context run or a single changed line at a time.
    while i < len(body):
        line = body[i]
        if _is_diff_context(line):
            # Accumulate context lines and look ahead for more changes
            ctx_start = i
            while i < len(body) and _is_diff_context(body[i]):
                i += 1
            ctx_run = body[ctx_start:i]

            # Only a run with changes on both sides is interior.
            has_changes_before = any(line.startswith(("+", "-")) for line in current)
            has_changes_after = i < len(body)

            # A long enough interior run is a split point: it ends the current
            # region as trailing context and starts the next as leading context.
            if has_changes_before and has_changes_after and len(ctx_run) >= min_context:
                current.extend(ctx_run)
                regions.append(current)
                current = list(ctx_run)
            else:
                current.extend(ctx_run)
        else:
            current.append(line)
            i += 1
            # A "\ No newline at end of file" marker belongs to the changed line
            # before it, not to a context run that could split an edited last line.
            while i < len(body) and body[i].startswith("\\"):
                current.append(body[i])
                i += 1

    # The last region has no split point after it to close it.
    if current:
        regions.append(current)

    # No interior run was long enough.
    if len(regions) <= 1:
        return None

    return regions


def _make_hunk_header(old_start: int, old_count: int, new_start: int, new_count: int, label: str = "") -> str:
    """Format a @@ hunk header line."""
    return f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{label}\n"


def _rebuild_sub_hunk_headers(original_header: str, sub_hunks: list[list[str]]) -> list[list[str]]:
    """Recompute @@ headers for each sub-hunk after a split.

    Assigns correct line numbers and counts so each sub-hunk applies on its own.
    Positions are against the original (unpatched) file. Consecutive sub-hunks
    from _split_hunk share a context run (the trailing context of one is the
    leading context of the next), so that run is not counted twice.
    """
    old_pos, _, new_pos, _, label = parse_hunk_header(original_header)

    result: list[list[str]] = []
    for i, body_lines in enumerate(sub_hunks):
        # Header the sub-hunk at the current position on each side.
        old_count, new_count = _count_sides(body_lines)
        header = _make_hunk_header(old_pos, old_count, new_pos, new_count, label)
        result.append([header] + body_lines)

        # Advance past this sub-hunk, then step back over the context run the
        # next sub-hunk starts with.
        old_pos += old_count
        new_pos += new_count
        if i + 1 < len(sub_hunks):
            shared = _trailing_context_count(body_lines)
            old_pos -= shared
            new_pos -= shared

    return result


def _count_sides(body_lines: list[str]) -> tuple[int, int]:
    """Return (old_count, new_count) for a hunk body.

    "\\ No newline at end of file" markers annotate the previous line and don't count.
    """
    old_count = new_count = 0
    for line in body_lines:
        # Checked first, since _is_diff_context would count a marker as context.
        if line.startswith("\\"):
            continue
        # A context line is on both sides.
        if _is_diff_context(line):
            old_count += 1
            new_count += 1

        # A removed line is only on the old side, an added line only on the new.
        elif line.startswith("-"):
            old_count += 1
        elif line.startswith("+"):
            new_count += 1
    return old_count, new_count


def _trailing_context_count(body_lines: list[str]) -> int:
    """Return how many context lines end a hunk body."""
    count = 0
    # Walk back to the last changed line. A marker stops the walk too; only a
    # file's last sub-hunk can end in one, and its count is never used.
    for line in reversed(body_lines):
        if not _is_diff_context(line) or line.startswith("\\"):
            break
        count += 1
    return count


def _strip_path(line: str, prefix: str) -> str:
    # Git appends a tab to ---/+++ paths that contain spaces (for GNU patch).
    return line[len(prefix) :].rstrip("\n").removesuffix("\t")


def _parse_file_header(header_lines: list[str]) -> FileDiff | None:
    """Parse a file block's extended header. Returns None if it has no usable paths.

    Quoted paths (git quotes names with control characters, quotes or
    backslashes) are not handled, so those files stay staged.
    """
    old_path: str | None = None
    new_path: str | None = None
    new_mode: str | None = None
    saw_old = saw_new = False
    for line in header_lines:
        # The old side: a/<path>, /dev/null for a new file, or a quoted path.
        if line.startswith("--- "):
            saw_old = True
            if line.startswith("--- a/"):
                old_path = _strip_path(line, "--- a/")

        # The new side: b/<path>, /dev/null for a deleted file, or a quoted path.
        elif line.startswith("+++ "):
            saw_new = True
            if line.startswith("+++ b/"):
                new_path = _strip_path(line, "+++ b/")

        # The mode of a created file, or the new mode of a mode change.
        elif line.startswith(("new file mode ", "new mode ")):
            new_mode = line.rstrip("\n").rsplit(" ", 1)[1]

    # A side without an a/ or b/ path must be /dev/null; anything else is a
    # quoted path.
    if not (saw_old and saw_new) or (old_path is None and new_path is None):
        return None
    if old_path is None and not any(line.startswith("--- /dev/null") for line in header_lines):
        return None
    if new_path is None and not any(line.startswith("+++ /dev/null") for line in header_lines):
        return None
    return FileDiff(header_lines=header_lines, old_path=old_path, new_path=new_path, new_mode=new_mode)


def _sub_hunks(hunk_lines: list[str], min_context: int) -> list[list[str]]:
    """Split one hunk like `git add -p` 's', returning each sub-hunk with its @@ header.

    Positions are against the original file. Sub-hunks after the first have
    their leading context trimmed to DIFF_CONTEXT lines, like a hunk git
    itself would emit.
    """
    # A min_context of 0 turns splitting off (--split-context 0).
    sub_hunks = _split_hunk(hunk_lines, min_context) if min_context > 0 else None
    if sub_hunks is None:
        return [hunk_lines]

    result = []
    # Header each sub-hunk, then trim the context run it shares with the one before.
    for i, sub_hunk in enumerate(_rebuild_sub_hunk_headers(hunk_lines[0], sub_hunks)):
        header, body = sub_hunk[0], sub_hunk[1:]
        if i > 0:
            # The shared context run can be longer than git would put before a hunk.
            leading = 0
            while leading < len(body) and _is_diff_context(body[leading]):
                leading += 1

            # Keep its last DIFF_CONTEXT lines, and move the header down past the rest.
            trim = max(0, leading - DIFF_CONTEXT)
            if trim:
                old_start, old_count, new_start, new_count, label = parse_hunk_header(header)
                header = _make_hunk_header(
                    old_start + trim, old_count - trim, new_start + trim, new_count - trim, label
                )
                body = body[trim:]
        result.append([header] + body)
    return result


def _is_blank_change(line: str) -> bool:
    return line.startswith(("+", "-")) and not line[1:].strip()


def _indent(line: str) -> int:
    """Return the width of a diff line's leading whitespace, counting a tab as one."""
    text = line[1:]
    return len(text) - len(text.lstrip(" \t"))


def _change_regions(body: list[str]) -> Iterator[tuple[int, int]]:
    """Yield (start, end) for each run of changed lines in a hunk body.

    A "\\ No newline at end of file" marker stays in the run it follows.
    """
    i = 0
    while i < len(body):
        # Skip context lines, and any marker after one.
        if not body[i].startswith(("+", "-")):
            i += 1
            continue

        # Extend the run up to the next context line.
        start = i
        while i < len(body) and body[i].startswith(("+", "-", "\\")):
            i += 1
        yield start, i


def _blank_line_cuts(body: list[str]) -> list[int]:
    """Return body indices where a run of changes can be cut at a blank line.

    Within a region of only additions or only removals, cut after a run of
    blank or whitespace-only lines if the next non-blank line is indented no
    deeper than the region's first non-blank line. That separates sibling
    blocks (functions, tests, paragraphs) without cutting inside a block's
    body, and without knowing anything about the language.

    Without a cut there, two blocks added in one run share a hunk, and so a
    temp commit, even when they belong in different final commits.
    """
    cuts: list[int] = []
    for start, end in _change_regions(body):
        # A replacement stays whole: which removed lines pair with which
        # added ones isn't clear enough to split.
        changes = [(j, body[j]) for j in range(start, end) if not body[j].startswith("\\")]
        if len({line[0] for _, line in changes}) != 1:
            continue

        # Indentation is compared against the region's first block.
        ref_indent: int | None = None
        after_blank = False
        for j, line in changes:
            if _is_blank_change(line):
                # Leading blanks don't count: there's no block before them to cut off.
                after_blank = ref_indent is not None
                continue

            # The first non-blank line sets the reference; a later one right
            # after blanks, indented no deeper, starts a sibling block.
            if ref_indent is None:
                ref_indent = _indent(line)
            elif after_blank and _indent(line) <= ref_indent:
                cuts.append(j)
            after_blank = False
    return cuts


def _as_context(lines: list[str], drop: str) -> list[str]:
    """Rewrite one side of a hunk body as context lines.

    Lines starting with *drop* are left out, along with any marker after them.
    """
    out: list[str] = []
    kept = False
    for line in lines:
        # A marker belongs to the line before it, so it goes wherever that line went.
        if line.startswith("\\"):
            if kept:
                out.append(line)
            continue

        # Keep context and the other side's changes, all as context lines.
        kept = not line.startswith(drop)
        if kept:
            out.append(line if _is_diff_context(line) else " " + line[1:])
    return out


def _context_slice(lines: list[str], from_end: bool) -> list[str]:
    """Take up to DIFF_CONTEXT lines from one end of *lines*, keeping a marker with its line."""
    positions = [i for i, line in enumerate(lines) if not line.startswith("\\")]
    if not positions:
        return []

    # Taken from the end, the slice runs to the end of lines, so markers come along.
    n = min(DIFF_CONTEXT, len(positions))
    if from_end:
        return lines[positions[-n] :]

    # Taken from the start, a marker after the last line taken still belongs to it.
    end = positions[n - 1] + 1
    if end < len(lines) and lines[end].startswith("\\"):
        end += 1
    return lines[:end]


def _line_cuts(body: list[str]) -> tuple[list[str], list[int]]:
    """Return a hunk body and the indices where it can be cut into one piece per changed line.

    In a region with both removals and additions, the k-th removed line is
    paired with the k-th added one, and the body is reordered to interleave
    them so each pair can be cut out. That suits a run of edited lines, like
    an import block. Unpaired lines get pieces of their own. A region with a
    "\\ No newline at end of file" marker is only reordered if it has a
    single kind of change, so the marker stays after its line.

    A blank or whitespace-only line stays in the piece before it.
    """
    out: list[str] = []
    cuts: list[int] = []
    prev = 0

    # Rebuild the body region by region, copying the context between regions as is.
    for start, end in _change_regions(body):
        out.extend(body[prev:start])
        prev = end
        region = body[start:end]
        removed = [line for line in region if line.startswith("-")]
        added = [line for line in region if line.startswith("+")]

        # Break the region into units, the changed lines of one piece each.
        if removed and added and any(line.startswith("\\") for line in region):
            # Interleaving would move the marker away from its line.
            units = [region]
        elif removed and added:
            # Pair removals with additions in order; the longer side's extra lines stand alone.
            units = [[r, a] for r, a in zip(removed, added, strict=False)]
            n = min(len(removed), len(added))
            units += [[line] for line in removed[n:] + added[n:]]
        else:
            # A marker stays in the unit of the line it follows.
            units = []
            for line in region:
                if line.startswith("\\"):
                    units[-1].append(line)
                else:
                    units.append([line])

        # Cut before each unit, unless it starts with a blank line.
        for unit in units:
            if out and not _is_blank_change(unit[0]):
                cuts.append(len(out))
            out.extend(unit)
    out.extend(body[prev:])

    # The first non-blank change has no earlier piece to cut it from, so
    # leading blank lines join it.
    first = next((i for i, line in enumerate(out) if line.startswith(("+", "-")) and not _is_blank_change(line)), None)
    return out, [cut for cut in cuts if first is not None and cut > first]


def _split_at_blank_lines(hunk_lines: list[str]) -> list[list[str]]:
    """Cut one hunk at the blank lines _blank_line_cuts finds; return each piece with its @@ header."""
    return _split_at(hunk_lines[0], hunk_lines[1:], _blank_line_cuts(hunk_lines[1:]))


def _split_at_lines(hunk_lines: list[str]) -> list[list[str]]:
    """Cut one hunk into a piece per changed line, as _line_cuts finds; return each piece with its @@ header."""
    body, cuts = _line_cuts(hunk_lines[1:])
    return _split_at(hunk_lines[0], body, cuts)


def _split_at(header: str, body: list[str], cuts: list[int]) -> list[list[str]]:
    """Cut a hunk body at *cuts*; return each piece with its @@ header.

    The pieces apply in order, each on top of the previous ones, and are
    positioned for that, not against the original file. Adjacent pieces
    share context lines, so deps.compute_dependencies requires them to stay
    in order during group. A hunk with no cuts comes back unchanged.
    """
    if not cuts:
        return [[header, *body]]

    old_start, old_count, new_start, _, label = parse_hunk_header(header)
    # An empty old side (a new file) means "insert after line old_start". Later
    # pieces always start with context, so they count from the line after.
    later_old_start = old_start if old_count else old_start + 1

    pieces: list[list[str]] = []
    bounds = [0, *cuts, len(body)]
    for lo, hi in itertools.pairwise(bounds):
        # Context is the file as this piece finds it: earlier pieces applied
        # (their new side), later ones not yet (their old side).
        lead = _context_slice(_as_context(body[:lo], drop="-"), from_end=True)
        trail = _context_slice(_as_context(body[hi:], drop="+"), from_end=False)
        piece = lead + body[lo:hi] + trail

        # Position the piece after the new side of the earlier pieces.
        shift = _count_sides(body[:lo])[1] - _count_sides(lead)[0]
        piece_old_start = later_old_start + shift if lo else old_start
        piece_old, piece_new = _count_sides(piece)
        pieces.append([_make_hunk_header(piece_old_start, piece_old, new_start + shift, piece_new, label)] + piece)
    return pieces


def _parse_file_block(
    file_block: str,
    min_context: int,
    hunk_per_line: bool = False,
    split_on_blank_lines: bool = True,
    split_new_files: bool = False,
) -> list[Hunk]:
    """Turn one file's diff into hunks that apply in order, each on top of the previous ones."""
    lines = split_lines(file_block)

    header_end = next((i for i, line in enumerate(lines) if HUNK_HEADER.match(line.rstrip("\n"))), None)
    if header_end is None:
        # Binary file, pure rename, mode-only change, ... — nothing to patch.
        return []

    # A file whose header can't be parsed is skipped, and stays staged.
    file_diff = _parse_file_header(lines[:header_end])
    if file_diff is None:
        return []

    # A deleted file is named by its old path.
    file_path = file_diff.new_path or file_diff.old_path
    assert file_path is not None

    # Group the body by @@ header.
    raw_hunks: list[list[str]] = []
    for line in lines[header_end:]:
        if HUNK_HEADER.match(line.rstrip("\n")):
            raw_hunks.append([])
        raw_hunks[-1].append(line)

    # A new file's blocks usually land in one commit, so it is split at blank
    # lines only on request.
    split_on_blank_lines = split_on_blank_lines and (split_new_files or not file_diff.is_new)

    hunks: list[Hunk] = []
    # Net lines added by this file's earlier sub-hunks: shifts the old-side
    # position of every later one.
    delta = 0
    for raw_hunk in raw_hunks:
        for sub_hunk in _sub_hunks(raw_hunk, min_context):
            # A deleted file must be emptied by its last hunk, so its hunks stay whole.
            if file_diff.is_deleted:
                pieces = [sub_hunk]
            elif hunk_per_line:
                pieces = _split_at_lines(sub_hunk)
            elif split_on_blank_lines:
                pieces = _split_at_blank_lines(sub_hunk)
            else:
                pieces = [sub_hunk]

            # Shift each piece past the earlier sub-hunks, and make it a Hunk.
            for piece in pieces:
                header, body = piece[0], piece[1:]
                old_start, old_count, new_start, new_count, label = parse_hunk_header(header)
                if delta:
                    # Earlier hunks are applied by now, so only the old side
                    # moves; the new side already counts from the staged file.
                    header = _make_hunk_header(old_start + delta, old_count, new_start, new_count, label)

                # The file's first hunk carries its creation, rename or mode change.
                hunks.append(
                    Hunk(
                        file_path=file_path,
                        line_desc=f"L{new_start}-{new_start + new_count - 1}",
                        file=file_diff,
                        lines=[header] + body,
                        first_in_file=not hunks,
                    )
                )

            # Pieces are positioned relative to each other, so the shift moves
            # on once per sub-hunk.
            _, old_count, _, new_count, _ = parse_hunk_header(sub_hunk[0])
            delta += new_count - old_count
    return hunks


def parse_all_hunks(
    diff_text: str,
    min_context: int = SPLIT_CONTEXT,
    hunk_per_line: bool = False,
    split_on_blank_lines: bool = True,
    split_new_files: bool = False,
) -> list[Hunk]:
    """Parse a unified diff into one hunk per temp commit.

    Hunks are split like `git add -p` 's', then at blank lines between
    sibling blocks (unless *split_on_blank_lines* is False, or the file is new
    and *split_new_files* is False), or with *hunk_per_line* at
    every changed line. Each hunk's old-side offset
    accounts for the earlier hunks in its file, so applying the hunks in
    order, each on top of the previous ones, reproduces the diff.

    Binary files and other entries without @@ hunks are skipped.
    """
    file_starts = [m.start() for m in FILE_HEADER.finditer(diff_text)]
    hunks: list[Hunk] = []

    # Each file's block runs from its "diff --git" line to the next one.
    for file_index, start in enumerate(file_starts):
        end = file_starts[file_index + 1] if file_index + 1 < len(file_starts) else len(diff_text)
        hunks.extend(
            _parse_file_block(diff_text[start:end], min_context, hunk_per_line, split_on_blank_lines, split_new_files)
        )
    return hunks


# ---------------------------------------------------------------------------
# Applying hunks
# ---------------------------------------------------------------------------

# Extended header lines slicing knows how to replay.
SUPPORTED_HEADER_PREFIXES = (
    "diff --git ",
    "index ",
    "--- ",
    "+++ ",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
)

# Modes a file can have in a text diff. Gitlinks (160000) are submodules.
SUPPORTED_MODES = {"100644", "100755", "120000"}


@dataclass
class _FileState:
    """A file as the temp commits built so far leave it."""

    mode: str
    lines: list[str]


def _encode(text: str) -> bytes:
    return text.encode("utf-8", errors="surrogateescape")


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="surrogateescape")


def check_supported(hunks: list[Hunk]) -> None:
    """Raise SliceError if any file header has something slicing doesn't model (copies, submodules)."""
    for hunk in hunks:
        if not hunk.first_in_file:
            continue

        # Any other header line would be replayed wrong: a copy, say, would become a rename.
        for line in hunk.file.header_lines:
            if not line.startswith(SUPPORTED_HEADER_PREFIXES):
                raise SliceError(f"{hunk.file_path}: unsupported diff header {line.rstrip()!r}")

        # The new mode must be one fast-import can write as file content.
        if hunk.file.new_mode is not None and hunk.file.new_mode not in SUPPORTED_MODES:
            raise SliceError(f"{hunk.file_path}: unsupported mode {hunk.file.new_mode}")


def hunk_sides(body: list[str]) -> tuple[list[str], list[str]]:
    """Return the (old, new) lines a hunk body replaces and inserts.

    Lines keep their "\\n"; a "\\ No newline at end of file" marker strips it
    from the line before it, on whichever side(s) that line belongs to.
    """
    old: list[str] = []
    new: list[str] = []
    previous: tuple[list[str], ...] = ()

    for line in body:
        # A marker changes the line before it instead of adding one.
        if line.startswith("\\"):
            for side in previous:
                side[-1] = side[-1].removesuffix("\n")
            continue
        if line in ("\n", "\r\n"):
            # diff.suppressBlankEmpty: a blank context line without its leading space.
            text, previous = line, (old, new)
        elif line.startswith(" "):
            text, previous = line[1:], (old, new)
        elif line.startswith("-"):
            text, previous = line[1:], (old,)
        elif line.startswith("+"):
            text, previous = line[1:], (new,)
        else:
            raise SliceError(f"unexpected hunk line {line!r}")

        # Add the line to the side(s) it belongs to.
        for side in previous:
            side.append(text)
    return old, new


def apply_hunk(lines: list[str], hunk_lines: list[str]) -> None:
    """Apply one hunk (its @@ header + body) to a file's lines in place, strictly."""
    old_start, old_count, _, _, _ = parse_hunk_header(hunk_lines[0])
    old, new = hunk_sides(hunk_lines[1:])
    # An empty old side means "insert after line old_start".
    pos = old_start - 1 if old_count else old_start
    if lines[pos : pos + len(old)] != old:
        raise SliceError(f"hunk {hunk_lines[0].rstrip()!r} does not match the file")
    lines[pos : pos + len(old)] = new


# ---------------------------------------------------------------------------
# Building the temp commits
# ---------------------------------------------------------------------------


def temp_commit_message(hunk: Hunk, commit_count: int) -> str:
    """Build a stable, unique temp commit message.

    The message encodes the file path, line range, and a hash of the patch
    content so the AI agent can reason about individual hunks later:
      diff_hash    — identifies the exact patch content
      commit_count — ensures uniqueness when the same patch appears twice
    """
    patch = "".join([hunk.file_path, "\n", *hunk.lines])
    diff_hash = hashlib.sha256(_encode(patch)).hexdigest()[:8]
    return f"temp: {hunk.file_path}:{hunk.line_desc} #{diff_hash}-{commit_count}"


def _diff(revs: list[str], paths: list[str]) -> str:
    """Return `git diff <revs> -- <paths>` as text.

    Undecodable bytes survive as surrogates (surrogateescape), so content
    round-trips exactly when encoded back. The flags guard against user config
    that would make the diff unappliable or relative to the current directory.
    """
    cmd = git(
        "-c",
        "core.quotePath=false",
        "-c",
        "diff.suppressBlankEmpty=false",
        "diff",
        *revs,
        f"-U{DIFF_CONTEXT}",
        "--no-ext-diff",
        "--no-textconv",
        "--no-relative",
        "--",
        *paths,
        _return_cmd=True,
    )
    return _decode(cmd.stdout)


def _staged_diff(paths: list[str]) -> str:
    """Return the staged diff as text."""
    return _diff(["--cached"], paths)


def _ls_tree(treeish: str) -> dict[str, tuple[str, str]]:
    """Return {path: (mode, sha)} for every blob/gitlink in a tree."""
    out = git("ls-tree", "-r", "-z", "--full-tree", treeish, _return_cmd=True).stdout
    entries: dict[str, tuple[str, str]] = {}

    # With -z, records end in NUL and paths are not quoted.
    for record in out.split(b"\0"):
        if not record:
            continue
        # "<mode> <type> <sha>\t<path>"
        meta, path = record.split(b"\t", 1)
        mode, _type, sha = meta.decode().split()
        entries[_decode(path)] = (mode, sha)
    return entries


def _ls_index() -> dict[str, tuple[str, str]]:
    """Return {path: (mode, sha)} for every stage-0 entry in the real index."""
    out = git("ls-files", "-s", "-z", "--full-name", "--", ":/", _return_cmd=True).stdout
    entries: dict[str, tuple[str, str]] = {}

    # With -z, records end in NUL and paths are not quoted.
    for record in out.split(b"\0"):
        if not record:
            continue
        # "<mode> <sha> <stage>\t<path>"; stages 1-3 are unresolved conflicts.
        meta, path = record.split(b"\t", 1)
        mode, sha, stage = meta.decode().split()
        if stage == "0":
            entries[_decode(path)] = (mode, sha)
    return entries


def _read_blobs(shas: list[str]) -> dict[str, bytes]:
    """Read many blobs with a single `git cat-file --batch`."""
    if not shas:
        return {}
    out: bytes = git("cat-file", "--batch", _in="".join(f"{sha}\n" for sha in shas), _return_cmd=True).stdout
    # Each blob comes back as "<sha> <type> <size>\n<content>\n".
    blobs: dict[str, bytes] = {}
    pos = 0
    for sha in shas:
        # A missing object comes back as "<sha> missing", with no content.
        eol = out.index(b"\n", pos)
        header = out[pos:eol].decode().split()
        if len(header) != 3:
            raise SliceError(f"cat-file could not read {sha}")

        # Take exactly <size> bytes of content, then skip the newline after it.
        size = int(header[2])
        blobs[sha] = out[eol + 1 : eol + 1 + size]
        pos = eol + 1 + size + 1
    return blobs


def _load_preimages(hunks: list[Hunk], head_sha: str) -> dict[str, _FileState]:
    """Load HEAD's mode and content for every file the hunks modify, delete or rename."""
    head = _ls_tree(head_sha)
    needed: dict[str, tuple[str, str]] = {}

    # New files have no preimage; every other file is read once, at its first hunk.
    for hunk in hunks:
        if not hunk.first_in_file or hunk.file.old_path is None:
            continue

        # The file must be in HEAD, with a mode fast-import can write back.
        path = hunk.file.old_path
        if path not in head:
            raise SliceError(f"{path}: not in HEAD")
        mode, sha = head[path]
        if mode not in SUPPORTED_MODES:
            raise SliceError(f"{path}: unsupported mode {mode}")
        needed[path] = (mode, sha)

    # Read every blob in one process; files with the same content share a blob.
    blobs = _read_blobs(sorted({sha for _, sha in needed.values()}))
    return {path: _FileState(mode, split_lines(_decode(blobs[sha]))) for path, (mode, sha) in needed.items()}


def _data(payload: bytes) -> Iterator[bytes]:
    """Yield a fast-import data command, which gives the payload's exact byte count."""
    yield b"data %d\n" % len(payload)
    yield payload
    yield b"\n"


def _commit_stream(
    hunks: list[Hunk],
    messages: list[str],
    head_sha: str,
    ref: str,
    author: str,
    committer: str,
    preimages: dict[str, _FileState],
) -> Iterator[bytes]:
    """Yield a fast-import stream with one commit per hunk, chained from head_sha.

    preimages holds HEAD's version of each file the hunks modify; those states
    are updated in place as hunks apply.
    """
    state: dict[str, _FileState | None] = dict(preimages)
    for mark, (hunk, msg) in enumerate(zip(hunks, messages, strict=True), start=1):
        # Collect this commit's file commands in ops, then emit the commit.
        file_diff = hunk.file
        target = hunk.file_path
        ops: list[bytes] = []

        # A file's first hunk sets up its state: created, renamed, or given a new mode.
        if hunk.first_in_file:
            if file_diff.old_path is None:  # new file
                state[target] = _FileState(file_diff.new_mode or "100644", [])
            else:
                # Start from HEAD's version, which _load_preimages put in state.
                current = state.get(file_diff.old_path)
                if current is None:
                    raise SliceError(f"{file_diff.old_path}: no preimage")
                if file_diff.new_path is not None and file_diff.new_path != file_diff.old_path:
                    # Rename: the old path goes away in this same commit.
                    state[file_diff.old_path] = None
                    ops.append(b"D " + _encode(file_diff.old_path) + b"\n")
                if file_diff.new_mode is not None:
                    current.mode = file_diff.new_mode
                state[target] = current

        # Apply the hunk to the file as the earlier commits left it.
        current = state.get(target)
        if current is None:
            raise SliceError(f"{target}: hunk for a file that no longer exists")
        apply_hunk(current.lines, hunk.lines)

        # A deleted file's single hunk empties it; anything else is written in full.
        if file_diff.is_deleted:
            if current.lines:
                raise SliceError(f"{target}: deleted file not empty after its hunks")
            state[target] = None
            ops.append(b"D " + _encode(target) + b"\n")
        else:
            ops.append(b"M " + current.mode.encode() + b" inline " + _encode(target) + b"\n")
            ops.append(b"".join(_data(_encode("".join(current.lines)))))

        # Commits to the same ref chain onto each other; only the first needs a parent.
        yield b"commit " + ref.encode() + b"\n"
        yield b"mark :%d\n" % mark
        yield b"author " + _encode(author) + b"\n"
        yield b"committer " + _encode(committer) + b"\n"
        yield from _data(_encode(msg + "\n"))
        if mark == 1:
            yield b"from " + head_sha.encode() + b"\n"
        yield from ops
        yield b"\n"
    yield b"done\n"


def _run_fast_import(stream: Iterator[bytes]) -> None:
    """Feed a stream to `git fast-import`.

    The stream can be large (full file content per commit). sh pulls it chunk
    by chunk as the pipe accepts it, so memory stays flat.

    If producing the stream fails, the error is held and the input ends
    before the `done` command, so fast-import exits with an error and updates
    no refs; the held error is then raised. Letting it escape would kill sh's
    stdin thread without closing stdin, and fast-import would wait forever.
    BaseException, because SliceError is a SystemExit.
    """
    errors: list[BaseException] = []

    # Ends the stream early on an error, instead of raising in sh's stdin thread.
    def guarded() -> Iterator[bytes]:
        try:
            yield from stream
        except BaseException as e:
            errors.append(e)

    try:
        git("fast-import", "--quiet", "--done", _in=guarded())
    except sh.ErrorReturnCode as e:
        if not errors:
            raise SliceError(f"git fast-import failed: {_decode(e.stderr).strip()}") from e
    if errors:
        raise errors[0]


def _verify(tip: str, hunks: list[Hunk], expected: dict[str, tuple[str, str]], expected_name: str) -> None:
    """Check that every path the hunks touched has the same mode and blob in tip as in *expected*.

    All of a file's hunks get committed, so its final content must be exactly
    what was diffed. Deleted and renamed-away paths must be absent from both.
    """
    touched = {path for hunk in hunks for path in (hunk.file.old_path, hunk.file.new_path) if path}

    tree = _ls_tree(tip)
    for path in sorted(touched):
        # A path absent from both compares None to None, and passes.
        if tree.get(path) != expected.get(path):
            raise SliceError(f"{path}: result differs from {expected_name}")


def _commit_hunks(
    hunks: list[Hunk],
    messages: list[str],
    head_sha: str,
    expected: dict[str, tuple[str, str]],
    expected_name: str,
) -> str:
    """Create one temp commit per hunk on top of head_sha. Returns the new tip; HEAD is not moved.

    Hunks are applied to file contents in Python, and all commits are streamed
    through a single `git fast-import`. Before returning, every touched path in
    the tip is checked against *expected*: the real index, or the tree of the
    commit being sliced.
    """
    check_supported(hunks)
    preimages = _load_preimages(hunks, head_sha)
    # curate_git carries the Git Curate author; the committer is the user.
    author = str(curate_git("var", "GIT_AUTHOR_IDENT")).strip()
    committer = str(git("var", "GIT_COMMITTER_IDENT")).strip()

    # A unique scratch ref, so concurrent slices in other worktrees don't collide.
    ref = f"refs/git-curate/slice-{uuid.uuid4().hex}"
    try:
        _run_fast_import(_commit_stream(hunks, messages, head_sha, ref, author, committer, preimages))
        tip = str(git("rev-parse", "--verify", ref)).strip()
    finally:
        with contextlib.suppress(sh.ErrorReturnCode):
            git("update-ref", "-d", ref)

    _verify(tip, hunks, expected, expected_name)
    return tip


def slice_hunks(
    paths: list[str],
    min_context: int = SPLIT_CONTEXT,
    hunk_per_line: bool = False,
    split_on_blank_lines: bool = True,
    split_new_files: bool = False,
) -> int:
    """Decompose the staged diff into one commit per hunk, or with *hunk_per_line* per changed line.

    HEAD moves once, after all temp commits exist and match the index, so HEAD
    and the real index are untouched if slicing fails.

    Returns the number of atomic commits created.
    """
    try:
        diff_text = _staged_diff(paths)
    except sh.ErrorReturnCode:
        return 0

    hunks = parse_all_hunks(diff_text, min_context, hunk_per_line, split_on_blank_lines, split_new_files)
    if not hunks:
        return 0

    # Build every commit first; slicing fails here without touching HEAD.
    messages = [temp_commit_message(hunk, n) for n, hunk in enumerate(hunks, start=1)]
    head_sha = str(git("rev-parse", "HEAD")).strip()
    try:
        tip = _commit_hunks(hunks, messages, head_sha, _ls_index(), "the index")
    except SliceError as e:
        print(f"error: cannot slice: {e.reason}", file=sys.stderr)
        raise

    # Every commit exists and matches the index: list them, then move HEAD.
    for n, msg in enumerate(messages, start=1):
        print(f"  [{n}] {msg}")
    # The old-value check refuses to move HEAD if it changed while slicing.
    git("update-ref", "-m", "git-curate: slice", "HEAD", tip, head_sha)
    return len(hunks)


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _dry_run_remaining(diff_text: str) -> str:
    """List every hunk in a diff as it appears, without splitting, for dry-run display."""
    lines_out: list[str] = []
    count = 0

    # Each @@ header is listed under the file named by the +++ line before it.
    current_file: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/") :]
        match = HUNK_HEADER.match(line)
        if match and current_file:
            new_start = match.group(3)
            new_count = match.group(4) or "1"
            new_end = int(new_start) + int(new_count) - 1
            count += 1
            lines_out.append(f"  [{count}] {current_file}:L{new_start}-{new_end}")

    return "\n".join(lines_out)


def _apply_from_squash(from_commit: str) -> None:
    """Squash commits from from_commit..HEAD back into the staging area.

    Uses git reset --soft so all those commits become staged changes again,
    ready to be re-sliced at the hunk level.
    """
    parent_sha = resolve_rewrite_from(from_commit)
    git("reset", "--soft", parent_sha)
    print(f"Reset HEAD to {parent_sha[:SHA_DISPLAY_LEN]} (squashed {from_commit!r}..HEAD into staging)\n")


def _ensure_staged_or_stage_all(paths: list[str], all_changes: bool) -> None:
    """Make sure there is something staged before slicing, or exit clearly.

    Possible states:
      - Already staged             → nothing to do, proceed.
      - Nothing staged + --all     → stage the given paths, or all changes to tracked files.
      - Nothing staged or unstaged → say so and return; slicing then finds nothing.
      - Nothing staged, no --all   → tell the user to stage something and exit.
    """
    staged_stat = str(git.diff("--cached", "--stat")).strip()
    if staged_stat:
        # Already have staged changes — proceed.
        return

    unstaged_stat = str(git.diff("--stat")).strip()

    if unstaged_stat and all_changes:
        # --all was passed: stage the given paths, or every tracked file's changes.
        if paths:
            git.add("--", *paths)
        else:
            git.add("-u")
    elif not unstaged_stat:
        print("Nothing to slice — no staged or unstaged changes.")
        return
    else:
        print(
            "Nothing staged. Pass --all to stage and slice all unstaged changes,\n"
            "or stage what you want first with: git add <files>",
            file=sys.stderr,
        )
        raise Exit()


def _print_dry_run_hunks(paths: list[str]) -> None:
    """List the staged diff's hunks, before splitting, without committing anything."""
    print("Dry-run — no commits will be created:\n")
    diff_text = str(git.diff("--cached", "-U3", "--", *paths)) if paths else str(git.diff("--cached", "-U3"))
    output = _dry_run_remaining(diff_text)
    if output:
        print(output)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# Splitting options, shared with the bare `git-curate` command.
SplitContextOption = Annotated[
    int,
    typer.Option(
        "--split-context",
        help=(
            "Minimum run of context lines between two change regions required"
            " to split a hunk. The default of 1 matches git add -p 's'; raise it"
            " for fewer temp commits, or set 0 to disable splitting."
        ),
    ),
]
HunkPerLineOption = Annotated[
    bool,
    typer.Option(
        "--hunk-per-line",
        help=(
            "Give every changed line its own temp commit; an edited line keeps"
            " its removed and added sides together. For small diffs, such as"
            " import blocks, where adjacent lines belong in different commits."
        ),
    ),
]
SplitOnBlankLinesOption = Annotated[
    bool,
    typer.Option(
        "--split-on-blank-lines/--no-split-on-blank-lines",
        help=(
            "Split runs of added or removed lines at blank lines between sibling"
            " blocks. Turn off to keep new code together and get fewer temp commits."
        ),
    ),
]
SplitNewFilesOption = Annotated[
    bool,
    typer.Option(
        "--split-new-files/--no-split-new-files",
        help=("Also split new files at blank lines. Off by default: a new file's blocks usually land in one commit."),
    ),
]


@app.callback()
def slice_command(
    paths: Annotated[
        list[str] | None,
        typer.Argument(
            help="Limit slicing to these files (default: all staged files)",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="List the staged hunks (before --split-context splitting) without committing",
        ),
    ] = False,
    all_changes: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Stage all unstaged changes to tracked files (or the given paths) before slicing",
        ),
    ] = False,
    split_context: SplitContextOption = SPLIT_CONTEXT,
    hunk_per_line: HunkPerLineOption = False,
    split_on_blank_lines: SplitOnBlankLinesOption = True,
    split_new_files: SplitNewFilesOption = False,
    from_commit: Annotated[
        str | None,
        typer.Option(
            "--from",
            help=(
                "Squash commits from this SHA (inclusive) back into the staged area "
                "and re-slice them together with any currently staged changes. "
                "Useful when you want to rewrite existing commits at the hunk level."
            ),
        ),
    ] = None,
) -> None:
    """Slice staged changes into one atomic commit per diff hunk."""
    paths = paths or []

    # --from: squash existing commits back into staging before slicing.
    # Skipped during dry-run because the reset would be permanent even if we
    # never create any commits.
    if from_commit is not None and not dry_run:
        _apply_from_squash(from_commit)

    # Guard: ensure there is actually something staged (or stage it with --all).
    _ensure_staged_or_stage_all(paths, all_changes)

    if dry_run:
        _print_dry_run_hunks(paths)
        return

    print("Slicing hunks into atomic commits...\n")
    n = slice_hunks(
        paths,
        min_context=split_context,
        hunk_per_line=hunk_per_line,
        split_on_blank_lines=split_on_blank_lines,
        split_new_files=split_new_files,
    )

    if n == 0:
        print("Nothing to slice — staged diff is empty.")
    else:
        print(f"\nDone. Created {n} atomic temp commit(s).")
        print(
            "Next step: write a grouping spec from `git-curate diff --tmp`, then run `git-curate group --spec <spec>`."
        )
