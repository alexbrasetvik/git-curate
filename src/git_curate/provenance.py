"""Which original commits a squashed rewrite's hunks came from.

`--squash-first` soft-resets a range of commits into one staged diff, which
by itself no longer says who wrote what. Before the reset, the range is still
known, so slicing can follow each changed line back to its commit and name
it in a Curate-Source trailer, as slicing each commit on its own does. The
authors module then turns those trailers into the final commits' authors.

Fast path: when one author wrote every commit in the range, without
co-authors, and the index holds nothing beyond the range, every hunk is
credited to the newest commit, without running blame.

Otherwise each hunk's lines are blamed over base..old_head:

- An added line is credited to the commit that wrote it (`git blame`).

- A deleted line is credited to the commit that deleted it: `git blame
  --reverse` names the last commit that still had it, and the deleter is the
  next one in first-parent order.

Only the last version of a line survives the squash, so a line that Alice
added and Bob then changed is credited to Bob for the added side and to
Alice for the deleted one; with no deleted side, Alice drops out. Lines
staged beyond the range are credited to `staged`, meaning the current user.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import sh

from .common import SHA_DISPLAY_LEN, git

if TYPE_CHECKING:
    from .slice import Hunk

# The Curate-Source value for lines that are only staged, not committed.
STAGED = "staged"

# The commit git blame gives a line that is not committed yet.
_NOT_COMMITTED = "0" * 40

# A porcelain blame line naming a line's commit: "<sha> <orig line> <final line> [<group size>]".
_BLAME_LINE = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)")


@dataclass(frozen=True)
class SquashSource:
    """The range a --squash-first reset is about to collapse into the index."""

    base: str
    old_head: str

    @classmethod
    def before_reset(cls, base: str) -> SquashSource:
        """Record base..HEAD; call this before `git reset --soft <base>`."""
        return cls(str(git("rev-parse", base)).strip(), str(git("rev-parse", "HEAD")).strip())


def source_trailers(hunks: list[Hunk], src: SquashSource) -> list[list[str]]:
    """Return the Curate-Source trailer lines for each hunk of the squashed diff.

    Each hunk's sources are listed in the range's order, with `staged` last.
    """
    commits = str(git("rev-list", "--reverse", "--first-parent", f"{src.base}..{src.old_head}")).split()
    if not commits:
        return [[] for _ in hunks]

    staged_extra = _index_differs(src.old_head)
    if not staged_extra and _single_author(src):
        sources: list[set[str]] = [{src.old_head} for _ in hunks]
    else:
        sources = _blame_hunks(hunks, src, commits, staged_extra)

    subjects = _subjects(src)
    position = {sha: n for n, sha in enumerate(commits)}

    def order(source: str) -> int:
        return position.get(source, len(commits))  # staged sorts last

    trailers: list[list[str]] = []
    for found in sources:
        lines = []
        for source in sorted(found, key=order):
            value = STAGED if source == STAGED else f"{source[:SHA_DISPLAY_LEN]} {subjects.get(source, '')}".rstrip()
            lines.append(f"Curate-Source: {value}")
        trailers.append(lines)
    return trailers


# ---------------------------------------------------------------------------
# Fast path
# ---------------------------------------------------------------------------


def _index_differs(rev: str) -> bool:
    """Return True if the index holds changes beyond *rev*."""
    try:
        git("diff", "--cached", "--quiet", rev)
    except sh.ErrorReturnCode:
        return True
    return False


def _single_author(src: SquashSource) -> bool:
    """Return True if one author, without co-authors, wrote every commit in the range."""
    out = str(git.log("--format=%ae%x1f%(trailers:key=Co-authored-by,valueonly)", f"{src.base}..{src.old_head}"))
    emails = set()
    for line in out.splitlines():
        email, _, coauthors = line.partition("\x1f")
        if coauthors.strip():
            return False
        if email:
            emails.add(email.lower())
    return len(emails) <= 1


def _subjects(src: SquashSource) -> dict[str, str]:
    out = str(git.log("--format=%H %s", f"{src.base}..{src.old_head}"))
    subjects: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, subject = line.partition(" ")
        subjects[sha] = subject
    return subjects


# ---------------------------------------------------------------------------
# Deep path
# ---------------------------------------------------------------------------


@dataclass
class _FileLines:
    """The changed lines of one file across its hunks, by hunk index."""

    old_path: str | None
    new_path: str | None
    added: dict[int, int]  # staged-file line number -> hunk index
    deleted: dict[int, int]  # base-file line number -> hunk index


def _changed_lines(hunks: list[Hunk]) -> list[_FileLines]:
    """Find every hunk's added and deleted lines, numbered in the staged and base files.

    New-side starts already count from the staged file. Old-side starts count
    from the file with the earlier hunks applied, so subtracting the net lines
    those hunks added gives the base file's numbers.
    """
    from .slice import parse_hunk_header

    files: list[_FileLines] = []
    current: tuple[str | None, str | None] | None = None
    net = 0
    for index, hunk in enumerate(hunks):
        key = (hunk.file.old_path, hunk.file.new_path)
        if key != current:
            current = key
            net = 0
            files.append(_FileLines(hunk.file.old_path, hunk.file.new_path, {}, {}))
        old_start, _, new_start, _, _ = parse_hunk_header(hunk.lines[0])
        old_line, new_line = old_start - net, new_start
        for line in hunk.lines[1:]:
            if line.startswith("\\"):
                continue  # "\ No newline at end of file"
            if line.startswith("-"):
                files[-1].deleted[old_line] = index
                old_line += 1
                net -= 1
            elif line.startswith("+"):
                files[-1].added[new_line] = index
                new_line += 1
                net += 1
            else:  # context, with or without its leading space
                old_line += 1
                new_line += 1
    return files


def _blame_hunks(hunks: list[Hunk], src: SquashSource, commits: list[str], staged_extra: bool) -> list[set[str]]:
    """Return the commits (or `staged`) each hunk's lines came from, by blaming base..old_head."""
    sources: list[set[str]] = [set() for _ in hunks]
    in_range = set(commits)
    nxt = dict(zip(commits, [*commits[1:], STAGED], strict=True))
    nxt[src.base] = commits[0]

    for f in _changed_lines(hunks):
        try:
            if f.added:
                assert f.new_path is not None
                for line, sha in _blame(f.new_path, f.added, src, reverse=False, contents=staged_extra):
                    if sha == _NOT_COMMITTED:
                        sources[f.added[line]].add(STAGED)
                    elif sha in in_range:
                        sources[f.added[line]].add(sha)
            if f.deleted:
                assert f.old_path is not None
                for line, sha in _blame(f.old_path, f.deleted, src, reverse=True, contents=False):
                    # The deleter is the commit after the last one that had the line.
                    if sha in nxt:
                        sources[f.deleted[line]].add(nxt[sha])
        except sh.ErrorReturnCode:
            pass  # filled in below from the commits that touched the file

        # A hunk blame couldn't place gets every commit that touched its file.
        hunk_indexes = {*f.added.values(), *f.deleted.values()}
        if any(not sources[i] for i in hunk_indexes):
            touching = _touching(src, [p for p in (f.old_path, f.new_path) if p is not None], in_range)
            for i in hunk_indexes:
                if not sources[i]:
                    sources[i].update(touching or {STAGED})
    return sources


