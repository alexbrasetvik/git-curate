"""End-to-end orchestration: optional rewrite → slice → AI grouping."""

from __future__ import annotations

import sys

import sh
import typer

from .common import (
    SHA_DISPLAY_LEN,
    Exit,
    abort_session,
    clear_failed_attempt,
    count_commits_since,
    git,
    load_failed_attempt,
    resolve_base,
    resolve_rewrite_from,
)
from .harness import get_harness, resolve_harness_name, resolve_model
from .slice import slice_hunks

# ── Low-level git helpers ────────────────────────────────────────────────────


def _staged_files() -> list[str]:
    return [f for f in str(git.diff("--cached", "--name-only")).strip().splitlines() if f]


def _has_staged_changes() -> bool:
    return bool(str(git.diff("--cached", "--stat")).strip())


def _find_closest_base_branch() -> str:
    """Return 'main' or 'master', whichever exists and has the fewest commits ahead of HEAD."""
    candidates: list[tuple[int, str]] = []
    for name in ("main", "master"):
        try:
            git("rev-parse", "--verify", f"refs/heads/{name}")
        except sh.ErrorReturnCode:
            continue
        try:
            merge_base = str(git("merge-base", "HEAD", name)).strip()
            count = count_commits_since(merge_base)
            candidates.append((count, name))
        except sh.ErrorReturnCode:
            continue
    if not candidates:
        print("error: no 'main' or 'master' branch found", file=sys.stderr)
        raise Exit()
    candidates.sort()
    return candidates[0][1]


def _resolve_rewrite_parent(
    rewrite_from: str | None,
    rewrite_branch: str | None,
) -> str | None:
    """Return the SHA to ``git reset --soft`` to, or None if no rewrite is requested."""
    if rewrite_from is not None and rewrite_branch is not None:
        print("error: --rewrite-from and --rewrite-branch are mutually exclusive", file=sys.stderr)
        raise Exit()

    if rewrite_from is not None:
        return resolve_rewrite_from(rewrite_from)

    if rewrite_branch is not None:
        target = rewrite_branch if rewrite_branch else _find_closest_base_branch()
        try:
            merge_base = str(git("merge-base", "HEAD", target)).strip()
        except sh.ErrorReturnCode as e:
            print(f"error: could not find merge-base with branch {target!r}", file=sys.stderr)
            raise Exit() from e
        return merge_base

    return None


def _show_rewrite_summary(parent_sha: str) -> None:
    """Show the oldest commit in the range and a one-line log of all commits."""
    rev_list = str(git("rev-list", f"{parent_sha}..HEAD")).strip().splitlines()
    if not rev_list:
        print("error: no commits between HEAD and the rewrite base", file=sys.stderr)
        raise Exit()
    oldest_sha = rev_list[-1]
    print(str(git("show", "--stat", oldest_sha)))
    count = len(rev_list)
    print(f"\n{count} commit(s) to be replaced:")
    print(str(git("log", "--oneline", f"{parent_sha}..HEAD")))


# ── Pre-flight helpers ───────────────────────────────────────────────────────


def _validate_resume_restart(resume: bool, restart: bool) -> None:
    """Guard: --resume and --restart cannot be used together."""
    if resume and restart:
        print("error: --resume and --restart are mutually exclusive", file=sys.stderr)
        raise typer.Exit(1)


def _exit_if_nothing_to_do(existing_base: str | None, has_staged: bool, all_changes: bool) -> None:
    """Exit early when there's no staged work and no in-progress session to continue.

    --all bypasses this check because it will stage everything from the working tree.
    """
    if existing_base is None and not has_staged and not all_changes:
        print("No staged changes and no active session — nothing to do.")
        print("Stage changes with `git add`, or pass --all/-a.")
        raise typer.Exit(0)


def _abort_session_and_restage(existing_base: str, all_changes: bool) -> None:
    """Drop all temp commits and re-add the original files to the index.

    After aborting, we put the same paths back into the staging area so the
    user doesn't lose their staged selection.  If --all was requested, there's
    no need: the slice step will call `git add -A` anyway.
    """
    files = _staged_files()
    n = abort_session(existing_base)
    suffix = "s" if n != 1 else ""
    print(f"Aborted: dropped {n} temp commit{suffix}, HEAD reset to {existing_base[:SHA_DISPLAY_LEN]}.")
    if files and not all_changes:
        git("add", "--", *files)


