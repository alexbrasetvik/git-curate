---
name: git-curate
description: Make logical git commits, considering the individual line changes and their relationships, and that a single file can have multiple different logical changes that should become separate commits.
license: MIT
metadata:
  tags: [git]
allowed-tools:
  - Bash(uvx git-curate *)
  - Bash(git log *)
---

# Grouping and squashing git skill

You are a software engineer curating logical git commits.

`uvx git-curate slice` makes fine-grained `temp:` commits, each modifying a contiguous line range in a single file. Group these `temp:` commits into logical final commits with meaningful messages, in an order that respects dependencies and reviewer ergonomics.

Main principle: Going from many small commits to fewer large commits is easy. Splitting a commit is hard. **If in doubt, leave more commits.**

A line goes into a single commit. Never split a single line across different commits.

This skill changes only commit history, not code. Invoke `uvx git-curate group` with a JSON spec describing how to group the existing commits.

**Never use `git rebase`, `git cherry-pick`, or any other git command to rearrange commits. `uvx git-curate group` is the only permitted execution mechanism. Do not write helper scripts.**

## Execution approach

Spawn a sub-agent to handle the diff reading and grouping. This keeps the parent context clean from large diff output.

**Before spawning**, run step 0 to determine the session state and extract the base SHA. Then write a 3–5 sentence summary of what this session addressed: what feature or bug, key design decisions, and major areas of change.

Then use the Agent tool with a prompt structured exactly as follows. Do not summarize, condense, or paraphrase any part of it:

```
You are helping curate git commits for a project. Temp commits have already
been created via `uvx git-curate slice`. Your job is steps 1–4: read the
diff, group commits, and execute the grouping.

Session context: <your 3–5 sentence summary here>

---
<paste the entire ## Approach section below, word for word>
---

Base: <the 12-char SHA from the Base: line of `uvx git-curate status`>
Spec path: .git/git-curate-spec.json
```

The parent handles step 0 (slicing). The sub-agent handles steps 1–4 only and
reports back with the final commit log.

## Approach

0. Check the current state:

   ```bash
   uvx git-curate status
   ```

   The output has four lines:

   ```
   Session: active (N temp commits)
   Base:    a1b2c3d12345 — Commit message of the base
   Staged:  N files
   Unstaged: N files
   ```

   **Extract the base SHA**: it is the 12-character hex string on the `Base:` line, before the `—`.

   Based on the `Session:` line:
   - **Session active**: proceed directly to step 1 (no slicing needed).
   - **Session none, staged files**: run `uvx git-curate slice`.
   - **Session none, no staged files, unstaged files**: ask the user:
     _"You have unstaged changes but nothing is staged. Should I treat all unstaged changes as what will be committed (equivalent to `git add -u`; untracked files are not included)?"_
     If they confirm, run `uvx git-curate slice --all`. If they decline, ask them to stage what they want first and stop.
   - **Session none, nothing staged or unstaged**: nothing to do.

   **Never run `git add`**: staging is either already done by the user or handled by the `--all` flag.

   After slicing (if needed), re-run `uvx git-curate status` and extract the updated base SHA before spawning the sub-agent.

1. Get the git diff between the base and HEAD.
2. Group commits into logical units of work based on their diff.
3. Write a JSON spec that describes the grouping and the final commit messages.
4. Run `uvx git-curate group` with the spec to execute the grouping and squashing.

### Get the git diff

The base SHA is on the **Base** line at the end of this prompt. Use it explicitly:

```bash
uvx git-curate diff --tmp <base>
```

The `--tmp` flag writes the diff to `.git/git-curate-diff.patch` inside the
repo and prints `<path> <line-count>` on a single line. Capture both values
from the output. **Do not run `wc -l`**; the count is already there. Use
**only the path printed by this command**; do not read any other patch files
referenced in context.

Page through the file using the Read tool with `offset` and `limit`. **This file is your only source of truth.** Do not run `git show`, `git diff`, or `git log` variants to inspect individual commits. Everything you need is in the diff file.

Before reading each page, print a brief progress line — e.g. `Reading diff (lines 1–500 of 2400)…` — so the user can track progress.

**Do NOT use** `cat`, `head`, `tail`, `wc`, `grep`, `sed`, or `awk`. The Read tool is the only permitted way to read the diff.

### Group commits

Use the commit diffs to understand which commits share the same logical change.

For example, renaming a method involves changes to the definition, all call sites, and tests. Group these into one commit: `Rename calculate() to compute()`.

Separate refactorings should be separate commits, not a single commit with multiple refactorings that mention them all.

This is particularly important for large code moves: If sliced commits exist that move the code and then also modify it, the move itself must be its own commit before the modifications.

Ease of understanding per commit trumps. Tests and quality checks are allowed to fail in intermediate commits to allow for this.

It is OK to have a commit that is just a single line change if it is logically distinct from other changes.

Formatting or whitespace-only changes belong in their own commit, never mixed with functional changes.

Dead code removal belongs with the refactor that made it dead, not as a trailing cleanup commit.

Build, tooling, and config changes are isolated from feature work.

### Test code grouping

Regression tests for bug fixing of existing code should get its own commit and precede the fix, to enable easy verification that the test fails without the fix.

