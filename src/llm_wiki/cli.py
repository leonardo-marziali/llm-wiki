"""Typer CLI entry point (`wiki`)."""

import sys

import typer
from typer.core import TyperGroup

from llm_wiki import __version__
from llm_wiki.errors import WikiError


class WikiGroup(TyperGroup):
    """Root group that turns any `errors.WikiError` into `error: <msg>` on
    stderr with exit code 2 (no traceback).

    Because it lives in the app itself, it applies to every command
    registered on `app` -- including under `CliRunner().invoke(app, ...)`.
    Later tasks just register commands with `@app.command()` and raise
    WikiError subclasses; no per-command handling is needed.
    """

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except WikiError as e:
            typer.echo(f"error: {e}", err=True)
            sys.exit(2)


app = typer.Typer(cls=WikiGroup, no_args_is_help=True, add_completion=False)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"wiki {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """LLM-maintained wiki: deterministic core."""


def main() -> None:
    app()
