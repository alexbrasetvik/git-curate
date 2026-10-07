"""Shared utilities for the git-curate subcommands."""

from __future__ import annotations

import functools
import os
import shutil
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import sh
import typer
from typer.main import CommandFunctionType


class SubApp(typer.Typer):
    """Typer sub-app pre-configured for use with app.add_typer().

    Two quirks are baked in so individual modules don't need to repeat them:

    context_settings={"allow_interspersed_args": True}
        Click groups default allow_interspersed_args to False, so options that
        follow a positional Argument (e.g. `group <base> --spec file.json`) get
        misread as subcommand names.  Opting back in fixes that.

    callback default invoke_without_command=True
        Each sub-app has exactly one entry point.  This default makes Typer call
        the callback when the group name is typed (e.g. `git-curate slice`)
        without requiring a further subcommand name.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(context_settings={"allow_interspersed_args": True}, **kwargs)

    def callback(
        self, *args: Any, invoke_without_command: bool = True, **kwargs: Any
    ) -> Callable[[CommandFunctionType], CommandFunctionType]:
        parent_decorator = super().callback(*args, invoke_without_command=invoke_without_command, **kwargs)

        def decorator(fn: CommandFunctionType) -> CommandFunctionType:
            @functools.wraps(fn)
            def wrapper(*fn_args: Any, **fn_kwargs: Any) -> Any:
                pre_checks()
                return fn(*fn_args, **fn_kwargs)

            return parent_decorator(wrapper)  # type: ignore[return-value]

        return decorator


class EnvOverlay(Mapping[str, str]):
    """os.environ with fixed overrides on top, read when a command runs.

    sh's _env replaces the whole environment, so each bake needs the full
    set. Reading os.environ lazily (rather than copying it at import) keeps
    later changes to it, such as monkeypatch.setenv in tests, visible to git.
    """

    def __init__(self, overrides: Mapping[str, str]) -> None:
        self.overrides = dict(overrides)

    def __getitem__(self, key: str) -> str:
        if key in self.overrides:
            return self.overrides[key]
        return os.environ[key]

    def __iter__(self) -> Iterator[str]:
        return iter({**os.environ, **self.overrides})

    def __len__(self) -> int:
        return len(os.environ.keys() | self.overrides.keys())


git = sh.git.bake(
    # We care about diff quality over speed:
    "-c",
    "diff.algorithm=patience",
    # Guard settings the user's config could override:
    "--no-pager",
    "-c",
    "color.ui=false",
    "-c",
    "diff.noprefix=false",
    "-c",
    "diff.mnemonicPrefix=false",
    "-c",
    "apply.whitespace=nowarn",
    "-c",
    "log.showSignature=false",
    "-c",
    "rebase.autosquash=false",
    "-c",
    "rebase.backend=merge",
    # Reordering temp commits makes each pick's merge base (its original
    # parent) differ wildly from HEAD, so directory-rename heuristics see
    # phantom renames and stop with "implicit dir rename" conflicts.
    "-c",
    "merge.directoryRenames=false",
    "-c",
    "commit.gpgSign=false",
    _tty_out=False,
)

# We identify our commits based on author, and not e.g. git commit message prefixes
# which could be brittle:
CURATE_AUTHOR_NAME = "Git Curate"
CURATE_AUTHOR_EMAIL = "git-curate@local"
SHA_DISPLAY_LEN = 12

# Like git but with author identity set via env vars.
# git reads GIT_AUTHOR_* from the environment, not from -c flags.
# slice.py uses this for the author ident of temp commits.
curate_git = git.bake(
    _env={
        **os.environ,
        "GIT_AUTHOR_NAME": CURATE_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": CURATE_AUTHOR_EMAIL,
    }
)


def find_slice_base() -> str:
    """Walk HEAD backward and return the SHA of the first non-curate commit.

    That commit is the session base: it was HEAD before slice started, so
    git log base..HEAD covers exactly the temp commits.

    Falls back to the root commit when every commit is curate-authored.
    """
    # %H = full SHA, %ae = author email; one "sha email" line per commit, newest first
    for line in str(git.log("--format=%H %ae", "HEAD")).strip().splitlines():
        sha, email = line.split(" ", 1)
        if email.strip() != CURATE_AUTHOR_EMAIL:
            return sha
    # --max-parents=0 selects the root commit (no parents); fallback when every commit is ours
    return str(git("rev-list", "--max-parents=0", "HEAD")).strip()


def resolve_rewrite_from(from_ref: str) -> str:
    """Validate *from_ref* as an ancestor of HEAD and return its parent SHA.

    Used by ``slice --from`` and the top-level rewrite flow.  The caller is
    responsible for the actual ``git reset --soft <parent>``.
    """
    try:
        from_sha = str(git("rev-parse", "--verify", from_ref)).strip()
    except sh.ErrorReturnCode as e:
        print(f"fatal: not a valid commit: {from_ref!r}", file=sys.stderr)
        raise InvalidRefError() from e
    try:
        git("merge-base", "--is-ancestor", from_sha, "HEAD")
    except sh.ErrorReturnCode as e:
        print(
            f"error: {from_ref!r} is not an ancestor of HEAD.\nThe commit must be reachable from the current branch.",
            file=sys.stderr,
        )
        raise NotAncestorError() from e
    try:
        parent_sha = str(git("rev-parse", f"{from_sha}^")).strip()
    except sh.ErrorReturnCode as e:
        print(
            f"error: {from_ref!r} has no parent. Cannot rewrite from the root commit.",
            file=sys.stderr,
        )
        raise RootCommitError() from e
    return parent_sha


def count_commits_since(base: str) -> int:
    """Return the number of commits between *base* and HEAD."""
    return int(str(git("rev-list", "--count", f"{base}..HEAD")).strip())


@dataclass
class Commit:
    sha: str
    message: str


def list_commits(base: str) -> list[Commit]:
    """Return commits from base..HEAD, oldest first."""
    log = str(git.log("--reverse", "--format=%H %s", f"{base}..HEAD")).strip()
    if not log:
        return []
    commits = []
    for line in log.splitlines():
        sha, message = line.split(" ", 1)
        commits.append(Commit(sha=sha, message=message))
    return commits


def abort_session(base_sha: str) -> int:
    """Reset HEAD to *base_sha* and return the number of dropped commits."""
    n = count_commits_since(base_sha)
    git("reset", "--mixed", base_sha)
    # A saved spec names the temp commits just dropped, so it can't be reused.
    clear_failed_attempt()
    return n


# ---------------------------------------------------------------------------
# Failed group attempts
# ---------------------------------------------------------------------------
#
# When `group` fails, the spec is the expensive part: an agent spent a whole
# session reasoning it out. Keep it (and why it failed) under the git dir so a
# later harness run can revise it instead of regrouping from scratch.


@dataclass
class FailedAttempt:
    spec_text: str
    output: str
    path: str  # where spec.json lives, for editing and re-running by hand


def _failed_attempt_dir() -> str:
    return str(git("rev-parse", "--git-path", "git-curate-failed")).strip()


def save_failed_attempt(base: str, spec_text: str, output: str) -> str:
    """Record a failed group attempt for *base*; return the saved spec path."""
    d = _failed_attempt_dir()
    os.makedirs(d, exist_ok=True)
    for name, content in (("base", base), ("spec.json", spec_text), ("error.txt", output)):
        with open(os.path.join(d, name), "w") as f:
            f.write(content)
    return os.path.abspath(os.path.join(d, "spec.json"))


def load_failed_attempt(base: str) -> FailedAttempt | None:
    """Return the failed attempt saved for *base*, or None.

    A record saved for a different base belongs to an older session whose
    temp commits no longer exist, so it is deleted.
    """
    d = _failed_attempt_dir()
    try:
        with open(os.path.join(d, "base")) as f:
            saved_base = f.read().strip()
        with open(os.path.join(d, "spec.json")) as f:
            spec_text = f.read()
        with open(os.path.join(d, "error.txt")) as f:
            output = f.read()
    except OSError:
        return None  # nothing saved

    if saved_base != base:
        clear_failed_attempt()
        return None
    return FailedAttempt(spec_text=spec_text, output=output, path=os.path.abspath(os.path.join(d, "spec.json")))


def clear_failed_attempt() -> None:
    shutil.rmtree(_failed_attempt_dir(), ignore_errors=True)


class Exit(SystemExit):
    def __init__(self, code: int = 1) -> None:
        super().__init__(code)


class NotInGitRepoError(Exit):
    pass


class RebaseInProgressError(Exit):
    pass


class InvalidRefError(Exit):
    pass


class NotAncestorError(Exit):
    pass


class RootCommitError(Exit):
    pass


class NoSessionError(Exit):
    pass


class InvalidSpecError(Exit):
    pass


class SliceError(Exit):
    """The staged diff can't be sliced: something unsupported, or a result that doesn't match the index."""

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.reason = reason

    def __str__(self) -> str:
        return self.reason


class RebaseFailedError(Exit):
    def __init__(self, output: str = "") -> None:
        super().__init__()
        self.output = output


class UnknownHarnessError(Exit):
    pass


class CLINotFoundError(Exit):
    pass


class ClaudeError(Exit):
    pass


def ensure_in_git_repo() -> None:
    try:
        git("rev-parse", "--is-inside-work-tree")
    except sh.ErrorReturnCode as e:
        print("fatal: not inside a git repository", file=sys.stderr)
        raise NotInGitRepoError() from e


def pre_checks() -> None:
    ensure_in_git_repo()
    check_no_rebase_in_progress()


def rebase_in_progress(cwd: str | None = None) -> bool:
    """Return True if a rebase is in progress in the repo at *cwd*."""
    git_dir = str(git("rev-parse", "--absolute-git-dir", _cwd=cwd)).strip()
    return any(os.path.isdir(os.path.join(git_dir, d)) for d in ("rebase-merge", "rebase-apply"))


def check_no_rebase_in_progress() -> None:
    """Raise RebaseInProgressError if a rebase is already in progress."""
    if rebase_in_progress():
        print(
            "Error: a rebase is already in progress.\n\n"
            "Resolve it first:\n"
            "  git rebase --continue   # after fixing conflicts\n"
            "  git rebase --abort      # to cancel it entirely",
            file=sys.stderr,
        )
        raise RebaseInProgressError()


def resolve_base() -> str | None:
    """Return the session base SHA, or None if no active session.

    A session is active when HEAD carries a git-curate@local author — slice
    has run but group hasn't yet.
    """
    try:
        head_email = str(git.log("-1", "--format=%ae")).strip()
    except sh.ErrorReturnCode:
        return None
    if head_email != CURATE_AUTHOR_EMAIL:
        return None
    return find_slice_base()
