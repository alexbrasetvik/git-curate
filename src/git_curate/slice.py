"""
Phase 1: Hunk-level commit decomposition
=========================================

Problem
-------
AI agents can stage whole files (git add) but cannot drive git add -p.
They produce large file-level commits where you want many small, logical ones.

Three-phase approach
--------------------
This script is Phase 1 of three:

  Phase 1 (this slice tool, mechanical):
    Decomposes all staged changes into one atomic commit per diff hunk.

  Phase 2 (AI agent):
    Reads the N temp commits and decides which hunks belong together
    (e.g. "commits 1-5 → feat: auth, 6-12 → refactor: schema,
    13-15 → test: auth"). Outputs a grouping spec (JSON).

  Phase 3 (group tool, mechanical):
    Executes the rebase plan non-interactively via GIT_SEQUENCE_EDITOR,
    collapsing the temp commits into clean final commits.

Algorithm (Phase 1)
-------------------
  1. Run `git diff --cached -U3` on the index (or specified files).
  2. Parse the first hunk from the output (first @@ block of the first
     file), including the required diff/---/+++ header lines, to form
     a self-contained mini-patch.
  3. Apply that hunk to a throwaway temp index (GIT_INDEX_FILE) seeded
     from HEAD. The real staged index stays untouched.
  4. Write the resulting tree and create a commit via git commit-tree
     (plumbing, no hooks), then advance HEAD with git update-ref.
     Temp commits carry the Git Curate <git-curate@local> author so
     group and abort can detect them.
  5. Repeat from step 1. HEAD advances each iteration so the staged diff
     shrinks as committed hunks move into HEAD.
  6. Stop when `git diff --cached` returns empty.

Replaying them in order reconstructs the original staged state.

Key properties
--------------
- Non-destructive: the real staged index stays untouched.
- No stale offsets: the loop re-reads the diff each iteration.
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

    # Dry-run — show what would be committed without committing:
    uvx git-curate slice --dry-run

    # Stage all unstaged changes then slice:
    uvx git-curate slice --all

    # Rewrite history from an earlier commit (inclusive):
    uvx git-curate slice --from abc1234

    # Slice again after making more changes (iterative workflow):
    uvx git-curate slice
"""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from typing import Annotated, Any

import sh
import typer

from .common import Exit, SubApp, curate_git, git, resolve_rewrite_from, temp_git_index

app = SubApp()


@dataclass
class Hunk:
    file_path: str
    line_desc: str
    mini_patch: str


# ---------------------------------------------------------------------------
# Patch parsing
# ---------------------------------------------------------------------------

# Matches a per-file diff header: "diff --git a/foo b/foo"
FILE_HEADER = re.compile(r"^diff --git a/.+ b/.+$", re.MULTILINE)

