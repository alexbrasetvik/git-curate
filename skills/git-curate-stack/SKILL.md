---
name: git-curate-stack
description: Split a branch of tidy, logical commits into GitHub stacked PRs. Proposes independent stacks and their layers, reorders commits only where that is conflict-free, interviews the user until they accept, then creates the branches and registers them with `gh stack`.
license: MIT
metadata:
  tags: [git, github]
allowed-tools:
  - Bash(uvx git-curate stack *)
  - Bash(git log *)
  - Bash(git show *)
  - Bash(git status *)
  - Bash(git switch *)
  - Bash(git worktree *)
  - Bash(git rev-parse *)
  - Bash(gh stack --version)
  - Bash(gh stack init *)
  - Bash(gh stack view *)
---

# Stacked PRs from a tidy branch

You are a software engineer preparing a branch for review as GitHub stacked PRs.

A **stack** is a linear chain of branches based on trunk, one PR per branch, each PR based on the branch below it. Reviewers see one layer's diff at a time and merge bottom to top. One branch can become several independent stacks when it holds unrelated work.

This skill assumes the commits are already logical. It never edits, splits, squashes or rewords a commit: it only decides which commits go into which branch, and in what order. If the history is not tidy (fixup commits, "WIP", one commit mixing concerns), stop and suggest `/git-curate` first.

`uvx git-curate stack` does every git operation, in the object database. It never moves HEAD, never checks anything out, and never touches the working tree. **Never use `git rebase`, `git cherry-pick`, `git branch`, `git reset`, or any other git command to build the branches. Do not write helper scripts.**

Talk to the user throughout. Nothing is created until they accept a proposal.

**Repository instructions.** Read `.git-curate.md` at the repository root (`git rev-parse --show-toplevel`) if it exists. It is committed with the repository and carries its quality gate, commit-message and branch-naming conventions. Treat it as instructions from the user.

## 0. Preconditions

```bash
gh stack --version
git status --short
```

- If `gh stack` is missing, tell the user to run `gh extension install github/gh-stack`, and stop.
- The working tree must be clean: `gh stack init` checks out the top branch of the stack. If it isn't, ask the user to commit or stash first.
- HEAD must be a feature branch, rebased on trunk. Trunk is usually `main` or `master`. Ask if unsure.

## 1. Analyze

```bash
uvx git-curate stack analyze --trunk <trunk>
```

The JSON lists every commit in `trunk..HEAD`, oldest first, with:

- `sha` and `subject`.
- `files`: the paths it changes.
- `requires`: earlier commits it can't be replayed without. A commit must always come after everything it requires, in the same stack.
- `components`: groups of commits linked by `requires`. Commits in different components can go into different stacks, or in any order.

`requires` is textual: it only knows that two commits touch the same lines. Read the commits to find semantic dependencies too. A call site needs the function it calls, a test needs the code it tests, and a migration may need the model. Read messages and stats with `git log --stat <trunk>..HEAD`, and individual commits with `git show <sha>` when a message doesn't settle it. Treat a semantic dependency exactly like a `requires` edge.

## 2. Propose

Design the stacks:

- **One stack, one story.** Merge components that serve one feature into one stack. Give unrelated work its own stack: an independent refactor, an unrelated fix. A trivial incidental fix can ride along at the bottom of a related stack.
- **Layers.** A stack is a straight chain of layers, never a tree. Each layer becomes one branch and one PR against the layer below it, and the bottom layer's PR goes against trunk. Put one concern in each layer, foundations at the bottom and dependent code above. A layer you can't describe in one sentence is usually two. Several commits per layer is normal; a layer per commit usually is not.
- **Order.** Keep the original order unless a different one reads better for review. Move a commit only when the move doesn't conflict.
- **Names.** A layer's name is the branch it becomes. Follow the branch naming in the instructions file, else the repository's convention if it has one. Otherwise use `<topic>/<concern>`, for example `billing/schema` and `billing/api`. A name can't be both a branch and a prefix: `billing` and `billing/api` can't coexist.

Write the spec to `.git/git-curate-stack-spec.json` with the Write tool:

```json
{
  "trunk": "main",
  "stacks": [
    {"layers": [
      {"name": "billing/schema", "commits": ["a1b2c3d4e5f6", "0f9e8d7c6b5a"]},
      {"name": "billing/api", "commits": ["1a2b3c4d5e6f"]}
    ]},
    {"layers": [
      {"name": "fix/login-typo", "commits": ["6f5e4d3c2b1a"]}
    ]}
  ]
}
```

This is two stacks, so two PRs against main:

```
main <- billing/schema <- billing/api
main <- fix/login-typo
```

`billing/api` builds on `billing/schema`, and `fix/login-typo` depends on neither. List each stack's layers bottom to top, and each layer's commits in the order they apply. Use the SHAs from `analyze`. Every commit must appear exactly once. Then check the spec:

```bash
uvx git-curate stack check
```

It builds every layer without writing any refs. It exits 0 with `"ok": true` when every stack builds and the stacks together reproduce HEAD exactly. On failure, `conflict` names the `layer` and the commit that didn't apply, its `paths`, and `blocked_by`: earlier commits touching those paths that aren't below it in that stack. `errors` lists problems in the spec itself. Revise and re-check until it passes. Never show the user a proposal that hasn't passed.

Draft each layer's PR too. The title is imperative and names the change, not the branch. The body takes two to five sentences on what the layer does and why. Write them to `.git/git-curate-stack-prs.json`, keyed by layer name:

