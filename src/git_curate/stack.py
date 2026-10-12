"""
Stacks — split a tidy branch into GitHub stacked-PR branches
============================================================

Backs the git-curate-stack skill. The branch's commits are never edited, only
reordered or redistributed into one or more stacks, each based on trunk. Everything happens in the object database with
``git merge-tree`` and ``git hash-object``: HEAD, the current branch and the
working tree are never touched, and ``apply`` only creates new branch refs.

All output is JSON on stdout, for the agent.

Spec format (JSON file):
------------------------
    {
        "trunk": "main",
        "stacks": [
            {"layers": [
                {"name": "billing/schema", "commits": ["a1b2c3d", "e4f5a6b"]},
                {"name": "billing/api", "commits": ["c7d8e9f"]}
            ]},
            {"layers": [
                {"name": "fix/typo", "commits": ["0a1b2c3"]}
            ]}
        ]
    }

A stack is a straight chain, never a tree. Its layers are listed bottom to
top: the first is based on trunk and each later one on the layer before it.
Each layer becomes one branch, named by "name", and one PR against the layer
below. The example gives two stacks, and so two PRs against main:

    main <- billing/schema <- billing/api
    main <- fix/typo

billing/api builds on billing/schema; fix/typo depends on neither.

Commits are SHA prefixes from ``analyze``. Every commit in trunk..HEAD must
appear exactly once. A layer's commits are listed in the order they apply.

Algorithm:
----------
Replaying commit C onto X is a three-way merge with C's parent as the base:
``git merge-tree --write-tree --merge-base=C^ X C``. It succeeds exactly when
cherry-picking C onto X would apply without conflicts. Older git (2.43
included) merges only commits, not trees, so a merged tree that is merged
again is first wrapped in a throwaway commit.

``analyze`` finds, for each commit i, the later commits that can't be
replayed without it: it rebuilds the chain from i's parent leaving i out, and
any later commit that conflicts requires i and is left out too, so whatever
needs that commit registers as needing i as well.

That is one chain per commit, so up to n²/2 merges. Two things keep it cheap.
The chains advance together, one commit at a time, so each step's merges run
in one ``git merge-tree --stdin``: at most 2n processes, not n²/2. And a chain
only merges a commit whose paths overlap what the chain left out; elsewhere
the chain matches the original history, so the merge can't conflict. Skipped
commits are picked up by the chain's next merge, whose base is the original
tree the chain last caught up with, or by a catch-up merge when that next
commit conflicts and is dropped.

``check`` builds each stack bottom to top and merges the stack tops together;
the result must equal HEAD's tree, so nothing is lost in the split.

Usage:
------
    uvx git-curate stack analyze [--trunk main]
    uvx git-curate stack check [--spec <path>]
    uvx git-curate stack apply [--spec <path>]

The spec defaults to git-curate-stack-spec.json in the git dir, the path
``git rev-parse --git-path git-curate-stack-spec.json`` prints: usually
.git/git-curate-stack-spec.json, but per worktree in a linked one.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from typing import Annotated, Any, NoReturn

import sh
import typer

from .common import (
    SHA_DISPLAY_LEN,
    Exit,
    git,
    list_commits,
    pre_checks,
    resolve_branch_base,
)

DEFAULT_SPEC_NAME = "git-curate-stack-spec.json"

app = typer.Typer(no_args_is_help=True)


@app.callback()
def _checks() -> None:
    """Split a tidy branch into stacked-PR branches without touching the working tree."""
    pre_checks()


class StackFailedError(Exit):
    """The JSON report on stdout says why."""


# ---------------------------------------------------------------------------
# Replaying commits
# ---------------------------------------------------------------------------


@dataclass
class Conflict:
    paths: list[str]


def merge_trees(base: str, ours: str, theirs: str) -> str | Conflict:
    """Three-way merge commits in the object database; return the tree or the conflicted paths."""
    result = git(
        "merge-tree",
        "--write-tree",
        "--name-only",
        "--no-messages",
        f"--merge-base={base}",
        ours,
        theirs,
        _ok_code=[0, 1],
        _return_cmd=True,
    )
    # The first line is the tree; on conflict the conflicted paths follow.
    lines: list[str] = result.stdout.decode().strip().splitlines()
    if result.exit_code == 1:
        return Conflict(paths=lines[1:])
    return lines[0]


def merge_batch(merges: list[tuple[str, str, str]]) -> list[str | Conflict]:
    """Like merge_trees for each (base, ours, theirs), in one git process."""
    if not merges:
        return []
    out = str(
        git(
            "merge-tree",
            "--stdin",
            "--write-tree",
            "--name-only",
            "--no-messages",
            _in="".join(f"{base} -- {ours} {theirs}\n" for base, ours, theirs in merges),
        )
    )
    # Each merge prints NUL-terminated fields: a status (1 clean, 0 conflict),
    # the tree, any conflicted paths, then an empty field.
    fields = iter(out.split("\0"))
    results: list[str | Conflict] = []
    for _ in merges:
        status, tree = next(fields), next(fields)
        paths = list(iter(fields.__next__, ""))
        results.append(tree if status == "1" else Conflict(paths=paths))
    return results


# The identity of commit_trees' commits; fixed, so a tree always gets the same commit.
_WRAP_IDENT = "git-curate <git-curate@local> 0 +0000"


def commit_trees(trees: list[str]) -> list[str]:
    """Wrap each of *trees* in a parentless commit, for merging it again; in one git process."""
    if not trees:
        return []
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for n, tree in enumerate(trees):
            path = os.path.join(d, str(n))
            with open(path, "w") as f:
                f.write(f"tree {tree}\nauthor {_WRAP_IDENT}\ncommitter {_WRAP_IDENT}\n\n")
            paths.append(path)
        out = str(git("hash-object", "-w", "-t", "commit", "--stdin-paths", _in="".join(f"{p}\n" for p in paths)))
    return out.split()


def _parent(sha: str) -> str:
    return str(git("rev-parse", f"{sha}^")).strip()


def _tree(rev: str) -> str:
    return str(git("rev-parse", f"{rev}^{{tree}}")).strip()


def _files(sha: str) -> list[str]:
    out = str(git("diff-tree", "--no-commit-id", "--name-only", "-r", sha)).strip()
    return out.splitlines() if out else []


# "Name <email> 1700000000 +0100", the value of a raw commit's author or committer header.
_IDENT_HEADER = re.compile(rb"^(.*) <(.*)> (\d+ [+-]\d{4})$")


def _read_commit(sha: str) -> tuple[dict[bytes, bytes], bytes]:
    """Return the raw commit object's headers, first of each, and its message, as bytes."""
    raw: bytes = git("cat-file", "commit", sha, _return_cmd=True).stdout
    header, _, message = raw.partition(b"\n\n")
    headers: dict[bytes, bytes] = {}
    for line in header.split(b"\n"):
        if line.startswith(b" "):
            continue  # the continuation of a multi-line header, such as gpgsig
        key, _, value = line.partition(b" ")
        headers.setdefault(key, value)
    return headers, message