# Matches a hunk header: "@@ -start,count +start,count @@ optional context"
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def _is_diff_context(line: str) -> bool:
    """Return True if line is a context line (space-prefixed or bare blank).

    Git normally prefixes context lines with a space, but diff.suppressBlankEmpty
    emits bare newlines instead. Treat both as context to keep gap-counting correct.
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

    while i < len(body):
        line = body[i]
        if _is_diff_context(line):
            # Accumulate context lines and look ahead for more changes
            ctx_start = i
            while i < len(body) and _is_diff_context(body[i]):
                i += 1
            ctx_run = body[ctx_start:i]

            # If there are changes before AND after this context, and the
            # run is long enough, this is a split point.
            has_changes_before = any(line.startswith(("+", "-")) for line in current)
            has_changes_after = i < len(body)

            if has_changes_before and has_changes_after and len(ctx_run) >= min_context:
                # End the current region with trailing context
                current.extend(ctx_run)
                regions.append(current)
                # Start a new region with leading context (same lines)
                current = list(ctx_run)
            else:
                current.extend(ctx_run)
        else:
            current.append(line)
            i += 1

    if current:
        regions.append(current)

    if len(regions) <= 1:
        return None

    return regions


def _make_hunk_header(old_start: int, old_count: int, new_start: int, new_count: int, label: str = "") -> str:
    """Format a @@ hunk header line."""
    return f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{label}\n"


def _rebuild_sub_hunk_headers(original_header: str, sub_hunks: list[list[str]]) -> list[list[str]]:
    """Recompute @@ headers for each sub-hunk after a split.

    Assigns correct line numbers and counts so git apply accepts each sub-hunk.
    """
    match = HUNK_HEADER.match(original_header.rstrip("\n"))
    assert match is not None
    old_pos = int(match.group(1))
    new_pos = int(match.group(3))
    label = match.group(5) or ""

    result: list[list[str]] = []
    for body_lines in sub_hunks:
        old_count = sum(1 for line in body_lines if _is_diff_context(line) or line.startswith("-"))
        new_count = sum(1 for line in body_lines if _is_diff_context(line) or line.startswith("+"))
        header = _make_hunk_header(old_pos, old_count, new_pos, new_count, label)
        result.append([header] + body_lines)

        # Advance positions: context and removed lines consume old,
        # context and added lines consume new.
        for line in body_lines:
            if _is_diff_context(line):
                old_pos += 1
                new_pos += 1
            elif line.startswith("-"):
                old_pos += 1
            elif line.startswith("+"):
                new_pos += 1

    return result


def parse_first_hunk(diff_text: str, min_context: int = 4) -> Hunk | None:
    """Extract the first hunk from a unified diff as a self-contained mini-patch.

    Returns (file_path, line_desc, mini_patch) or None if diff is empty /
    unparseable.

    The mini-patch is a fully valid unified diff that `git apply` will accept:
      - diff --git header
      - --- / +++ lines
      - exactly one @@ hunk

    Binary files (no +++ / @@ lines) get skipped; the function tries the next file.
    """
    if not diff_text.strip():
        return None

    # -- locate all file headers --
    file_starts = [m.start() for m in FILE_HEADER.finditer(diff_text)]
    if not file_starts:
        return None

    # Iterate through files in order; skip binary files (no +++ b/ or @@ lines)
    for file_index, start in enumerate(file_starts):
        end = file_starts[file_index + 1] if file_index + 1 < len(file_starts) else len(diff_text)
        file_block = diff_text[start:end]

        lines = file_block.splitlines(keepends=True)

        # -- collect the file-level header (diff, index, ---, +++) --
        header_lines: list[str] = []
        hunk_start_idx: int | None = None
        file_path: str | None = None

        a_path: str | None = None
        for i, line in enumerate(lines):
            if line.startswith("+++ b/"):
                file_path = line[len("+++ b/") :].strip()
            elif line.startswith("--- a/"):
                a_path = line[len("--- a/") :].strip()
            if HUNK_HEADER.match(line.rstrip("\n")):
                hunk_start_idx = i
                break
            header_lines.append(line)

        # Fallback for deleted files: +++ /dev/null → use the --- a/<file> path
        if file_path is None and a_path is not None:
            file_path = a_path

        if hunk_start_idx is None or file_path is None:
            # Binary file or other non-patchable entry — try the next file
            continue

        # -- collect exactly the first hunk (from @@ up to next @@ or EOF) --
        hunk_lines: list[str] = [lines[hunk_start_idx]]
        for line in lines[hunk_start_idx + 1 :]:
            if HUNK_HEADER.match(line.rstrip("\n")):
                break
            hunk_lines.append(line)

        # Try to split the hunk (like git add -p 's').
        # On success, commit only the first sub-hunk; the rest stay staged for the next iteration.
        # min_context=0 disables splitting.
        sub_hunks = _split_hunk(hunk_lines, min_context) if min_context > 0 else None
        if sub_hunks is not None:
            hunk_lines = _rebuild_sub_hunk_headers(hunk_lines[0], sub_hunks)[0]

        # Parse the hunk header for a human-readable description
        match = HUNK_HEADER.match(hunk_lines[0].rstrip("\n"))
        assert match is not None
        new_start = match.group(3)
        new_count = match.group(4) or "1"
        new_end = int(new_start) + int(new_count) - 1
        line_desc = f"L{new_start}-{new_end}"

        mini_patch = "".join(header_lines) + "".join(hunk_lines)

        # Ensure the patch ends with a newline so `git apply` doesn't complain
        if not mini_patch.endswith("\n"):
            mini_patch += "\n"

        return Hunk(file_path=file_path, line_desc=line_desc, mini_patch=mini_patch)

    return None


# ---------------------------------------------------------------------------
# Slice loop
# ---------------------------------------------------------------------------


def _commit_single_hunk(git_tmp: Any, hunk: Hunk, commit_count: int) -> None:
    """Apply one hunk to the throwaway index and advance HEAD with a new temp commit.

    git_tmp is a baked sh command that points at a separate GIT_INDEX_FILE,
    so the real staged index is never touched.

    The commit message encodes the file path, line range, and a hash of the
    patch content so the AI agent can reason about individual hunks later.
    """
    # Seed the temp index from HEAD, then layer this hunk on top of it.
    git_tmp("read-tree", "HEAD")
    git_tmp.apply("--cached", _in=hunk.mini_patch)
    new_tree = str(git_tmp("write-tree")).strip()

    # Build a stable, unique commit message:
    #   diff_hash    — identifies the exact patch content
    #   commit_count — ensures uniqueness when the same patch appears twice
    head_sha = str(git("rev-parse", "HEAD")).strip()
    diff_hash = hashlib.sha256(hunk.mini_patch.encode()).hexdigest()[:8]
    msg = f"temp: {hunk.file_path}:{hunk.line_desc} #{diff_hash}-{commit_count}"

    # commit-tree + update-ref: plumbing path that bypasses hooks.
    # curate_git sets GIT_AUTHOR_NAME/EMAIL to git-curate@local so we can
    # identify our temp commits later (abort, status, etc.).
    new_sha = str(curate_git("commit-tree", new_tree, "-p", head_sha, "-m", msg)).strip()
    git("update-ref", "HEAD", new_sha)

    print(f"  [{commit_count}] {msg}")


def slice_hunks(paths: list[str], min_context: int = 4) -> int:
    """Decompose the staged diff into one commit per hunk.

    Uses a temporary GIT_INDEX_FILE so the real staged index stays untouched.
    Each iteration parses the first hunk from the current staged diff, commits
    it, then loops — HEAD advances each round, shrinking the staged diff until
    nothing remains.

    Returns the number of atomic commits created.
    """
    commit_count = 0

    with temp_git_index() as git_tmp:
        while True:
            # Re-read the diff every iteration: HEAD has advanced, so hunks
            # that were committed in previous rounds have moved out of the
            # staged diff and into HEAD.  Stale offsets would break git apply.
            try:
                diff_text = (
                    str(git.diff("--cached", "-U3", "--", *paths)) if paths else str(git.diff("--cached", "-U3"))
                )
            except sh.ErrorReturnCode:
                break

            result = parse_first_hunk(diff_text, min_context)
            if result is None:
                # Staged diff is empty — every hunk has been committed.
                break

            commit_count += 1
            _commit_single_hunk(git_tmp, result, commit_count)

    return commit_count


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _dry_run_remaining(diff_text: str) -> str:
    """Parse all hunks in a diff for dry-run display."""
    lines_out: list[str] = []
    count = 0

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
    print(f"Reset HEAD to {parent_sha[:12]} (squashed {from_commit!r}..HEAD into staging)\n")


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
    """Show all hunks that would be committed, without actually committing."""
    print("Dry-run — no commits will be created:\n")
    diff_text = str(git.diff("--cached", "-U3", "--", *paths)) if paths else str(git.diff("--cached", "-U3"))
    output = _dry_run_remaining(diff_text)
    if output:
        print(output)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


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
            help="Show the hunks that would be committed without committing",
        ),
    ] = False,
    all_changes: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Stage all unstaged changes to tracked files (or the given paths) before slicing",
        ),
    ] = False,
    split_context: Annotated[
        int,
        typer.Option(
            "--split-context",
            help=(
                "Minimum run of context lines between two change regions required"
                " to split a hunk (like git add -p 's'). Set to 0 to disable splitting."
            ),
        ),
    ] = 4,
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
    n = slice_hunks(paths, min_context=split_context)

    if n == 0:
        print("Nothing to slice — staged diff is empty.")
    else:
        print(f"\nDone. Created {n} atomic temp commit(s).")
        print("Next step: run Phase 2 to semantically group these commits.")
