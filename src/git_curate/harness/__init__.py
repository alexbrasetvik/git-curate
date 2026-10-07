"""Harness protocol and registry for AI model invocation."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import sh
from rich.console import Console

from ..common import NotInGitRepoError, UnknownHarnessError, load_failed_attempt

console = Console()

THINKING_MSGS = [
    "Amending the past",
    "Applying patch",
    "Archiving doubts",
    "Asking <<<<<<< why it feels that way",
    "Bisecting the blame",
    "Blaming myself",
    "Checking out mentally",
    "Cherry-picking excuses",
    "Cloning a better idea",
    "Committing with optimism",
    "Compressing deltas",
    "Counting objects",
    "Detaching HEAD, emotionally",
    "Fast-forwarding regret",
    "Fetching fresh chaos",
    "Finding the fork point",
    "Force-pushing consequences",
    "Garbage collecting",
    "Grafting goodness",
    "Grepping through the wreckage",
    "Ignoring responsibly",
    "Indexing thoughts",
    "Merging timelines",
    "Packing refs",
    "Processing unresolved conflict trauma",
    "Pruning dead branches",
    "Pulling it together",
    "Pulling questionable history",
    "Pushing boundaries",
    "Pushing my luck",
    "Rebasing reality",
    "Resetting expectations",
    "Resolving conflicts",
    "Resolving the merge base",
    "Reusing recorded regret",
    "Reviewing commitment issues",
    "Revising history",
    "Rewinding history",
    "Shallow cloning your patience",
    "Signing in triplicate",
    "Squashing bad decisions",
    "Staging changes",
    "Stashing dignity",
    "Tagging this disaster",
    "Walking the object graph",
]


class BaseHarness:
    """Shared scaffolding for all harnesses (temp dir lifecycle, repo root resolution)."""

    def __init__(self, model: str | None = None) -> None:
        # None leaves the choice to the harness CLI's own default.
        self.model = model

    def model_args(self) -> list[str]:
        """Return the `--model` argument for the harness CLI, if a model was chosen."""
        return ["--model", self.model] if self.model else []

    def run(self, base_sha: str) -> None:
        try:
            repo_root = sh.git("rev-parse", "--show-toplevel", _err=os.devnull).strip()
        except sh.ErrorReturnCode as e:
            print("error: not inside a git repository", file=sys.stderr)
            raise NotInGitRepoError() from e

        raw_dir = tempfile.mkdtemp(prefix="git-curate-")
        # Resolve symlinks (macOS /tmp → /private/tmp) so the realpath matches
        # what the Write tool sees.
        temp_dir = os.path.realpath(raw_dir)
        spec_path = os.path.join(temp_dir, "spec.json")

        # Start from a previously failed spec, if any, so the agent revises it
        # in place (it may only edit files under temp_dir).
        previous = load_failed_attempt(base_sha)
        if previous is not None:
            shutil.copyfile(previous.path, spec_path)

        try:
            self._run(base_sha, repo_root, temp_dir, spec_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _run(self, base_sha: str, repo_root: str, temp_dir: str, spec_path: str) -> None:
        raise NotImplementedError


def build_prompt(base_sha: str, spec_path: str) -> str:
    from ..deps import compute_dependencies

    approach = load_approach()

    deps = compute_dependencies(base_sha)
    constraint_lines = ""
    if deps:
        lines = [
            "\nOrdering constraints — these commit pairs touched overlapping line ranges",
            "and must keep their original relative order to avoid rebase conflicts:",
        ]
        for d in deps:
            lines.append(f'- "{d.earlier_msg}" must precede "{d.later_msg}"')
        constraint_lines = "\n".join(lines) + "\n"

    previous_lines = ""
    previous = load_failed_attempt(base_sha)
    if previous is not None:
        previous_lines = (
            "\nA previous grouping attempt failed; the rebase was aborted and the temp\n"
            "commits are unchanged. The spec path already contains that attempt's spec.\n"
            "Revise it rather than regrouping from scratch; a failure usually means two\n"
            "commits touching the same lines were put in the wrong order.\n\n"
            f"Failure output:\n```\n{previous.output.strip()}\n```\n"
        )

    # A mechanical check and input checks whether any staged changes need slicing first, so
    # when we get to this point, the agent can assume everything has been sliced and not ask
    # about doing so.
    return (
        "You are helping curate git commits for a project. Temp commits have already\n"
        "been created via `uvx git-curate slice`. Your job: read the diff, group\n"
        "commits logically, and execute the grouping.\n\n"
        "**Never use `git rebase`, `git cherry-pick`, or any other git command to\n"
        "rearrange commits. `uvx git-curate group` is the only permitted execution\n"
        "mechanism. Do not write helper scripts.**\n\n"
        "---\n"
        f"{approach}\n"
        "---\n\n"
        # It's important to keep the static parts of the prompt first, then the dynamic parts,
        # for prompt cacheability.
        f"Base: {base_sha}\n"
        f"Spec path: {spec_path}\n"
        f"{constraint_lines}"
        f"{previous_lines}"
    )


def resolve_harness_name(name: str | None) -> str:
    """Return *name* if given, otherwise read git config git-curate.harness, else 'claude'."""
    if name is not None:
        return name
    try:
        return str(sh.git("config", "git-curate.harness")).strip()
    except sh.ErrorReturnCode:
        return "claude"


def resolve_model(model: str | None) -> str | None:
    """Return *model* if given, otherwise read git config git-curate.model, else None."""
    if model is not None:
        return model
    try:
        return str(sh.git("config", "git-curate.model")).strip() or None
    except sh.ErrorReturnCode:
        return None


def get_harness(name: str, model: str | None = None) -> BaseHarness:
    from .claude import ClaudeHarness
    from .pi import PiHarness

    registry = {
        "claude": ClaudeHarness,
        "pi": PiHarness,
    }
    if name not in registry:
        available = ", ".join(registry)
        print(f"error: unknown harness {name!r}. Available: {available}", file=sys.stderr)
        raise UnknownHarnessError()
    return registry[name](model)


def load_approach() -> str:
    """Return the ## Approach section from the packaged SKILL.md."""
    from importlib.resources import files

    text = files("git_curate.harness").joinpath("SKILL.md").read_text(encoding="utf-8")
    marker = "\n## Approach\n"
    idx = text.find(marker)
    if idx == -1:
        raise RuntimeError("'## Approach' section not found in SKILL.md")
    return text[idx + len(marker) :]