def replay(sha: str, onto: str) -> str | Conflict:
    """Return a commit like *sha* but on top of commit *onto*, or the conflict that prevents it.

    The new commit keeps the message, author and committer date, so replaying
    the same plan twice gives the same SHAs. The committer is whoever runs it.
    """
    parent = _parent(sha)
    if parent == onto:
        return sha  # already in place; keep the original
    tree = merge_trees(parent, onto, sha)
    if isinstance(tree, Conflict):
        return tree

    # The new object copies the author, any encoding header and the message
    # byte for byte. commit-tree can't: it re-encodes text that isn't valid
    # UTF-8 and has no encoding header, as if it were Latin-1.
    headers, message = _read_commit(sha)
    committer = _IDENT_HEADER.match(headers[b"committer"])
    me = _IDENT_HEADER.match(git("var", "GIT_COMMITTER_IDENT", _return_cmd=True).stdout.strip())
    assert committer is not None and me is not None, f"{sha}: unparsable committer"
    lines = [
        b"tree " + tree.encode(),
        b"parent " + onto.encode(),
        b"author " + headers[b"author"],
        b"committer %s <%s> %s" % (me[1], me[2], committer[3]),
    ]
    if b"encoding" in headers:
        lines.append(b"encoding " + headers[b"encoding"])
    obj = b"\n".join(lines) + b"\n\n" + message
    return str(git("hash-object", "-w", "-t", "commit", "--stdin", _in=obj)).strip()


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


@dataclass
class CommitInfo:
    sha: str
    subject: str
    files: list[str]
    requires: list[str] = field(default_factory=list)


def _reject_merges(base: str) -> None:
    merges = str(git("rev-list", "--min-parents=2", f"{base}..HEAD")).strip()
    if merges:
        _fail({"ok": False, "errors": [f"merge commits are not supported: {merges.splitlines()[0]}"]})


