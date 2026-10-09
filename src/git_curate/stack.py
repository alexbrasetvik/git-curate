"""
Stacks — split a tidy branch into GitHub stacked-PR branches
============================================================

Backs the git-curate-stack skill. The branch's commits are never edited, only
reordered or redistributed into one or more stacks, each based on trunk. Everything happens in the object database with
``git merge-tree`` and ``git commit-tree``: HEAD, the current branch and the
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
cherry-picking C onto X would apply without conflicts.

``analyze`` finds, for each commit i, the later commits that can't be
replayed without it: it rebuilds the chain from i's parent leaving i out, and
any later commit that conflicts requires i and is left out too, so whatever
needs that commit registers as needing i as well.

``check`` builds each stack bottom to top and merges the stack tops together;
the result must equal HEAD's tree, so nothing is lost in the split.

Usage:
------
    uvx git-curate stack analyze [--trunk main]
    uvx git-curate stack check --spec .git/git-curate-stack-spec.json
    uvx git-curate stack apply --spec .git/git-curate-stack-spec.json
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import Annotated, Any, NoReturn

import sh
import typer

from .common import (
    GIT_ENV,
    SHA_DISPLAY_LEN,
    EnvOverlay,
    Exit,
    git,
    list_commits,
    pre_checks,
    resolve_branch_base,
)

DEFAULT_SPEC_PATH = ".git/git-curate-stack-spec.json"

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
    """Three-way merge tree-ishes in the object database; return the tree or the conflicted paths."""
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


def _parent(sha: str) -> str:
    return str(git("rev-parse", f"{sha}^")).strip()


def _tree(rev: str) -> str:
    return str(git("rev-parse", f"{rev}^{{tree}}")).strip()


def _files(sha: str) -> list[str]:
    out = str(git("diff-tree", "--no-commit-id", "--name-only", "-r", sha)).strip()
    return out.splitlines() if out else []


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

    fields = str(git.log("-1", "--format=%an%x00%ae%x00%ad%x00%cd", "--date=raw", sha)).rstrip("\n").split("\0")
    name, email, author_date, committer_date = fields
    # The raw object keeps the message byte for byte, which %B doesn't.
    raw = str(git("cat-file", "commit", sha))
    message = raw.split("\n\n", 1)[1] if "\n\n" in raw else ""
    commit_git = git.bake(
        _env=EnvOverlay(
            {
                **GIT_ENV.overrides,
                "GIT_AUTHOR_NAME": name,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_AUTHOR_DATE": author_date,
                "GIT_COMMITTER_DATE": committer_date,
            }
        )
    )
    return str(commit_git("commit-tree", tree, "-p", onto, "-F", "-", _in=message)).strip()


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


def compute_requires(base: str) -> list[CommitInfo]:
    """List base..HEAD with, for each commit, the earlier commits it can't be replayed without."""
    commits = list_commits(base)
    infos = [CommitInfo(sha=c.sha, subject=c.message, files=_files(c.sha)) for c in commits]
    trees = [_tree(c.sha) for c in commits]
    parent_trees = [_tree(base)] + trees[:-1]

    requires: list[set[int]] = [set() for _ in commits]
    for i in range(len(commits)):
        tip = parent_trees[i]
        for j in range(i + 1, len(commits)):
            result = merge_trees(parent_trees[j], tip, trees[j])
            if isinstance(result, Conflict):
                requires[j].add(i)
            else:
                tip = result

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
    return str(git.log("-1", "--format=%s", sha)).strip()


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
        combined = merged
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


SpecOption = Annotated[str, typer.Option("--spec", help="Path to the JSON stack spec.")]


@app.command("check")
def check_command(spec: SpecOption = DEFAULT_SPEC_PATH) -> None:
    """Simulate the spec: report each layer, and the first conflict if a reorder isn't clean. Writes no refs."""
    parsed, base = _load(spec)
    report = build(parsed, base)
    _emit(report)
    if not report["ok"]:
        raise StackFailedError()


@app.command("apply")
def apply_command(spec: SpecOption = DEFAULT_SPEC_PATH) -> None:
    """Create a branch for each layer, all or none. Never moves HEAD or touches the working tree."""
    parsed, base = _load(spec)
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
