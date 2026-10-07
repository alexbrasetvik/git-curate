"""
Session commit log
==================

Lists commit subjects since a base commit, oldest first. Without a base,
auto-detects from the active session. With no active session and no base,
says so and exits successfully.

Intended for agent usage, without granting them full git access.

Usage:
------
    # Auto-detect base from active session:
    uvx git-curate log

    # Explicit base (e.g. copied from `uvx git-curate status`):
    uvx git-curate log a1b2c3d
"""

from __future__ import annotations

from typing import Annotated

import typer

from .common import SHA_DISPLAY_LEN, SubApp, git, resolve_base

app = SubApp()


@app.callback()
def log_command(
    base: Annotated[
        str | None,
        typer.Argument(
            help=("Base commit — show commits from here to HEAD. Defaults to the active session base (from `status`)."),
        ),
    ] = None,
) -> None:
    """List temp commits in the current git-curate session."""
    if base is None:
        base = resolve_base()
        if base is None:
            print("No git-curate session in progress.")
            return

    lines = str(git.log("--reverse", "--format=%s", f"{base}..HEAD")).strip().splitlines()
    if not lines:
        print("No commits above the base.")
        return

    n = len(lines)
    commit_suffix = "s" if n != 1 else ""
    print(f"{n} commit{commit_suffix} since {base[:SHA_DISPLAY_LEN]}:\n")
    for i, msg in enumerate(lines, 1):
        print(f"  {i}. {msg}")
