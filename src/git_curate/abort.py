"""
Abort a git-curate session
==========================

Resets HEAD to the session base, removing all temp commits slice created.
Leaves changes unstaged in the working tree.

With no active session, says so and exits successfully.

Usage:
------
    uvx git-curate abort
"""

from __future__ import annotations

from .common import SHA_DISPLAY_LEN, SubApp, abort_session, resolve_base

app = SubApp()


@app.callback()
def abort_command() -> None:
    """Reset HEAD to the session base, removing all temp commits."""
    base_sha = resolve_base()
    if base_sha is None:
        print("No git-curate session in progress — nothing to abort.")
        return

    n = abort_session(base_sha)
    suffix = "s" if n != 1 else ""
    print(f"Aborted. Reset HEAD to {base_sha[:SHA_DISPLAY_LEN]} (dropped {n} temp commit{suffix}).")
