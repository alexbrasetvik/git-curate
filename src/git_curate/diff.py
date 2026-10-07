"""
Step 2 helper: emit the unformatted diff for grouping
======================================================

Prints `git log -p` from a base ref to HEAD, with no pager, no colour,
and no other formatting — ready to pipe to a model or script.

Without an explicit base, resolve_base() finds the first non-curate commit
walking back from HEAD. With no active session and no explicit base, the
command prints a notice to stderr and exits successfully.

With --tmp, the diff goes to .git/git-curate-diff.patch and the command
prints "<path> <line-count>" instead.

Usage:
------
    # Default: diff since the git-curate session base
    uvx git-curate diff

    # Explicit base ref or SHA
    uvx git-curate diff main
    uvx git-curate diff a1b2c3d

    # Write to .git/git-curate-diff.patch, print its path and line count
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
            help="Write the diff to .git/git-curate-diff.patch and print its path and line count instead.",
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
        repo_root = str(git("rev-parse", "--show-toplevel")).strip()
        patch_path = os.path.join(repo_root, ".git", "git-curate-diff.patch")
        with open(patch_path, "w") as f:
            f.write(output)
        line_count = output.count("\n") + 1
        print(f"{patch_path} {line_count}")
    else:
        print(output)