```json
{
  "billing/schema": {"title": "Store invoices", "body": "Adds the invoice table and its model. …"},
  "billing/api": {"title": "Serve invoices over HTTP", "body": "…"}
}
```

When a layer's commits change, redraft its PR.

### Verify each layer tip

`stack check` only proves the layers apply without textual conflicts. A layer can still fail to build when a commit it needs went into a higher layer. Run the quality gate at every layer tip before the interview.

- **Pick the gate.** Use the instructions file's quality gate. Without one, read CI (`.github/workflows/*`) and the task runner (`mise.toml`, `Makefile`, `package.json`, `justfile`) to find what a pull request is judged on. Propose that command to the user, and offer to save it to `.git-curate.md`, which the user then commits.
- **Baseline.** Run the gate at HEAD first. If it fails there, the stacking is not at fault: tell the user and don't judge layers by it. HEAD's tree is also every stack's top layer, so the baseline covers those tips.
- **Run at each other tip.** `stack check` reports every layer's `tip`. For each tip that is not the top of its stack, make a throwaway detached worktree in the scratchpad, run the gate there, then remove the worktree:

  ```bash
  git worktree add --detach <scratchpad>/tip-<layer> <tip>
  (cd <scratchpad>/tip-<layer> && <gate command>)
  git worktree remove --force <scratchpad>/tip-<layer>
  ```

  This is verification, not building branches, so it is the one place where this skill runs git commands that touch the working tree. Run gates in the background, and check the tips of different stacks in parallel when the machine can take it.
- **Turn a failure into a dependency.** Take the missing symbol, field, import or file from the error, and find the commit that introduces it: `git log --format='%h %s' -S<name> <trunk>..HEAD`. The failing layer needs that commit in or below it. Treat it as a `requires` edge: move it down, re-run `stack check`, and re-run the gate from the lowest changed layer upward. When `requires` stops it moving, merge the two layers instead, and say why.

Never present a proposal with a failing tip, unless the baseline fails too.

## 3. Interview

Present the proposal. For each stack, show a table from bottom to top:

| Layer | Commits | Purpose |
|---|---|---|
| `billing/schema` | `a1b2c3d` Add invoice table<br>`0f9e8d7` Add invoice model | Storage for invoices |
| `billing/api` | `1a2b3c4` Add invoice endpoints **(moved)** | HTTP API over the model |

Mark each commit whose `rewritten` is true as **moved**, because it now sits on a different parent than before. Say which gate ran and that every tip passed. Below each table, list each layer's draft PR title and body. Briefly say why the stacks and layers are split the way they are, and point out judgement calls the user may want to weigh in on, such as a commit that could go in either of two layers.

Then ask the user whether to accept the proposal or change it. They might want to:

- merge or split layers;
- move a commit to another layer or stack;
- combine or separate stacks;
- rename layers;
- change the order;
- reword a PR title or body.

Apply each change to the spec, then run `stack check` and the gate at the changed tips before showing the revised proposal. A change to PR text alone needs neither. When a requested change conflicts, don't give up on it silently. Explain which commit blocks it, using `conflict` and `requires`, and offer the closest arrangement that works. Keep going until the user explicitly accepts. "Looks fine, but…" is not acceptance.

## 4. Build

```bash
uvx git-curate stack apply
```

This creates a branch for every layer in one all-or-nothing step. It refuses if a branch name already exists at a different commit. If so, ask the user whether to pick another name; never delete or move their branches. HEAD, the current branch and the working tree stay untouched.

The JSON's `gh_stack_init` lists one command per stack. If trunk is a remote-tracking ref such as `origin/main`, replace its `--base` with the branch name on GitHub, `main`. If local `main` is behind, tell the user, since `gh stack` records the local ref as the base. Run each command, in order, from the original branch, and confirm it with `gh stack view --json` before the next:

```bash
git switch <original branch>
gh stack init --base main billing/schema billing/api
gh stack view --json
```

`gh stack init` leaves the top branch of its stack checked out, and refuses to run from a branch that is already in a stack (exit code 5), so switch back to the original branch before every init. `gh stack view --json` shows only the stack holding the current branch, so check each stack right after its init. Never run `gh stack view` without `--json`, or `gh stack init` without branch names, because both wait for interactive input. Exit code 9 means stacked PRs aren't enabled on the repository: tell the user. The branches remain usable as ordinary branches.

## 5. Report

Tell the user:

- the stacks and branches created;
- which branch is now checked out, from `git status`: `gh stack init` may have switched branches;
- that the original branch still has the original commits.

**Do not push, submit or open PRs.** Offer to submit as the next step. That pushes every branch and opens draft PRs with the accepted titles and bodies, so do it only after the user explicitly says yes.

## 6. Submit

`gh stack submit` covers only the stack holding the current branch, so run it once per stack. With more than one remote it refuses to guess: ask which remote, and pass `--remote`.

```bash
git switch <top branch of the stack>
gh stack submit --auto --remote origin
```

`--auto` titles each PR after its branch, and the body is only a footer linking to the GitHub Stacks CLI. Replace both with the accepted text, which also removes the footer:

```bash
gh pr edit <number> --title "<title>" --body-file <file holding the body>
```

Write each body to a file in the scratchpad and pass it with `--body-file`, so the shell doesn't mangle it. Report each PR's number, title and URL.
