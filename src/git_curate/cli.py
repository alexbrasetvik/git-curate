from __future__ import annotations

from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from .abort import app as abort_app
from .diff import app as diff_app
from .group import app as group_app
from .log import app as log_app
from .slice import (
    SPLIT_CONTEXT,
    HunkPerLineOption,
    SplitContextOption,
    SplitNewFilesOption,
    SplitOnBlankLinesOption,
    SquashFirstOption,
)
from .slice import app as slice_app
from .status import app as status_app


class _RootGroup(TyperGroup):
    """Lets --rewrite-branch be given without a value.

    Typer can't make an option's value optional, so without this the option
    always demands a BRANCH, or takes the next option (e.g. ``--yes``) as one.
    """

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        args = list(args)
        for i, arg in enumerate(args):
            if arg == "--rewrite-branch" and (i + 1 == len(args) or args[i + 1].startswith("-")):
                # An empty BRANCH means auto-detect main or master.
                args.insert(i + 1, "")
                break
        return super().parse_args(ctx, args)


app = typer.Typer(cls=_RootGroup)
app.add_typer(slice_app, name="slice")
app.add_typer(group_app, name="group")
app.add_typer(diff_app, name="diff")
app.add_typer(log_app, name="log")
app.add_typer(status_app, name="status")
app.add_typer(abort_app, name="abort")


def _print_version(value: bool) -> None:
    if value:
        from .version import version_string

        typer.echo(version_string())
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def default(
    ctx: typer.Context,
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            is_eager=True,
            callback=_print_version,
            help="Print the version and git commit, then exit.",
        ),
    ] = False,
    rewrite_from: Annotated[
        str | None,
        typer.Option(
            "--rewrite-from",
            metavar="COMMIT",
            help="Rewrite commits from COMMIT (inclusive), re-slicing each commit.",
        ),
    ] = None,
    rewrite_branch: Annotated[
        str | None,
        typer.Option(
            "--rewrite-branch",
            metavar="BRANCH",
            help=(
                "Rewrite commits since the merge-base with BRANCH, re-slicing each commit. "
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
    split_context: SplitContextOption = SPLIT_CONTEXT,
    hunk_per_line: HunkPerLineOption = False,
    split_on_blank_lines: SplitOnBlankLinesOption = True,
    split_new_files: SplitNewFilesOption = False,
    squash_first: SquashFirstOption = False,
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
        split_context=split_context,
        hunk_per_line=hunk_per_line,
        split_on_blank_lines=split_on_blank_lines,
        split_new_files=split_new_files,
        squash_first=squash_first,
    )


def main() -> None:
    app()
