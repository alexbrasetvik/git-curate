"""Authorship for the final commits of a rewrite.

Temp commits carry the Git Curate author, so `group` has to choose each final
commit's author. Fresh staged work belongs to whoever runs git-curate, and
`--reset-author` says so. A rewrite replaces commits that already have
authors, and each temp commit sliced from one names it in a Curate-Source
trailer. This module follows those trailers back to the original commits:

- The author of a group's last change becomes its author, with that
  change's author date: when the agent squashes Alice's change and Bob's
  later one, Bob is the author.

- Every other author, and every Co-authored-by trailer on the original
  commits, becomes a Co-authored-by trailer on the final commit.

Temp commits without a Curate-Source trailer, such as staged changes sliced
alongside a rewrite, count as the current user's, as do changes named
`Curate-Source: staged` by a squashed rewrite (see provenance.py).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

import sh

from .common import SHA_DISPLAY_LEN, git
from .provenance import STAGED

# Field and record separators for parsing git log output; neither can occur in
# names, emails, dates or SHAs.
_FS = "\x1f"
_RS = "\x1e"

_IDENT = re.compile(r"^(.*?) <(.*)>")


@dataclass(frozen=True)
class Ident:
    name: str
    email: str

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"

    @property
    def key(self) -> str:
        """Identity comparisons ignore the name and the email's case."""
        return self.email.lower()


@dataclass
class Authorship:
    """The author and co-authors of one final commit.

    *author* is None when the last change is the current user's uncommitted
    work; the amend then uses --reset-author, as for fresh staged work.
    """

    author: Ident | None
    date: str | None
    coauthors: list[str] = field(default_factory=list)


@dataclass
class _Source:
    author: Ident
    date: str
    coauthors: list[str]


def parse_ident(text: str) -> Ident | None:
    """Parse "Name <email>", ignoring anything after the email, such as git var's timestamp."""
    m = _IDENT.match(text.strip())
    return Ident(m.group(1), m.group(2)) if m else None


def _ident_key(text: str) -> str:
    """The Ident.key of "Name <email>", or the whole text lowercased if it isn't one."""
    ident = parse_ident(text)
    return ident.key if ident is not None else text.lower()


def current_user() -> Ident:
    ident = parse_ident(str(git("var", "GIT_AUTHOR_IDENT")))
    assert ident is not None, "git var GIT_AUTHOR_IDENT is always 'Name <email> time tz'"
    return ident


def _temp_commit_sources(shas: list[str]) -> dict[str, list[str]]:
    """Return each temp commit's Curate-Source SHAs, or `staged`, in trailer order.

    A temp commit without a Curate-Source trailer maps to an empty list.
    """
    out = str(
        git.log(
            "--no-walk=unsorted",
            f"--format={_RS}%H{_FS}%(trailers:key=Curate-Source,valueonly,separator=%x1f)",
            *shas,
        )
    )
    result: dict[str, list[str]] = {}
    for record in out.split(_RS)[1:]:
        sha, *values = record.rstrip("\n").split(_FS)
        result[sha] = [v.split()[0] for v in values if v.strip()]
    return result


def _load_sources(shas: list[str]) -> dict[str, _Source]:
    """Read the author, author date and Co-authored-by trailers of each original commit, by *shas* as given.

    An original commit that no longer resolves, for example after gc, is left
    out with a warning, and its changes count as the current user's.
    """
    fmt = f"--format={_RS}%an{_FS}%ae{_FS}%aI{_FS}%(trailers:key=Co-authored-by,valueonly,separator=%x1f)"
    try:
        # One call for all of them; the output is in the order given.
        records = str(git.log("--no-walk=unsorted", fmt, *shas, "--")).split(_RS)[1:]
        found = dict(zip(shas, records, strict=True))
    except (sh.ErrorReturnCode, ValueError):
        found = {}
        for sha in shas:
            try:
                found[sha] = str(git.log("-1", fmt, f"{sha}^{{commit}}", "--")).split(_RS)[1]
            except sh.ErrorReturnCode:
                print(
                    f"warning: original commit {sha[:SHA_DISPLAY_LEN]} not found; using your identity",
                    file=sys.stderr,
                )

    sources: dict[str, _Source] = {}
    for sha, record in found.items():
        name, email, date, *coauthors = record.rstrip("\n").split(_FS)
        sources[sha] = _Source(Ident(name, email), date, [c.strip() for c in coauthors if c.strip()])
    return sources


def plan_authorship(groups: list[list[str]]) -> list[Authorship | None]:
    """Choose the authorship of each final commit, squashed from the temp commits in each of *groups*.

    Each group's temp commits must be in their original order. A group none
    of whose temp commits came from an existing commit gets None, so the
    final commit is the current user's alone.
    """
    temps = _temp_commit_sources([sha for shas in groups for sha in shas])
    source_shas = list(dict.fromkeys(s for values in temps.values() for s in values if s != STAGED))
    if not source_shas:
        return [None for _ in groups]
    sources = _load_sources(source_shas)
    me = current_user()
    return [_authorship([s for sha in shas for s in temps.get(sha) or [STAGED]], sources, me) for shas in groups]


def _authorship(changes: list[str], sources: dict[str, _Source], me: Ident) -> Authorship | None:
    """Choose the authorship of a final commit made of *changes*, in order.

    Each change is an original commit's SHA or `staged`. The last change's
    author becomes the author, and every other author a co-author.
    """
    if all(c == STAGED for c in changes):
        return None

    # Each change's source, or None for the current user's own changes.
    found = [sources.get(c) for c in changes]
    last = found[-1]

    idents = [src.author if src is not None else me for src in found]
    candidates = [str(i) for i in idents]
    candidates += [c for src in found if src is not None for c in src.coauthors]
    coauthors: list[str] = []
    seen = {idents[-1].key}
    for c in candidates:
        key = _ident_key(c)
        if key not in seen:
            seen.add(key)
            coauthors.append(c)

    if last is None:
        return Authorship(author=None, date=None, coauthors=coauthors)
    return Authorship(author=last.author, date=last.date, coauthors=coauthors)


def add_coauthor_trailers(message: str, coauthors: list[str]) -> str:
    """Append a Co-authored-by trailer to *message* for each of *coauthors* it doesn't already name."""
    named = {_ident_key(value) for key, value in _trailers(message) if key.lower() == "co-authored-by"}
    new = [c for c in coauthors if _ident_key(c) not in named]
    if not new:
        return message
    args = [a for c in new for a in ("--trailer", f"Co-authored-by: {c}")]
    return str(git("interpret-trailers", *args, _in=message))


def _trailers(message: str) -> list[tuple[str, str]]:
    out = str(git("interpret-trailers", "--parse", _in=message))
    pairs = []
    for line in out.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            pairs.append((key.strip(), value.strip()))
    return pairs
