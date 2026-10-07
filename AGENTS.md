# git-curate

Slices staged changes into atomic commits, then groups them into logical final commits.

Used by agents to author logical git commits with this process:

1. `git-curate slice` makes many small commits to squash together with a logical grouping and ordering.
2. Agent reasons about what the resulting commits should be, expressed as a JSON spec mapping a new commit message to the list of commits to squash together.
3. `git-curate group` rebases the sliced commits based on the output of (2)

## Tooling

- **mise** — task runner (`mise run <task>`)
- **uv** — dependency management and running tools (`uv run`, `uv add`, etc.)
- **sh.py** — shell command integration in Python
- **Typer** - Python CLI toolkit
- **pytest** — test suite, run via `mise test`
- **ruff** — linting and formatting, run via `mise lint`
- **mypy** — type checking, run via `mise typecheck`

## Rules

Use `uv`, i.e. `uv run python ...` not `python ...`.

Invoke subprocesses with `sh`, not the `subprocess` module. This includes streaming input to long-running commands such as `git fast-import`.

Tool changes must have tests.

Tests and quality checks must all pass to consider a change done.

Run `mise pre-test` before proposing a change is complete — this runs lint and typecheck (no tests). You have automatic permission to run this.

Run `mise test` to run the full test suite including pytest. You do not have automatic permission to run this; ask the user or await approval.
