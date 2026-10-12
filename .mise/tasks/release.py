#!/usr/bin/env -S uv run --script
# MISE description="Tag the version in pyproject.toml and create its GitHub release"
# MISE raw=true
#
# /// script
# requires-python = ">=3.11"
# dependencies = ["sh"]
# ///
"""Tag the version in pyproject.toml and create its GitHub release.

Releases are source-only: the tag and a GitHub Release with no attached files,
which mise's pypi backend lists as an installable version. The tag is the bare
version (0.4.0, not v0.4.0) so it matches pyproject.toml and `git-curate --version`.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path
from typing import NoReturn

import sh

git = sh.git.bake("--no-pager", "-c", "color.ui=false", _tty_out=False)
gh = sh.gh.bake(_tty_out=False)


def die(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def ci_result(sha: str) -> str:
    """The latest CI run's conclusion for sha, its status while it runs, or "missing"."""
    runs = json.loads(str(gh.run.list(commit=sha, workflow="ci.yml", json="status,conclusion")))
    if not runs:
        return "missing"
    run = runs[0]
    return str(run["conclusion"] if run["status"] == "completed" else run["status"])


def main() -> None:
    root = Path(str(git("rev-parse", "--show-toplevel")).strip())
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        die(f"pyproject.toml version {version!r} is not X.Y.Z")

    if not sys.stdin.isatty():
        die("releasing needs an interactive terminal to confirm")
    try:
        gh.auth.status()
    except sh.ErrorReturnCode:
        die("gh is not authenticated; run 'gh auth login'")

    try:
        branch = str(git("symbolic-ref", "--short", "HEAD")).strip()
    except sh.ErrorReturnCode:
        die("HEAD is detached")
    if branch != "main":
        die(f"on branch {branch!r}, not main")
    if str(git.status("--porcelain")).strip():
        die("working tree has uncommitted or untracked changes")

    git.fetch("--quiet", "--tags", "origin", "main")
    sha = str(git("rev-parse", "HEAD")).strip()
    if sha != str(git("rev-parse", "origin/main")).strip():
        die("main is not at origin/main; push or pull first")

    if str(git("tag", "--list", version)).strip():
        die(f"tag {version} already exists; bump the version in pyproject.toml")

    # The release must point at a commit CI has passed on.
    ci = ci_result(sha)
    if ci == "missing":
        die(f"no CI run found for {sha}")
    if ci != "success":
        die(f"CI for {sha} is {ci!r}, not success")

    print(f"Release {version} from main at {str(git.log('-1', '--format=%h %s')).strip()}")
    print("CI passed on this commit.")
    print()
    try:
        previous = str(git.describe("--tags", "--abbrev=0")).strip()
    except sh.ErrorReturnCode:
        print("No earlier tag; this is the first release.")
    else:
        print(f"Commits since {previous}:")
        print(git.log("--oneline", f"{previous}..HEAD"), end="")
    print()

    try:
        answer = input(f"Tag {version}, push the tag, and create the GitHub release? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() != "y":
        die("aborted")

    git.tag("-a", version, "-m", f"git-curate {version}")
    git.push("origin", f"refs/tags/{version}", _fg=True)
    gh.release.create(version, "--verify-tag", "--title", version, "--generate-notes", _fg=True)


if __name__ == "__main__":
    main()