New features should have their tests grouped with the feature implementation, not in a separate commit.

Test infrastructure changes, e.g. refactoring test helpers or setup, belong in their own commit.

Larger tests that integrate multiple features belong in a separate commit after the features they test. E.g commits ["Add and test feature A", "Add and test feature B", "Add integration test for A and B working together"].

If ambigous, err on separate commits, as subsequent user squashing is easy.

### Order commits

Order commits topologically: if commit B depends on commit A, A comes first. Dependencies can span files, so assess the nature of each change when ordering.

Keep related commits adjacent. Prefer: Add A, Use A, Add B, Use B. Not: Add A, Add B, Use A, Use B.

Type and interface definitions precede their implementations.

Trivial changes are preferred early in the list of commits. This makes it easier to split PRs full of smaller and easier to review commits.

#### Check cross-group dependencies

After forming the initial grouping, check for cross-file dependencies that semantic grouping alone won't surface.

For each group, write down the **branch-specific names it introduces**: identifiers on `+` lines that are new to this branch (new table names, function/method names, class names, field names; not common words). Produce a short table before moving on:

Group 1 "Add helper utilities": introduces formatResult, parseConfig
Group 2 "Refactor core logic": introduces (none; consumes formatResult)
Group 3 "Add feature X": introduces (none)
…

Then scan the other groups' diffs for any use of each introduced name. If group B references a name that group A introduces, A must precede B.

Adjust the ordering before writing the spec. This catches producer/consumer pairs that live in different files and look unrelated at the semantic level.

#### Commits that change the same line in turn

Several `temp:` commits can change the same line, one after another. This happens when more than one slicing session touched a file, or when existing commits were rewritten: each original commit is sliced on its own, and its `temp:` commits carry a `Curate-Source:` trailer naming the commit they came from. For example, one commit renames `foo` to `bar` on an import line, and a later one changes it from `bar` to `bar, baz`.

Such commits can go in separate groups when they are separate logical changes, such as a rename and a new parameter on the same line. The group holding the earlier change must then come first. If they are the same logical change, put them in one group: the result is their combined change. Within a group, `group` applies commits in their original order, whatever order the spec lists them in.

The `Curate-Source:` trailers show how the original author split the work. Use them as hints, not as a grouping to reproduce.

**Explicit ordering constraints:** When hunk-range overlaps are detected, an **Ordering constraints** section appears at the end of this prompt listing the affected pairs. Every listed pair is a hard requirement — the `group` command will reject a spec that violates one. When the two commits of a pair are in different groups, the earlier commit's group must come first in the top-level list.

### Commit authoring

The headline (first line): a capitalized, imperative summary of 50 characters or fewer. For small or obvious commits, this is enough.

For larger commits (ones that touch multiple files, introduce new infrastructure, or contain a non-obvious design decision), add a body after a blank line. The blank line is critical; tools like rebase break when you run the summary and body together. Wrap body text at 72 characters.

Write the headline and the body in the imperative mood: "Fix bug" not "Fixed bug" or "Fixes bug." This matches what `git merge` and `git revert` generate.

The body states what the commit does to the code, as if giving the command: "Split hunks at blank lines between sibling blocks", "Add a test that slices two adjacent functions". Do not narrate history ("used to", "previously", "now", "this commit"). When the motivation is a limitation, state it as a present-tense fact ("Adjacent functions share one temp commit, so they cannot land separately.") and follow it with the imperative change.

Use the names that appear in the code and docs. Do not coin phrases like "one run of lines" for something the code calls a hunk.

Further paragraphs come after blank lines. Bullet points are fine: use a hyphen or asterisk followed by a single space, with blank lines between items, and a hanging indent. Keep the body focused: what problem it solves and the key approach. Omit anything self-evident from the headline.

### Write the grouping spec

The JSON spec is an ordered list of objects. The order determines the final commit ordering. Each object has two fields:

- message: the final commit message for the group
- commits: the list of existing commit messages that belong to this group (exact matches)

Each object in the top-level list becomes a new commit.

> **Note:** Existing commits not listed in any group pass through unchanged.
> Non-`temp:` commits you want to keep need not appear in the spec.

Example JSON spec with two final commits, from three original commits:

```json
[
  {
    "message": "New commit message\n\nIt can be multi-line.",
    "commits": ["existing commit message", "other existing commit message"]
  },
  {
    "message": "Another commit message\n\nIt can be multi-line.",
    "commits": ["yet another existing commit"]
  }
]
```

### Execute the grouping

Build one complete JSON spec covering all commits. Write it to the path on the
**Spec path** line at the end of this prompt using the Write tool, then run:

```bash
uvx git-curate group --spec <spec-path>
```

Single invocation only. Do not call `group` multiple times or attempt partial applies. Do not invoke `git rebase`. The `group` command deletes the spec file automatically on success.

If `group` fails with "Rebase stopped", it has already aborted the rebase and restored the temp commits. Read the failure output, revise the spec (usually two commits touching the same lines are in the wrong order), and run `group` again with the same spec path. Do not try to resolve conflicts by hand.

After the command succeeds, verify the result using the base SHA from the **Base** line:

```bash
uvx git-curate log <base>
```

Report the final commit log to the user, with a succinct summary of the grouping and ordering rationales.
