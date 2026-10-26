"""Typer CLI entry point (`wiki`)."""

import dataclasses
import json
import sys
from pathlib import Path

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


@app.command()
def init(
    path: Path = typer.Argument(..., help="Directory for the new wiki (or an existing wiki)."),
    name: str | None = typer.Option(None, "--name", help="Wiki name (default: the slugged directory name)."),
    profile: str = typer.Option("software", "--profile", help="software or general."),
    description: str = typer.Option("", "--description", help="One line: what belongs in this wiki."),
) -> None:
    """Create a wiki (git repo, schema, skeleton) or register an existing one."""
    from llm_wiki.init import init_wiki

    if profile not in ("software", "general"):
        raise WikiError("--profile must be software or general")
    res = init_wiki(path, name=name, profile=profile, description=description)
    verb = "created wiki" if res.created else "registered existing wiki"
    typer.echo(f"{verb} {res.ref.name} at {res.ref.path}")


@app.command()
def search(
    query: str = typer.Argument(..., help="Search terms."),
    wiki: str | None = typer.Option(None, "--wiki", help="Wiki name or path."),
    all_wikis: bool = typer.Option(False, "--all", help="Search every known wiki."),
    status: str | None = typer.Option(None, "--status", help="Only pages with this status."),
    section: str | None = typer.Option(None, "--section", help="Only pages in this section."),
    top: int = typer.Option(8, "--top", help="Maximum number of hits."),
    fmt: str = typer.Option("text", "--format", help="text or json."),
) -> None:
    """Find relevant pages (BM25 over title, summary, tags, id, body)."""
    from llm_wiki import registry
    from llm_wiki.search import search as run_search

    if all_wikis and wiki is not None:
        raise WikiError("use either --wiki or --all")
    if fmt not in ("text", "json"):
        raise WikiError("--format must be text or json")
    refs = registry.known_wikis() if all_wikis else [registry.resolve(wiki, Path.cwd())]
    hits = run_search(refs, query, top=top, status=status, section=section)
    if fmt == "json":
        typer.echo(json.dumps([dataclasses.asdict(h) for h in hits], ensure_ascii=False, indent=2))
    elif not hits:
        typer.echo("no matches")
    else:
        for h in hits:
            typer.echo(
                f"{h.score:.2f}  {h.wiki}:{h.id}  {h.title} — {h.summary} [{h.status}, {h.last_verified}]"
            )


@app.command()
def lint(
    wiki: str | None = typer.Option(None, "--wiki", help="Wiki name or path."),
    strict: bool = typer.Option(False, "--strict", help="Fail on warnings too."),
    fmt: str = typer.Option("text", "--format", help="text or json."),
    report_file: bool = typer.Option(False, "--report", help="Also write .wiki/reports/lint-<date>.md."),
) -> None:
    """Check the page contract, links, citations and caps."""
    from datetime import date

    from llm_wiki import registry
    from llm_wiki.config import load_config
    from llm_wiki.lint import lint as run_lint
    from llm_wiki.lint import write_report

    if fmt not in ("text", "json"):
        raise WikiError("--format must be text or json")
    ref = registry.resolve(wiki, Path.cwd())
    today = date.today()
    report = run_lint(ref.path, load_config(ref.path), today=today)
    if fmt == "json":
        data = {
            "errors": report.errors,
            "warnings": report.warnings,
            "findings": [dataclasses.asdict(f) for f in report.findings],
        }
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        for f in report.findings:
            typer.echo(f"{f.severity} {f.code} {f.path}: {f.message}")
        typer.echo(f"{report.errors} errors, {report.warnings} warnings")
    if report_file:
        # stderr in json mode keeps stdout parseable
        typer.echo(f"report: {write_report(ref.path, report, today)}", err=fmt == "json")
    if report.errors or (strict and report.warnings):
        raise typer.Exit(1)


@app.command()
def status(
    wiki: str | None = typer.Option(None, "--wiki", help="Wiki name or path."),
    all_wikis: bool = typer.Option(False, "--all", help="One row per known wiki."),
    fmt: str = typer.Option("text", "--format", help="text or json."),
) -> None:
    """Show counts against caps, stale pages, pending sources and recent log."""
    from llm_wiki import registry
    from llm_wiki.status import wiki_status

    if all_wikis and wiki is not None:
        raise WikiError("use either --wiki or --all")
    if fmt not in ("text", "json"):
        raise WikiError("--format must be text or json")
    if all_wikis:
        rows = []
        for ref in registry.known_wikis():
            try:
                rows.append(dataclasses.asdict(wiki_status(ref)))
            except WikiError as e:
                rows.append({"name": ref.name, "path": str(ref.path), "error": str(e)})
        if fmt == "json":
            typer.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for r in rows:
                if "error" in r:
                    typer.echo(f"{r['name']}  {r['path']}  error: {r['error']}")
                else:
                    typer.echo(
                        f"{r['name']}  {r['path']}  sources {r['sources']}/{r['max_sources']}  "
                        f"pages {r['pages']}/{r['max_pages']}  — {r['description']}"
                    )
        return
    st = wiki_status(registry.resolve(wiki, Path.cwd()))
    if fmt == "json":
        typer.echo(json.dumps(dataclasses.asdict(st), ensure_ascii=False, indent=2))
        return
    typer.echo(f"{st.name}  {st.path}")
    if st.description:
        typer.echo(st.description)
    typer.echo(f"sources {st.sources}/{st.max_sources}  pages {st.pages}/{st.max_pages}")
    typer.echo("sections: " + ", ".join(f"{k} {v}" for k, v in st.sections.items()))
    typer.echo(f"stale: {', '.join(st.stale) if st.stale else 'none'}")
    typer.echo(f"inbox {st.inbox}  quarantined {st.quarantined}")
    typer.echo(f"pending: new {st.pending['new']}, changed {st.pending['changed']}, deleted {st.pending['deleted']}")
    if st.last_log:
        typer.echo("recent log:")
        for ln in st.last_log:
            typer.echo(f"  {ln}")


def main() -> None:
    app()