def _resolve_session_conflict(
    existing_base: str,
    resume: bool,
    restart: bool,
    all_changes: bool,
) -> str | None:
    """Decide what to do when there is both an active session and new staged changes.

    Three possible outcomes:
      - --restart flag  → abort the old session; return None so the caller re-slices.
      - --resume flag   → silently continue with the existing session; return it unchanged.
      - no flag         → show interactive [c]ontinue / [r]estart / [q]uit prompt.

    Returns the (possibly cleared) existing_base:
      None  → session was aborted; caller should re-slice.
      str   → session continues; caller should skip slicing.
    """
    if restart:
        # Non-interactive: just abort and let the caller re-slice.
        _abort_session_and_restage(existing_base, all_changes)
        return None

    if resume:
        # Non-interactive: skip the prompt and continue with the existing session.
        return existing_base

    # Interactive prompt: let the user decide.
    n = count_commits_since(existing_base)
    staged_count = len(_staged_files())
    commit_suffix = "s" if n != 1 else ""
    file_suffix = "s" if staged_count != 1 else ""
    print(f"Active session ({n} temp commit{commit_suffix}) with {staged_count} staged file{file_suffix}.\n")
    print("  [c] Continue — proceed to AI with existing session")
    print("  [r] Restart  — abort session and re-slice staged changes")
    print("  [q] Quit\n")
    while True:
        choice = typer.prompt("Choice [c/r/q]", default="c").strip().lower()
        if choice in ("c", "r", "q"):
            break
    if choice == "q":
        raise typer.Exit(0)
    elif choice == "r":
        _abort_session_and_restage(existing_base, all_changes)
        return None
    # "c": fall through with the existing session intact.
    return existing_base


def _handle_preflight(
    existing_base: str | None,
    resume: bool,
    restart: bool,
    all_changes: bool,
) -> str | None:
    """Run all pre-flight checks for a normal (non-rewrite) invocation.

    Returns the (possibly updated) existing_base so the caller knows whether
    to run the slice step.
    """
    _validate_resume_restart(resume, restart)
    has_staged = _has_staged_changes()
    _exit_if_nothing_to_do(existing_base, has_staged, all_changes)

    # A conflict only arises when the user has both an in-progress session and
    # freshly staged changes.  In all other cases there's nothing to resolve.
    if existing_base is not None and has_staged:
        return _resolve_session_conflict(existing_base, resume, restart, all_changes)

    return existing_base


# ── Rewrite helper ───────────────────────────────────────────────────────────


def _run_rewrite(
    rewrite_from: str | None,
    rewrite_branch: str | None,
    existing_base: str | None,
    yes: bool,
) -> None:
    """Squash an existing commit range back into the staging area.

    Uses `git reset --soft <parent>` so that all the commits in the range
    become staged changes again.  The caller is responsible for setting
    existing_base = None afterwards to force the slice step.

    Exits with an error if there is already an active git-curate session,
    because resetting HEAD would destroy the temp commits we need to recover.
    """
    if existing_base is not None:
        print(
            "error: active git-curate session already exists.\nRun `git-curate abort` to discard it first.",
            file=sys.stderr,
        )
        raise typer.Exit(1)

    parent_sha = _resolve_rewrite_parent(rewrite_from, rewrite_branch)
    assert parent_sha is not None

    _show_rewrite_summary(parent_sha)
    if not yes and not typer.confirm("\nReplace these commits?", default=False):
        raise typer.Exit(0)

    git("reset", "--soft", parent_sha)
    print(f"Reset to {parent_sha[:SHA_DISPLAY_LEN]}.\n")


# ── Slice helper ─────────────────────────────────────────────────────────────


def _slice_changes(all_changes: bool) -> None:
    """Turn staged changes into one temp commit per hunk.

    With --all, first stage everything in the working tree so that untracked
    and modified-but-unstaged files are included.
    """
    if all_changes:
        git("add", "-A")
    n = slice_hunks(paths=[])
    if n == 0:
        print(
            "Nothing to slice. Stage changes with `git add` first, or pass --all.",
            file=sys.stderr,
        )
        raise typer.Exit(1)


# ── Post-harness helper ──────────────────────────────────────────────────────


def _warn_if_session_leftover() -> None:
    """After the harness exits, warn if any temp commits were not finalized.

    Leaving temp commits behind is normal when the user pauses mid-session
    (e.g. they closed the harness early).  This just reminds them how to
    resume or clean up.
    """
    leftover_base = resolve_base()
    if leftover_base is not None:
        n = count_commits_since(leftover_base)
        suffix = "s" if n != 1 else ""
        print(
            f"\nSession still active with {n} temp commit{suffix} after harness exit.\n"
            "  Resume: git-curate --resume\n"
            "  Abort:  git-curate abort",
            file=sys.stderr,
        )