def _blame(
    path: str, lines: Iterable[int], src: SquashSource, *, reverse: bool, contents: bool
) -> Iterable[tuple[int, str]]:
    """Yield (line, commit) for *lines* of *path*, blamed over base..old_head.

    With *reverse*, the lines are the base file's and the commit is the last
    one that still had each line. With *contents*, the lines are the staged
    file's, which has changes beyond old_head.
    """
    args = ["blame", "--porcelain", *_line_ranges(lines)]
    stdin = None
    if reverse:
        args.append("--reverse")
    if contents:
        args += ["--contents", "-"]
        stdin = git("cat-file", "blob", f":{path}", _return_cmd=True).stdout
    args += [f"{src.base}..{src.old_head}", "--", path]
    out = git(*args, _in=stdin) if stdin is not None else git(*args)
    for row in str(out).splitlines():
        m = _BLAME_LINE.match(row)
        if m:
            yield int(m.group(2)), m.group(1)


def _line_ranges(lines: Iterable[int]) -> list[str]:
    """Collapse line numbers into git blame -L arguments."""
    args: list[str] = []
    start = end = None
    for n in sorted(lines):
        if end is not None and n == end + 1:
            end = n
            continue
        if start is not None:
            args += ["-L", f"{start},{end}"]
        start = end = n
    if start is not None:
        args += ["-L", f"{start},{end}"]
    return args


def _touching(src: SquashSource, paths: list[str], in_range: set[str]) -> set[str]:
    out = str(git.log("--format=%H", f"{src.base}..{src.old_head}", "--", *paths))
    return {sha for sha in out.split() if sha in in_range}
