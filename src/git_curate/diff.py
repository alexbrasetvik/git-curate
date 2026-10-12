"""
Step 2 helper: emit the unformatted diff for grouping
======================================================

Prints `git log -p` from a base ref to HEAD, with no pager, no colour,
and no other formatting — ready to pipe to a model or script.

Without an explicit base, resolve_base() finds the first non-curate commit
walking back from HEAD. With no active session and no explicit base, the
command prints a notice to stderr and exits successfully.

With --tmp, the diff goes to git-curate-diff.patch in the git dir (per
worktree, see `git rev-parse --git-path`) and the command prints
"<path> <line-count>" instead.

Usage:
------
    # Default: diff since the git-curate session base
    uvx git-curate diff

    # Explicit base ref or SHA
    uvx git-curate diff main
    uvx git-curate diff a1b2c3d

    # Write to git-curate-diff.patch in the git dir, print its path and line count
    uvx git-curate diff --tmp
"""

from __future__ import annotations

import os
import sys
from typing import Annotated

import sh
import typer

from .common import Exit, SubApp, git, resolve_base

app = SubApp()


@app.callback()
def diff_command(
    base: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Base ref — commits from here to HEAD are shown. Defaults to auto-detection from the active session."
            ),
        ),
    ] = None,
    tmp: Annotated[
        bool,
        typer.Option(
            "--tmp",
            help="Write the diff to git-curate-diff.patch in the git dir and print its path and line count instead.",
        ),
    ] = False,
) -> None:
    """Print git log -p from base to HEAD, unformatted."""
    if base is None:
        base = resolve_base()
        if base is None:
            print("No git-curate session in progress.", file=sys.stderr)
            return

    try:
        output = str(git.log("--no-color", "-p", f"{base}..HEAD")).strip()
    except sh.ErrorReturnCode as e:
        print(f"error: git log failed: {e}", file=sys.stderr)
        raise Exit() from e

    if not output:
        if tmp:
            print("error: diff is empty — no commits between base and HEAD", file=sys.stderr)
            raise Exit()
        return

    if tmp:
        # In a linked worktree, .git is a file; --git-path gives the worktree's own git dir.
        patch_path = os.path.abspath(str(git("rev-parse", "--git-path", "git-curate-diff.patch")).strip())
        with open(patch_path, "w") as f:
            f.write(output)
        line_count = output.count("\n") + 1
        print(f"{patch_path} {line_count}")
    else:
        print(output)
