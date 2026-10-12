"""
Session status overview
=======================

Prints the current git-curate session state — human-readable and
parseable by an AI skill.

Exit codes:
  0 — always (even when no session is active)

Usage:
------
    uvx git-curate status
"""

from __future__ import annotations

import sh

from .common import SHA_DISPLAY_LEN, SubApp, count_commits_since, git, resolve_base
from .slice import staged_paths

app = SubApp()


@app.callback()
def status_command() -> None:
    """Show the current git-curate session state."""
    base = resolve_base()
    if base is not None:
        has_session = True
        try:
            n = count_commits_since(base)
        except sh.ErrorReturnCode:
            n = 0
        commit_suffix = "s" if n != 1 else ""
        session_line = f"Session: active ({n} temp commit{commit_suffix})"
    else:
        has_session = False
        # No session — base is HEAD: where a future slice would start from.
        base = str(git("rev-parse", "HEAD")).strip()
        session_line = "Session: none"

    base_subject = str(git.log("-1", "--format=%s", base)).strip()
    base_line = f"Base:    {base[:SHA_DISPLAY_LEN]} — {base_subject}"

    staged = staged_paths([])
    staged_count = len(staged)
    file_suffix = "s" if staged_count != 1 else ""
    staged_hint = ""
    if staged_count > 0:
        # Mid-session, staged files are usually ones slice couldn't slice.
        staged_hint = " — not in any temp commit; `group` leaves them unstaged" if has_session else " — ready to slice"
    staged_line = f"Staged:  {staged_count} file{file_suffix}{staged_hint}"

    unstaged = str(git.diff("--name-only")).strip()
    unstaged_count = len(unstaged.splitlines()) if unstaged else 0
    unstaged_suffix = "s" if unstaged_count != 1 else ""
    unstaged_hint = (
        " — stage first, or use --all" if unstaged_count > 0 and staged_count == 0 and not has_session else ""
    )
    unstaged_line = f"Unstaged: {unstaged_count} file{unstaged_suffix}{unstaged_hint}"

    print(session_line)
    print(base_line)
    print(staged_line)
    if has_session:
        for path in staged:
            print(f"  {path}")
    print(unstaged_line)
