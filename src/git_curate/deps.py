"""Hunk-overlap dependency detection between temp commits.

When multiple slice sessions precede a single curate, two temp commits can
touch the same lines in the same file.  Reordering them during the group step
causes a rebase conflict.  This module detects such pairs and expresses them as
explicit ordering constraints so the AI agent and the group validator can both
act on them.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

import sh

from .common import git, list_commits

TEMP_MSG_RE = re.compile(r"^temp: (.+):L(\d+)-(\d+) #")

# A commit line in `git log --format=%H -L` output: a SHA-1 or SHA-256.
SHA_LINE_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.MULTILINE)


@dataclass
class Dependency:
    """*earlier_msg* must appear before *later_msg* in the final rebase order."""

    earlier_msg: str
    later_msg: str


def parse_hunk_range(message: str) -> tuple[str, int, int] | None:
    """Return ``(file_path, start_line, end_line)`` from a temp commit message.

    Returns ``None`` for non-temp commits (e.g. real commits in the range when
    an explicit base is given).
    """
    m = TEMP_MSG_RE.match(message)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def _commits_touching(file: str, start: int, end: int, base: str) -> set[str] | None:
    """Return the commits in *base*..HEAD that touched *file* lines *start*–*end*, or None if git can't tell.

    Runs ``git log --format=%H -L start,end:file base..HEAD``.  The range is
    the later commit's, not the earlier one's, because it is relative to a
    state closer to HEAD, so the line numbers are more likely to match what
    git sees at HEAD.

    git fails on files that are binary or gone at HEAD; then the caller
    assumes the overlap is real, with a warning.
    """
    try:
        # The patch in the output can be in any encoding; only the SHAs matter.
        output = str(git.log("--format=%H", f"-L{start},{end}:{file}", f"{base}..HEAD", _decode_errors="replace"))
    except sh.ErrorReturnCode as e:
        reason = e.stderr.decode(errors="replace").strip()
        print(
            f"warning: cannot trace {file}:L{start}-{end} ({reason});"
            " assuming it depends on the earlier commits that overlap it",
            file=sys.stderr,
        )
        return None
    # Only the --format lines are bare SHAs; the rest is the patch.
    return set(SHA_LINE_RE.findall(output))


def compute_dependencies(base: str) -> list[Dependency]:
    """Return ordering constraints for all temp commits in *base*..HEAD.

    For every pair of temp commits that touch overlapping line ranges in the
    same file, verifies the overlap via ``git log -L`` and, if confirmed,
    records that the chronologically earlier commit must precede the later one.
    ``git log -L`` runs once per later commit, not once per pair.

    Single-session runs with non-overlapping hunks return an empty list.
    """
    commits = list_commits(base)

    parsed: list[tuple[str, str, str, int, int]] = []  # sha, message, file, start, end
    for c in commits:
        result = parse_hunk_range(c.message)
        if result is not None:
            file_path, start, end = result
            parsed.append((c.sha, c.message, file_path, start, end))

    pairs: list[tuple[int, int]] = []
    for j, (_b_sha, _b_msg, b_file, b_start, b_end) in enumerate(parsed):
        earlier = [
            i
            for i, (_a_sha, _a_msg, a_file, a_start, a_end) in enumerate(parsed[:j])
            if a_file == b_file and a_start <= b_end and b_start <= a_end
        ]
        if not earlier:
            continue
        touched = _commits_touching(b_file, b_start, b_end, base)
        pairs += [(i, j) for i in earlier if touched is None or parsed[i][0] in touched]

    return [Dependency(earlier_msg=parsed[i][1], later_msg=parsed[j][1]) for i, j in sorted(pairs)]
