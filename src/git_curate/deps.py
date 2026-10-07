"""Hunk-overlap dependency detection between temp commits.

When multiple slice sessions precede a single curate, two temp commits can
touch the same lines in the same file.  Reordering them during the group step
causes a rebase conflict.  This module detects such pairs and expresses them as
explicit ordering constraints so the AI agent and the group validator can both
act on them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .common import git, list_commits

TEMP_MSG_RE = re.compile(r"^temp: (.+):L(\d+)-(\d+) #")


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


def _verify_overlap(a_sha: str, b_file: str, b_start: int, b_end: int, base: str) -> bool:
    """Return True if commit *a_sha* also touched *b_file* lines *b_start*–*b_end*.

    Runs ``git log --format=%H -L b_start,b_end:b_file base..HEAD`` and checks
    whether *a_sha* appears in the output.  B's range is used (not A's) because
    it is relative to a state closer to HEAD, so the line numbers are more likely
    to match what git sees at HEAD.

    Falls back to ``True`` (assume dependency) on any error — binary files,
    deleted files, or git failures all get the conservative treatment.
    """
    try:
        output = str(git.log("--format=%H", f"-L{b_start},{b_end}:{b_file}", f"{base}..HEAD"))
        return bool(re.search(rf"^{re.escape(a_sha)}$", output, re.MULTILINE))
    except Exception:
        return True


def compute_dependencies(base: str) -> list[Dependency]:
    """Return ordering constraints for all temp commits in *base*..HEAD.

    For every pair of temp commits that touch overlapping line ranges in the
    same file, verifies the overlap via ``git log -L`` and, if confirmed,
    records that the chronologically earlier commit must precede the later one.

    Single-session runs with non-overlapping hunks return an empty list.
    """
    commits = list_commits(base)

    parsed: list[tuple[str, str, str, int, int]] = []  # sha, message, file, start, end
    for c in commits:
        result = parse_hunk_range(c.message)
        if result is not None:
            file_path, start, end = result
            parsed.append((c.sha, c.message, file_path, start, end))

    deps: list[Dependency] = []
    for i, (a_sha, a_msg, a_file, a_start, a_end) in enumerate(parsed):
        for _b_sha, b_msg, b_file, b_start, b_end in parsed[i + 1 :]:
            if a_file != b_file:
                continue
            if not (a_start <= b_end and b_start <= a_end):
                continue
            if _verify_overlap(a_sha, b_file, b_start, b_end, base):
                deps.append(Dependency(earlier_msg=a_msg, later_msg=b_msg))

    return deps
