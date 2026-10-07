from __future__ import annotations

from typing import Annotated

import typer

from .abort import app as abort_app
from .diff import app as diff_app
from .group import app as group_app
from .log import app as log_app
from .slice import app as slice_app
from .status import app as status_app

app = typer.Typer()
app.add_typer(slice_app, name="slice")
app.add_typer(group_app, name="group")
app.add_typer(diff_app, name="diff")
app.add_typer(log_app, name="log")
app.add_typer(status_app, name="status")
app.add_typer(abort_app, name="abort")


@app.callback(invoke_without_command=True)
def default(
    ctx: typer.Context,
    rewrite_from: Annotated[
        str | None,
        typer.Option(
            "--rewrite-from",
            metavar="COMMIT",
            help="Rewrite commits from COMMIT (inclusive) back into the index and re-slice.",
        ),
    ] = None,
    rewrite_branch: Annotated[
        str | None,
        typer.Option(
            "--rewrite-branch",
            metavar="BRANCH",
            is_flag=False,
            flag_value="",
            help=(
                "Rewrite commits from the merge-base with BRANCH back into the index and re-slice. "
                "Omit BRANCH to auto-detect main or master."
            ),
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompts."),
    ] = False,
    harness: Annotated[
        str | None,
        typer.Option(help="AI harness to invoke for grouping (default: git config git-curate.harness, or claude)."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            help=(
                "Model for the AI harness, e.g. opus or sonnet "
                "(default: git config git-curate.model, or the harness's own default)."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Slice only; do not invoke the AI harness."),
    ] = False,
    all_changes: Annotated[
        bool,
        typer.Option("--all", "-a", help="Stage all changes, including untracked files, before slicing."),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Proceed to AI with existing session, ignoring staged changes."),
    ] = False,
    restart: Annotated[
        bool,
        typer.Option("--restart", help="Abort existing session and re-slice staged changes."),
    ] = False,
) -> None:
    """Slice staged changes and invoke the AI harness to group them into logical commits."""
    if ctx.invoked_subcommand is not None:
        return

    from . import run

    run.curate(
        rewrite_from=rewrite_from,
        rewrite_branch=rewrite_branch,
        yes=yes,
        harness_name=harness,
        model=model,
        dry_run=dry_run,
        all_changes=all_changes,
        resume=resume,
        restart=restart,
    )


def main() -> None:
    app()
