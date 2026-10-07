"""
Step 3: Finalize — squash temp commits into final commits
=========================================================

Takes a base ref and a grouping specification (JSON), then squashes
the temp commits into logical final commits via non-interactive rebase.

The grouping spec references commits by message, not SHA, because SHAs
change across rewrites.

Grouping spec format (JSON file or stdin):
------------------------------------------
    [
        {
            "message": "feat: add auth endpoint",
            "commits": [
                "temp: src/auth.py:L1-15 #3f2a9c1e-1",
                "temp: src/auth.py:L20-30 #b7d04e52-2",
                "temp: tests/test_auth.py:L1-10 #0c9e7a13-3"
            ]
        },
        {
            "message": "refactor: simplify schema validation",
            "commits": [
                "temp: src/schema.py:L5-25 #5e81d2fa-4",
                "temp: src/schema.py:L40-60 #a4c7310b-5"
            ]
        }
    ]

Messages must match exactly. Commits not mentioned in any group get picked
unchanged, after all the groups.

Algorithm:
----------
1. Detect the base by walking HEAD backward to the first non-curate commit
   (or accept an explicit base argument).
2. List all commits between base and HEAD, keyed by message.
3. For each group in the spec, resolve message → current SHA.
4. Build a rebase plan: the first commit in each group gets "pick"
   followed by an "exec git commit --amend -F <file> --reset-author"
   to set the final message and restore the real author; the rest get
   "fixup". Ungrouped commits get plain "pick".
   Each commit message goes into a file under the git dir so newlines and special
   characters don't break the plan.
5. Set GIT_SEQUENCE_EDITOR to a script that replaces the rebase todo
   with this plan, then execute `git rebase -i --autostash <base>`
   non-interactively.
6. If the rebase stops, abort it (HEAD returns to the temp commits) and save
   the spec under the git dir so the grouping work can be revised, not redone.

Usage:
------
    # From stdin (default):
    echo '[...]' | uvx git-curate group

    # From a JSON file (deleted after success):
    uvx git-curate group --spec groups.json

    # Keep the spec file after success:
    uvx git-curate group --spec groups.json --keep-spec

    # Explicit base ref:
    uvx git-curate group main --spec groups.json

    # Dry-run (show the rebase plan without executing):
    uvx git-curate group --dry-run
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
from dataclasses import dataclass
from typing import Annotated

import sh
import typer

from .common import (
    SHA_DISPLAY_LEN,
    Commit,
    InvalidSpecError,
    NoSessionError,
    RebaseFailedError,
    SubApp,
    clear_failed_attempt,
    git,
    list_commits,
    pre_checks,
    rebase_in_progress,
    resolve_base,
    save_failed_attempt,
)

app = SubApp()


@dataclass
class Group:
    message: str
    commits: list[str]


@dataclass
class AmendEntry:
    message: str


def resolve_message_to_sha(
    commits: list[Commit],
    message: str,
) -> str | None:
    """Find the SHA for a commit by its message. Returns None if not found."""
    for commit in commits:
        if commit.message == message:
            return commit.sha
    return None


# ---------------------------------------------------------------------------
# Rebase plan generation
# ---------------------------------------------------------------------------


def _resolve_group_shas(
    group: Group,
    msg_to_sha: dict[str, str],
    claimed_shas: set[str],
    errors: list[str],
) -> list[str]:
    """Resolve a single group's commit messages to SHAs.

    Collects errors into the shared `errors` list rather than raising, so all
    problems across all groups are reported in one pass before aborting.

    Populates `claimed_shas` in-place to detect commits assigned to multiple
    groups.
    """
    group_shas: list[str] = []

    for commit_message in group.commits:
        sha = msg_to_sha.get(commit_message)
        if sha is None:
            errors.append(f"commit not found for message: {commit_message!r}\n  (in group {group.message!r})")
            continue
        if sha in claimed_shas:
            errors.append(f"commit claimed by multiple groups: {commit_message!r}")
            continue
        claimed_shas.add(sha)
        group_shas.append(sha)

    return group_shas


def build_rebase_plan(
    commits: list[Commit],
    groups: list[Group],
) -> list[str | AmendEntry]:
    """Build a rebase plan from a grouping spec.

    Returns a list of entries, each either:
      - a string      → raw rebase todo line ("pick <sha>" or "fixup <sha>")
      - AmendEntry    → amend the preceding pick with this final message

    AmendEntry is resolved to an "exec git commit --amend" line at execution
    time so that multi-line messages and special characters are safe (they get
    written to a temp file, not embedded in the shell command).
    """
    # Build a message → SHA lookup so the spec can reference commits by
    # human-readable message instead of SHA (which changes across rewrites).
    msg_to_sha: dict[str, str] = {}
    for commit in commits:
        if commit.message in msg_to_sha:
            print(
                f"warning: duplicate commit message: {commit.message!r}\n  using last occurrence",
                file=sys.stderr,
            )
        msg_to_sha[commit.message] = commit.sha

    # claimed_shas prevents one commit from appearing in two groups.
    claimed_shas: set[str] = set()
    todo_lines: list[str | AmendEntry] = []
    errors: list[str] = []

    for group in groups:
        if not group.commits:
            errors.append(f"group {group.message!r} has no commits")
            continue

        group_shas = _resolve_group_shas(group, msg_to_sha, claimed_shas, errors)
        if not group_shas:
            continue

        # First commit in the group: pick it, then amend its message.
        # --reset-author (applied later) restores the real user identity;
        # temp commits carry the Git Curate <git-curate@local> author.
        todo_lines.append(f"pick {group_shas[0]}")
        todo_lines.append(AmendEntry(group.message))

        # Remaining commits in the group: squash silently into the first.
        for sha in group_shas[1:]:
            todo_lines.append(f"fixup {sha}")

    if errors:
        print("Errors in grouping spec:\n", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        print(file=sys.stderr)
        raise InvalidSpecError()

    # Append any commits not claimed by any group as plain picks, preserving
    # their original order in the log so we don't reorder history.
    ungrouped = [c.sha for c in commits if c.sha not in claimed_shas]
    for sha in ungrouped:
        todo_lines.append(f"pick {sha}")

    return todo_lines


# ---------------------------------------------------------------------------
# Rebase execution
# ---------------------------------------------------------------------------


def _expand_plan_to_todo_lines(plan: list[str | AmendEntry], tmpdir: str) -> list[str]:
    """Expand AmendEntry sentinels into "exec git commit --amend" lines.

    Each message is written to a numbered temp file inside tmpdir so that
    newlines and special characters in commit messages don't break the shell
    command.  The files must outlive this function because the rebase reads
    them during execution.
    """
    todo_lines: list[str] = []
    amend_idx = 0

    for entry in plan:
        if isinstance(entry, AmendEntry):
            msg_path = os.path.join(tmpdir, f"msg_{amend_idx}.txt")
            with open(msg_path, "w") as f:
                f.write(entry.message)
            # --reset-author: restore real user identity after squashing temp
            # commits that carry the Git Curate <git-curate@local> author.
            todo_lines.append(f"exec git commit --amend -F '{msg_path}' --reset-author")
            amend_idx += 1
        else:
            todo_lines.append(entry)

    return todo_lines


def write_sequence_editor_script(
    todo_lines: list[str],
    script_path: str,
) -> None:
    """Write a shell script that replaces the rebase todo with our plan."""
    # Write the plan to a sibling file so the script can cp it —
    # embedding the content inline triggers heredoc quoting issues.
    plan_path = script_path + ".plan"
    with open(plan_path, "w") as f:
        f.write("\n".join(todo_lines) + "\n")

    script = f"#!/bin/sh\ncp '{plan_path}' \"$1\"\n"
    with open(script_path, "w") as f:
        f.write(script)
    os.chmod(script_path, 0o755)


def execute_rebase(
    base: str,
    plan: list[str | AmendEntry],
) -> None:
    """Run a non-interactive rebase with our plan.

    Writes the plan and the per-message files for the amend steps into a
    directory under the git dir.

    If the rebase stops (a conflict or a failing exec), it is aborted rather
    than left paused: the tool's `-c` settings would not survive a manual
    `git rebase --continue`, and the fix is a revised spec anyway.  HEAD is
    back on the temp commits when RebaseFailedError is raised.
    """
    pre_checks()

    # The amend steps read their message files while the rebase runs.
    msg_dir = str(git("rev-parse", "--git-path", "git-curate-group")).strip()
    shutil.rmtree(msg_dir, ignore_errors=True)  # stale, from an aborted rebase
    os.makedirs(msg_dir)

    # Expand AmendEntry sentinels into "exec" lines, writing each
    # message to a file so special characters are safe.
    todo_lines = _expand_plan_to_todo_lines(plan, os.path.abspath(msg_dir))

    # git runs the sequence editor on its todo; ours swaps in the plan.
    seq_script = os.path.join(msg_dir, "sequence-editor.sh")
    write_sequence_editor_script(todo_lines, seq_script)
    env = os.environ.copy()
    env["GIT_SEQUENCE_EDITOR"] = os.path.abspath(seq_script)

    try:
        result = git.rebase(
            "-i",
            "--autostash",
            base,
            _env=env,
            _err_to_out=True,
        )
        print(str(result).strip())
    except sh.ErrorReturnCode as e:
        output = e.stdout.decode(errors="replace")
        if rebase_in_progress():  # it may have failed before starting
            git.rebase("--abort")
        print(
            f"Rebase stopped:\n{textwrap.indent(output.strip(), '    ')}\n\n"
            "Aborted the rebase; the temp commits are restored.",
            file=sys.stderr,
        )
        raise RebaseFailedError(output) from e
    finally:
        shutil.rmtree(msg_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _resolve_base_or_exit(base: str | None) -> str:
    """Return the base SHA to use, auto-detecting it if not explicitly provided.

    Auto-detection walks HEAD backward to find the first commit not authored
    by git-curate@local (i.e., the commit before the slice session started).
    Exits with a clear error if no session is in progress and no base was given.
    """
    if base is not None:
        return base

    detected = resolve_base()
    if detected is None:
        print(
            "error: no git-curate session in progress.\nRun 'git-curate slice' first, or pass an explicit base ref.",
            file=sys.stderr,
        )
        raise NoSessionError()

    print(f"Detected base: {detected[:SHA_DISPLAY_LEN]}\n")
    return detected


def _read_spec(spec: str) -> str:
    """Read the grouping spec from stdin ('-') or a file path."""
    if spec == "-":
        return sys.stdin.read()
    with open(spec) as f:
        return f.read()


def _parse_spec_json(spec_text: str) -> list[Group]:
    """Parse the JSON grouping spec, exiting with a readable error on failure."""
    try:
        data: list[dict] = json.loads(spec_text)
        return [Group(message=g["message"], commits=g["commits"]) for g in data]
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in spec: {e}", file=sys.stderr)
        raise InvalidSpecError() from e


def _print_rebase_plan(commits: list[Commit], groups: list[Group], todo_lines: list[str | AmendEntry]) -> None:
    """Print a human-readable summary of the rebase plan before executing it."""
    ungrouped_count = len(commits) - sum(len(g.commits) for g in groups)
    print(f"Rebase plan ({len(commits)} commits → {len(groups)} groups + {ungrouped_count} ungrouped):\n")

    for entry in todo_lines:
        if isinstance(entry, AmendEntry):
            # Show only the first line of multi-line messages to keep output readable.
            first_line = entry.message.split("\n")[0]
            suffix = "..." if "\n" in entry.message else ""
            print(f"  exec git commit --amend -m '{first_line}{suffix}'")
        else:
            print(f"  {entry}")
    print()


def _cleanup_spec_file(spec: str) -> None:
    """Delete the spec file after a successful run.

    The spec is only useful once; leaving it around can cause confusion if
    git-curate group is re-run accidentally.
    """
    try:
        os.remove(spec)
        print(f"Removed spec file {spec}.")
    except OSError as e:
        print(f"warning: could not remove spec file {spec}: {e}", file=sys.stderr)


def _check_ordering_constraints(
    todo_lines: list[str | AmendEntry],
    deps: list,
    commits: list[Commit],
) -> None:
    """Raise InvalidSpecError if the rebase plan reverses any ordering constraint.

    *deps* is a list of ``deps.Dependency`` objects.  Each constraint says
    that ``earlier_msg`` must appear before ``later_msg`` in the rebase plan.
    """
    # Build SHA → plan position from pick/fixup lines only.
    sha_order: dict[str, int] = {}
    for entry in todo_lines:
        if isinstance(entry, str) and (entry.startswith("pick ") or entry.startswith("fixup ")):
            parts = entry.split()
            if len(parts) >= 2:
                sha_order[parts[1]] = len(sha_order)

    msg_to_sha = {c.message: c.sha for c in commits}

    errors: list[str] = []
    for dep in deps:
        a_sha = msg_to_sha.get(dep.earlier_msg)
        b_sha = msg_to_sha.get(dep.later_msg)
        if a_sha is None or b_sha is None:
            continue
        pos_a = sha_order.get(a_sha)
        pos_b = sha_order.get(b_sha)
        if pos_a is None or pos_b is None:
            continue
        if pos_a > pos_b:
            errors.append(f"must precede: {dep.earlier_msg!r}\n    comes after: {dep.later_msg!r}")

    if errors:
        print("Ordering constraint violations in grouping spec:\n", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        print(file=sys.stderr)
        raise InvalidSpecError()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@app.callback()
def group_command(
    base: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Base ref — commits from here to HEAD are considered "
                "(e.g. main, a1b2c3d). Defaults to auto-detection by walking "
                "HEAD backward to the first non-curate commit."
            ),
        ),
    ] = None,
    spec: Annotated[
        str,
        typer.Option(
            "--spec",
            help="Path to JSON grouping spec, or '-' for stdin (default: stdin)",
        ),
    ] = "-",
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show the rebase plan without executing",
        ),
    ] = False,
    keep_spec: Annotated[
        bool,
        typer.Option(
            "--keep-spec",
            help="Keep the spec file after a successful run (default: delete it)",
        ),
    ] = False,
) -> None:
    """Group and squash temp commits into logical final commits."""
    base = _resolve_base_or_exit(base)

    commits = list_commits(base)
    if not commits:
        print(f"No commits found between {base} and HEAD.")
        return

    # Read and parse the grouping spec.
    spec_text = _read_spec(spec)
    groups = _parse_spec_json(spec_text)

    # Build the rebase plan, then check it against hunk-overlap constraints.
    todo_lines = build_rebase_plan(commits, groups)

    from .deps import compute_dependencies

    deps = compute_dependencies(base)
    if deps:
        _check_ordering_constraints(todo_lines, deps, commits)

    _print_rebase_plan(commits, groups, todo_lines)

    if dry_run:
        print("Dry-run — no rebase executed.")
        return

    try:
        execute_rebase(base, todo_lines)
    except RebaseFailedError as e:
        # The spec took an agent's whole session to write; keep it for revising.
        saved = save_failed_attempt(base, spec_text, e.output)
        print(
            f"\nSaved the spec to {saved}.\n"
            "Revise the grouping (usually the ordering) and run `git-curate group` again.",
            file=sys.stderr,
        )
        raise
    clear_failed_attempt()

    # Clean up the spec file now that the rebase succeeded.
    # If the user passed --keep-spec or read from stdin, skip this.
    if spec != "-" and not keep_spec:
        _cleanup_spec_file(spec)

    print("\nDone. Run `git log --oneline` to inspect the result.")
