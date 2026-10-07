# git-curate

git-curate lets an AI agent commit at diff hunk granularity. A single file can produce several commits, one per logical change.

`git add -p / --patch` does this interactively. git-curate does it for agents.

## Logical commits are easier to understand

Reviewers need to know what changed and why. Focused commits answer that; large blobs don't.

Reviewing your colleague's changes and reviewing your AI agent's output are the same task: compress edits into a narrative a reader can follow. Logical commits serve both.

Existing tools stage whole files (`git add <file>`). They can't drive `git add -p`, the hunk-by-hunk staging workflow. AI-authored changes land as one massive commit per file, even when a file contains several independent logical changes. git-curate fixes that.

## How it works

The workflow has three phases:

1. **Slice**: `git-curate slice` creates one temporary commit per diff hunk, with no reasoning. Each commit matches one hunk from `git add -p`.
2. **Group** (AI): an AI agent reads the commit diffs and decides which hunks belong together, producing a JSON grouping spec.
3. **Finalize**: `git-curate group <spec>` squashes the `temp:` commits into final commits via non-interactive rebase.

Run `git-curate` alone to execute all three steps, using Claude or pi as the model harness.

## Installation

Install with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install --reinstall /path/to/git-curate
```

You can then run `git-curate`, `git curate`, or `uvx git-curate` depending on your shell setup. The skills use `uvx git-curate`.

Install the skill:

```bash
npx skills add /path/to/git-curate
```

## Usage

Run `uvx git-curate` or `git curate` with no options to run the full workflow:

- `uvx git-curate slice` commits each staged hunk separately.
- Claude Code or pi groups those commits into logical units.
- `uvx git-curate group` squashes them into the final commits.

### Slicing

`slice` creates one commit per hunk, for `group` to later squash.

Stage your changes, then run:

```bash
uvx git-curate slice
```

**Staged vs. unstaged:** `slice` operates on whatever is staged. If nothing is staged but you have unstaged changes, pass `--all` to stage changes to tracked files first (untracked files are not added):

```bash
uvx git-curate slice --all
```

`slice` only touches staged changes. Unstaged files are left alone.

To limit slicing to specific files:

```bash
uvx git-curate slice src/auth.py src/schema.py
```

To preview without committing:

```bash
uvx git-curate slice --dry-run
```

### Grouping

After slicing, write the diff to disk for the agent:

```bash
uvx git-curate diff --tmp
```

`--tmp` writes the diff to `.git/git-curate-diff.patch` and prints `<path> <line-count>` on one line. The agent reads this file and produces a JSON grouping spec.

The spec is an ordered list of groups. Order determines the final commit order:

```json
[
  {
    "message": "Rename calculate() to compute()\n\nUpdates the method definition, all call sites, and tests.",
    "commits": [
      "temp: src/math.py:L10-12",
      "temp: src/math.py:L45-45",
      "temp: tests/test_math.py:L8-8"
    ]
  },
  {
    "message": "Add overflow guard to compute()",
    "commits": [
      "temp: src/math.py:L13-18"
    ]
  }
]
```

`group` leaves any `temp:` commit not in the spec as-is. Non-`temp:` commits pass through unchanged.

Execute the spec:

```bash
uvx git-curate group .git/git-curate-spec.json
```

Verify the result:

```bash
git log refs/git-curate/base..HEAD
```

## Architecture

### Why many small commits first

Squashing commits is trivial. Splitting them is hard. Mix flour and water into dough and you can't separate them back out.

`slice` errs toward maximum granularity. One `temp:` commit per diff hunk is the finest grain the unified diff format supports without splitting lines. The agent then groups the hunks, a task that requires understanding code semantics.

### Division of labour

The tool handles mechanics: parsing unified diffs, computing correct hunk headers, managing a throwaway index, driving non-interactive rebase. These operations are deterministic and brittle; small errors corrupt history. They belong in tested code, not an LLM prompt.

The agent handles reasoning: deciding which hunks belong together and writing messages that explain intent. Language models handle this well; rule-based heuristics produce mediocre results. Each layer does what the other can't.

## Agent Skill

The repository includes a skill at [skills/git-curate/SKILL.md](skills/git-curate/SKILL.md). It covers invocation, staged vs. unstaged handling, spawning a focused sub-agent for grouping, and final log verification.

Invoke `/git-curate` in your agent session after making changes.

It works with Claude Code and pi.

The skill has access to the git-curate tools and `git log`. The git status and diff commands it needs are baked into `git-curate` to simplify permission handling.