class _Paths:
    """File paths, where a file also overlaps any path inside a directory of the same name."""

    def __init__(self, files: list[str]) -> None:
        self.files: set[str] = set()
        self.with_dirs: set[str] = set()
        self.add_files(files)

    def add_files(self, files: list[str]) -> None:
        for path in files:
            self.files.add(path)
            parts = path.split("/")
            self.with_dirs.update("/".join(parts[:n]) for n in range(1, len(parts) + 1))

    def add(self, other: _Paths) -> None:
        self.add_files(list(other.files))

    def overlaps(self, other: _Paths) -> bool:
        return not self.files.isdisjoint(other.with_dirs) or not other.files.isdisjoint(self.with_dirs)


def compute_requires(base: str) -> list[CommitInfo]:
    """List base..HEAD with, for each commit, the earlier commits it can't be replayed without."""
    commits = list_commits(base)
    infos = [CommitInfo(sha=c.sha, subject=c.message, files=_files(c.sha)) for c in commits]
    shas = [c.sha for c in commits]
    parents = [base] + shas[:-1]

    # Chain i leaves out commit i. Its tip is a commit whose tree equals
    # pos[i]'s, an original commit, except at the paths in dirty[i]: those of
    # i and of every commit it dropped.
    tips = list(parents)
    pos = list(shas)
    dirty = [_Paths(info.files) for info in infos]

    requires: list[set[int]] = [set() for _ in commits]
    for j in range(1, len(commits)):
        paths = _Paths(infos[j].files)
        chains = [i for i in range(j) if dirty[i].overlaps(paths)]
        results = merge_batch([(pos[i], tips[i], shas[j]) for i in chains])
        dropped = []
        merged: dict[int, str] = {}
        for i, result in zip(chains, results, strict=True):
            if isinstance(result, Conflict):
                requires[j].add(i)
                dirty[i].add(paths)
                dropped.append(i)
            else:
                merged[i] = result
                pos[i] = shas[j]
        # A dropped commit still leaves the skipped ones before it to pick up.
        behind = [i for i in dropped if pos[i] != parents[j]]
        for i, result in zip(behind, merge_batch([(pos[i], tips[i], parents[j]) for i in behind]), strict=True):
            if isinstance(result, Conflict):
                # Can't happen: the chain matches the original history outside what it left out.
                reason = f"skipped commits conflict with what {_short(commits[i].sha)} left out: {result.paths}"
                _fail({"ok": False, "errors": [f"internal error: {reason}"]})
            merged[i] = result
        for i, tip in zip(merged, commit_trees(list(merged.values())), strict=True):
            tips[i] = tip
        for i in dropped:
            pos[i] = shas[j]

    # Leaving i out drops everything that conflicts without it, so this is
    # mostly closed already; close it explicitly in case a replay happened to
    # apply cleanly without a commit it requires indirectly.
    for j in range(len(commits)):
        for k in sorted(requires[j]):
            requires[j] |= requires[k]

    for j, info in enumerate(infos):
        info.requires = [commits[i].sha for i in sorted(requires[j])]
    return infos


def components(infos: list[CommitInfo]) -> list[list[str]]:
    """Group commits connected by requires edges, in history order."""
    root = {info.sha: info.sha for info in infos}

    def find(sha: str) -> str:
        while root[sha] != sha:
            root[sha] = root[root[sha]]
            sha = root[sha]
        return sha

    for info in infos:
        for dep in info.requires:
            root[find(info.sha)] = find(dep)

    groups: dict[str, list[str]] = {}
    for info in infos:
        groups.setdefault(find(info.sha), []).append(info.sha)
    return list(groups.values())


def _short(sha: str) -> str:
    return sha[:SHA_DISPLAY_LEN]


def _default_trunk() -> str:
    from .run import _find_closest_base_branch

    return _find_closest_base_branch()


TrunkOption = Annotated[
    str | None,
    typer.Option("--trunk", help="Branch the stacks are based on (default: main or master, whichever is closer)."),
]


@app.command("analyze")
def analyze_command(trunk: TrunkOption = None) -> None:
    """List trunk..HEAD with the commits each one textually requires, and the independent groups they form."""
    trunk = trunk or _default_trunk()
    base = resolve_branch_base(trunk)
    _reject_merges(base)
    infos = compute_requires(base)
    _emit(
        {
            "trunk": trunk,
            "base": _short(base),
            "commits": [
                {
                    "sha": _short(info.sha),
                    "subject": info.subject,
                    "files": info.files,
                    "requires": [_short(s) for s in info.requires],
                }
                for info in infos
            ],
            "components": [[_short(s) for s in group] for group in components(infos)],
        }
    )