def _handle_failed_attempt(base_sha: str, yes: bool) -> bool:
    """After the harness exits, offer to reuse a failed grouping attempt.

    The harness's context is gone once it exits, but its spec is saved under
    the git dir.  Returns True if the caller should run the harness again,
    which starts from that saved spec.

    Keeps the spec without asking under --yes or without a terminal.
    """
    if resolve_base() != base_sha:
        return False  # grouping succeeded after all, or the session changed

    attempt = load_failed_attempt(base_sha)
    if attempt is None:
        return False

    # Show the tail of the failure; the full output is in the prompt of a retry.
    last_lines = "\n".join(attempt.output.strip().splitlines()[-5:])
    print(f"\nGrouping failed; the temp commits are unchanged.\n{last_lines}\n", file=sys.stderr)
    keep_msg = (
        f"Kept the spec at {attempt.path}.\n"
        "  Resume with the AI (it revises this spec): git-curate --resume\n"
        f"  Or edit it and run:                        git-curate group --spec {attempt.path} --keep-spec"
    )

    # Without a terminal there's no one to ask, so keep the spec.
    if yes or not sys.stdin.isatty():
        print(keep_msg, file=sys.stderr)
        return False

    # Ask until the answer is one of the choices.
    print("  [r] Retry   — run the AI again, starting from the saved spec")
    print("  [k] Keep    — keep the spec for later")
    print("  [d] Discard — delete the spec (temp commits stay)\n")
    while True:
        choice = typer.prompt("Choice [r/k/d]", default="r").strip().lower()
        if choice in ("r", "k", "d"):
            break

    if choice == "r":
        return True
    if choice == "k":
        print(keep_msg, file=sys.stderr)
    else:
        clear_failed_attempt()
    return False


# ── Main entry point ─────────────────────────────────────────────────────────


def curate(
    *,
    rewrite_from: str | None,
    rewrite_branch: str | None,
    yes: bool,
    harness_name: str | None,
    dry_run: bool,
    all_changes: bool,
    resume: bool = False,
    restart: bool = False,
    model: str | None = None,
) -> None:
    existing_base = resolve_base()

    # ── Step 1: Pre-flight ───────────────────────────────────────────────────
    # Validate flags, ensure there is something to do, and resolve any conflict
    # between an in-progress session and newly staged changes.
    # Skipped entirely when a rewrite was requested; rewrites handle their own
    # guard (no existing session) and bypass the staged-changes check.
    if rewrite_from is None and rewrite_branch is None:
        existing_base = _handle_preflight(existing_base, resume, restart, all_changes)

    # ── Step 2: Rewrite (optional) ───────────────────────────────────────────
    # If the user asked to squash a commit range, soft-reset HEAD to the
    # merge-base so all those commits become staged changes again.
    # We then clear existing_base to force the slice step below.
    if rewrite_from is not None or rewrite_branch is not None:
        _run_rewrite(rewrite_from, rewrite_branch, existing_base, yes)
        existing_base = None  # the staged changes from the reset must be re-sliced

    # ── Step 3: Slice ────────────────────────────────────────────────────────
    # Split staged changes into one temp commit per hunk.
    # Skipped when continuing an existing session (existing_base is not None),
    # because the temp commits from the previous run are still intact.
    if existing_base is None:
        _slice_changes(all_changes)

    # Resolve the base SHA now that slicing (if any) has completed.
    base_sha = resolve_base()
    if base_sha is None:
        print("error: no active git-curate session after slicing", file=sys.stderr)
        raise typer.Exit(1)

    # ── Step 4: Harness ──────────────────────────────────────────────────────
    # Hand off to the AI harness that groups the temp commits into logical
    # commits and writes the final commit messages.
    resolved = resolve_harness_name(harness_name)
    resolved_model = resolve_model(model)
    model_note = f", model: {resolved_model}" if resolved_model else ""

    if dry_run:
        print(f"Base: {base_sha}")
        print(f"Harness: {resolved}{model_note} (dry-run, not invoked)")
        return

    harness = get_harness(resolved, resolved_model)
    while True:
        print(f"Invoking {resolved} harness (base: {base_sha[:SHA_DISPLAY_LEN]}{model_note})…")
        harness.run(base_sha)
        if not _handle_failed_attempt(base_sha, yes):
            break

    _warn_if_session_leftover()