# ---------------------------------------------------------------------------
# check / apply
# ---------------------------------------------------------------------------


@dataclass
class LayerSpec:
    name: str
    commits: list[str]  # full SHAs once resolved


@dataclass
class StackSpec:
    trunk: str
    stacks: list[list[LayerSpec]]  # each stack's layers, bottom to top


def _shape_errors(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ["spec must be a JSON object with 'trunk' and 'stacks'"]
    errors = []
    if not isinstance(data.get("trunk"), str) or not data["trunk"]:
        errors.append("'trunk' must be a branch name")
    stacks = data.get("stacks")
    if not isinstance(stacks, list) or not stacks:
        return [*errors, "'stacks' must be a non-empty list"]
    for s, stack in enumerate(stacks, 1):
        layers = stack.get("layers") if isinstance(stack, dict) else None
        if not isinstance(layers, list) or not layers:
            errors.append(f"stack {s}: 'layers' must be a non-empty list")
            continue
        for n, layer in enumerate(layers, 1):
            if (
                not isinstance(layer, dict)
                or not isinstance(layer.get("name"), str)
                or not isinstance(layer.get("commits"), list)
                or not all(isinstance(c, str) for c in layer["commits"])
            ):
                errors.append(f"stack {s}, layer {n}: needs 'name' and a list of 'commits'")
            elif not layer["commits"]:
                errors.append(f"layer {layer['name']!r}: has no commits")
    return errors


def parse_spec(text: str) -> StackSpec:
    """Parse and validate the spec's shape; commits are resolved later, against the range."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        _fail({"ok": False, "errors": [f"invalid JSON in spec: {e}"]})
    errors = _shape_errors(data)
    if errors:
        _fail({"ok": False, "errors": errors})
    return StackSpec(
        trunk=data["trunk"],
        stacks=[
            [LayerSpec(name=layer["name"], commits=list(layer["commits"])) for layer in stack["layers"]]
            for stack in data["stacks"]
        ],
    )


def resolve_spec(spec: StackSpec, base: str) -> list[str]:
    """Resolve the spec's SHA prefixes in place against base..HEAD; return every error found."""
    in_range = [c.sha for c in list_commits(base)]
    errors: list[str] = []
    seen_commits: dict[str, str] = {}
    seen_names: set[str] = set()

    for stack in spec.stacks:
        for layer in stack:
            if layer.name in seen_names:
                errors.append(f"layer {layer.name!r} appears more than once")
            seen_names.add(layer.name)
            try:
                git("check-ref-format", f"refs/heads/{layer.name}")
            except sh.ErrorReturnCode:
                errors.append(f"layer {layer.name!r} is not a valid branch name")

            resolved = []
            for ref in layer.commits:
                matches = [sha for sha in in_range if sha.startswith(ref.lower())] if ref else []
                if not matches:
                    errors.append(f"{ref!r} is not a commit in {spec.trunk}..HEAD")
                    continue
                if len(matches) > 1:
                    errors.append(f"{ref!r} is ambiguous; use a longer prefix")
                    continue
                sha = matches[0]
                if sha in seen_commits:
                    errors.append(f"{_short(sha)} is in both {seen_commits[sha]!r} and {layer.name!r}")
                seen_commits[sha] = layer.name
                resolved.append(sha)
            layer.commits = resolved

    for sha in in_range:
        if sha not in seen_commits:
            errors.append(f"{_short(sha)} is not in any layer")
    return errors


def _subject(sha: str) -> str:
    return str(git.log("-1", "--format=%s", sha, _decode_errors="replace")).strip()


def _blocked_by(sha: str, placed: set[str], paths: list[str], base: str) -> list[str]:
    """Earlier commits, missing from this stack below *sha*, that touch the conflicted paths."""
    earlier = str(git("rev-list", "--reverse", f"{base}..{sha}^")).strip().splitlines()
    return [_short(c) for c in earlier if c not in placed and set(_files(c)) & set(paths)]


def build(spec: StackSpec, base: str) -> dict[str, Any]:
    """Build every stack in the object database and report the result as JSON-ready data."""
    report: dict[str, Any] = {"ok": True, "trunk": spec.trunk, "base": _short(base), "stacks": []}
    tops: list[str] = []

    for stack in spec.stacks:
        built: list[dict[str, Any]] = []
        report["stacks"].append({"layers": built})
        tip = base  # each layer builds on the one below it, the bottom one on trunk
        placed: set[str] = set()
        for layer in stack:
            entries = []
            for sha in layer.commits:
                result = replay(sha, tip)
                if isinstance(result, Conflict):
                    report["ok"] = False
                    report["conflict"] = {
                        "layer": layer.name,
                        "commit": _short(sha),
                        "subject": _subject(sha),
                        "paths": result.paths,
                        "blocked_by": _blocked_by(sha, placed, result.paths, base),
                    }
                    return report
                entries.append(
                    {
                        "sha": _short(sha),
                        "subject": _subject(sha),
                        "new_sha": _short(result),
                        "rewritten": result != sha,
                    }
                )
                placed.add(sha)
                tip = result
            built.append({"name": layer.name, "tip": tip, "commits": entries})
        tops.append(tip)

    # Every stack built; together they must reproduce HEAD.
    combined: str = tops[0]
    for top in tops[1:]:
        merged = merge_trees(base, combined, top)
        if isinstance(merged, Conflict):
            report["ok"] = False
            report["conflict"] = {"between_stacks": True, "paths": merged.paths}
            return report
        [combined] = commit_trees([merged])  # older git merges only commits
    head_tree = _tree("HEAD")
    if _tree(combined) != head_tree:
        report["ok"] = False
        diff = str(git.diff("--name-only", _tree(combined), head_tree)).strip()
        report["errors"] = [f"the stacks combined differ from HEAD in: {', '.join(diff.splitlines())}"]
    return report


def _load(spec_path: str) -> tuple[StackSpec, str]:
    try:
        with open(spec_path) as f:
            text = f.read()
    except OSError as e:
        _fail({"ok": False, "errors": [f"cannot read spec: {e}"]})
    spec = parse_spec(text)
    base = resolve_branch_base(spec.trunk)
    _reject_merges(base)
    errors = resolve_spec(spec, base)
    if errors:
        _fail({"ok": False, "errors": errors})
    return spec, base


SpecOption = Annotated[
    str | None,
    typer.Option(
        "--spec",
        help=f"Path to the JSON stack spec (default: `git rev-parse --git-path {DEFAULT_SPEC_NAME}`).",
    ),
]


def _spec_path(spec: str | None) -> str:
    """*spec*, or the default spec path in this worktree's git dir."""
    return spec or str(git("rev-parse", "--git-path", DEFAULT_SPEC_NAME)).strip()


@app.command("check")
def check_command(spec: SpecOption = None) -> None:
    """Simulate the spec: report each layer, and the first conflict if a reorder isn't clean. Writes no refs."""
    parsed, base = _load(_spec_path(spec))
    report = build(parsed, base)
    _emit(report)
    if not report["ok"]:
        raise StackFailedError()


@app.command("apply")
def apply_command(spec: SpecOption = None) -> None:
    """Create a branch for each layer, all or none. Never moves HEAD or touches the working tree."""
    parsed, base = _load(_spec_path(spec))
    report = build(parsed, base)
    if not report["ok"]:
        _fail(report)

    layers = [layer for stack in report["stacks"] for layer in stack["layers"]]
    to_create = []
    errors = []
    for b in layers:
        try:
            existing = str(git("rev-parse", "--verify", "--quiet", f"refs/heads/{b['name']}")).strip()
        except sh.ErrorReturnCode:
            to_create.append(b)
            continue
        if existing != b["tip"]:
            errors.append(f"branch {b['name']!r} already exists at {_short(existing)}")
    if errors:
        report["ok"] = False
        report["errors"] = errors
        _fail(report)

    if to_create:
        # One transaction: "create" refuses existing refs, and nothing is written unless all succeed.
        lines = ["start", *(f"create refs/heads/{b['name']} {b['tip']}" for b in to_create), "commit", ""]
        try:
            git("update-ref", "--stdin", _in="\n".join(lines))
        except sh.ErrorReturnCode as e:
            # e.g. "a" and "a/b" can't both be branches
            report["ok"] = False
            report["errors"] = [e.stderr.decode().strip()]
            _fail(report)

    report["created"] = [b["name"] for b in to_create]
    report["gh_stack_init"] = [
        shlex.join(["gh", "stack", "init", "--base", parsed.trunk, *(layer["name"] for layer in stack["layers"])])
        for stack in report["stacks"]
    ]
    _emit(report)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _emit(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2))


def _fail(data: dict[str, Any]) -> NoReturn:
    _emit(data)
    raise StackFailedError()
