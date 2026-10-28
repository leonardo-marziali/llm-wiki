"""Typer CLI entry point (`wiki`)."""

import dataclasses
import json
import sys
from pathlib import Path

import typer
from typer.core import TyperGroup

from llm_wiki import __version__
from llm_wiki.errors import WikiError

_WIKI_HELP = "Wiki name or path."
_FORMAT_HELP = "text or json."


def _check_format(fmt: str) -> None:
    if fmt not in ("text", "json"):
        raise WikiError("--format must be text or json")


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
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Small, capped, LLM-maintained wikis for coding agents."""


@app.command()
def init(
    path: Path = typer.Argument(
        ..., help="Directory for the new wiki (or an existing wiki)."
    ),
    name: str | None = typer.Option(
        None, "--name", help="Wiki name (default: the slugged directory name)."
    ),
    profile: str = typer.Option("software", "--profile", help="software or general."),
    description: str = typer.Option(
        "", "--description", help="One line: what belongs in this wiki."
    ),
) -> None:
    """Create a wiki (own git repo, or a folder in an existing repo) or register an
    existing one.
    """
    from llm_wiki.init import init_wiki

    if profile not in ("software", "general"):
        raise WikiError("--profile must be software or general")
    res = init_wiki(path, name=name, profile=profile, description=description)
    verb = "created wiki" if res.created else "registered existing wiki"
    msg = f"{verb} {res.ref.name} at {res.ref.path}"
    if res.host_repo is not None:
        msg += (
            f" inside existing git repository {res.host_repo}"
            " (not committed — review and commit it there)"
        )
    typer.echo(msg)


@app.command()
def search(
    query: str = typer.Argument(..., help="Search terms."),
    wiki: str | None = typer.Option(None, "--wiki", help=_WIKI_HELP),
    all_wikis: bool = typer.Option(False, "--all", help="Search every known wiki."),
    status: str | None = typer.Option(
        None, "--status", help="Only pages with this status."
    ),
    section: str | None = typer.Option(
        None, "--section", help="Only pages in this section."
    ),
    top: int = typer.Option(8, "--top", help="Maximum number of hits."),
    fmt: str = typer.Option("text", "--format", help=_FORMAT_HELP),
) -> None:
    """Find relevant pages (BM25 over title, summary, tags, id, body)."""
    from llm_wiki import registry
    from llm_wiki.search import search as run_search

    if all_wikis and wiki is not None:
        raise WikiError("use either --wiki or --all")
    _check_format(fmt)
    if top < 1:
        raise WikiError("--top must be >= 1")
    refs = registry.known_wikis() if all_wikis else [registry.resolve(wiki, Path.cwd())]
    hits = run_search(refs, query, top=top, status=status, section=section)
    if fmt == "json":
        typer.echo(
            json.dumps(
                [dataclasses.asdict(h) for h in hits], ensure_ascii=False, indent=2
            )
        )
    elif not hits:
        typer.echo("no matches")
    else:
        for h in hits:
            typer.echo(
                f"{h.score:.2f}  {h.wiki}:{h.id}  {h.title} — {h.summary} "
                f"[{h.status}, {h.last_verified}]"
            )


@app.command()
def lint(
    wiki: str | None = typer.Option(None, "--wiki", help=_WIKI_HELP),
    strict: bool = typer.Option(False, "--strict", help="Fail on warnings too."),
    fmt: str = typer.Option("text", "--format", help=_FORMAT_HELP),
    report_file: bool = typer.Option(
        False, "--report", help="Also write .wiki/reports/lint-<date>.md."
    ),
) -> None:
    """Check the page contract, links, citations and caps."""
    from datetime import date

    from llm_wiki import registry
    from llm_wiki.config import load_config
    from llm_wiki.lint import lint as run_lint
    from llm_wiki.lint import write_report

    _check_format(fmt)
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
        typer.echo(
            f"report: {write_report(ref.path, report, today)}", err=fmt == "json"
        )
    if report.errors or (strict and report.warnings):
        raise typer.Exit(1)


@app.command()
def status(
    wiki: str | None = typer.Option(None, "--wiki", help=_WIKI_HELP),
    all_wikis: bool = typer.Option(False, "--all", help="One row per known wiki."),
    fmt: str = typer.Option("text", "--format", help=_FORMAT_HELP),
) -> None:
    """Show counts against caps, stale pages, pending sources and recent log."""
    from llm_wiki import registry
    from llm_wiki.status import wiki_status

    if all_wikis and wiki is not None:
        raise WikiError("use either --wiki or --all")
    _check_format(fmt)
    if all_wikis:
        rows = [_status_row(ref) for ref in registry.known_wikis()]
        if fmt == "json":
            typer.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            _echo_status_rows(rows)
        return
    st = wiki_status(registry.resolve(wiki, Path.cwd()))
    if fmt == "json":
        typer.echo(json.dumps(dataclasses.asdict(st), ensure_ascii=False, indent=2))
    else:
        _echo_status(st)


def _status_row(ref) -> dict:
    """One `status --all` row; a wiki that fails to load becomes an error row."""
    from llm_wiki.status import wiki_status

    try:
        return dataclasses.asdict(wiki_status(ref))
    except Exception as e:  # noqa: BLE001  one broken wiki must not hide the others
        return {
            "name": ref.name,
            "path": str(ref.path),
            "error": str(e) or type(e).__name__,
        }


def _echo_status_rows(rows: list[dict]) -> None:
    for r in rows:
        if "error" in r:
            typer.echo(f"{r['name']}  {r['path']}  error: {r['error']}")
        else:
            typer.echo(
                f"{r['name']}  {r['path']}  "
                f"sources {r['sources']}/{r['max_sources']}  "
                f"pages {r['pages']}/{r['max_pages']}  — {r['description']}"
            )


def _echo_status(st) -> None:
    typer.echo(f"{st.name}  {st.path}")
    if st.description:
        typer.echo(st.description)
    typer.echo(
        f"sources {st.sources}/{st.max_sources}  pages {st.pages}/{st.max_pages}"
    )
    typer.echo("sections: " + ", ".join(f"{k} {v}" for k, v in st.sections.items()))
    typer.echo(f"stale: {', '.join(st.stale) if st.stale else 'none'}")
    if st.near_cap:
        typer.echo("near cap: " + "; ".join(st.near_cap))
    typer.echo(f"inbox {st.inbox}  quarantined {st.quarantined}")
    typer.echo(
        f"pending: new {st.pending['new']}, changed {st.pending['changed']}, "
        f"deleted {st.pending['deleted']}"
    )
    if st.last_log:
        typer.echo("recent log:")
        for ln in st.last_log:
            typer.echo(f"  {ln}")


@app.command()
def doctor(
    wiki: str | None = typer.Option(None, "--wiki", help=_WIKI_HELP),
    no_probe: bool = typer.Option(
        False, "--no-probe", help="Skip the paid claude -p probes."
    ),
    fmt: str = typer.Option("text", "--format", help=_FORMAT_HELP),
) -> None:
    """Check claude, git and optional tools (exit 1 if a check fails)."""
    from llm_wiki import registry
    from llm_wiki.config import LlmSettings, load_llm_settings
    from llm_wiki.doctor import Check, run_doctor
    from llm_wiki.errors import ConfigError, WikiNotFound

    _check_format(fmt)
    root = None
    try:
        root = registry.resolve(wiki, Path.cwd()).path
    except WikiNotFound:
        if wiki is not None:
            raise
    config_checks: list[Check] = []
    try:
        settings = load_llm_settings(root)
    except (
        ConfigError
    ) as e:  # a bad config is one more finding, not a reason to skip the rest
        config_checks.append(
            Check("config", "fail", f"{e}; using default llm settings")
        )
        settings = LlmSettings()
    checks = config_checks + run_doctor(
        settings, probe=not no_probe, config_ok=not config_checks
    )
    if fmt == "json":
        typer.echo(
            json.dumps(
                [dataclasses.asdict(c) for c in checks], ensure_ascii=False, indent=2
            )
        )
    else:
        width = max([16, *(len(c.name) for c in checks)])
        for c in checks:
            typer.echo(f"{c.status:<4}  {c.name:<{width}}  {c.detail}")
    if any(c.status == "fail" for c in checks):
        raise typer.Exit(1)


@app.command()
def ingest(
    paths: list[Path] | None = typer.Argument(
        None, help="Files or folders to copy into raw/ and ingest."
    ),
    note: str | None = typer.Option(
        None,
        "--note",
        help="Save this text as raw/notes/<date>-<slug>.md and ingest it.",
    ),
    trust: str | None = typer.Option(
        None,
        "--trust",
        help="Release this quarantined file (an injection false positive).",
    ),
    wiki: str | None = typer.Option(None, "--wiki", help=_WIKI_HELP),
    yes: bool = typer.Option(
        False, "--yes", help="Accept without asking when lint passes."
    ),
) -> None:
    """Ingest the inbox, new paths and every new, changed or deleted raw source,
    on a branch, then accept or reject the result at one gate.
    """
    from llm_wiki import registry
    from llm_wiki.ingest_run import run_ingest

    ref = registry.resolve(wiki, Path.cwd())
    try:
        report = run_ingest(
            ref,
            paths=paths or None,
            note=note,
            trust=trust,
            yes=yes,
            interactive=sys.stdin.isatty(),
        )
    except KeyboardInterrupt:
        typer.echo("interrupted: the ingest was rolled back", err=True)
        raise typer.Exit(130) from None
    if report.exit_code:
        raise typer.Exit(report.exit_code)


skill_app = typer.Typer(no_args_is_help=True, help="Manage the wiki-lookup skill.")
app.add_typer(skill_app, name="skill")


@skill_app.command("install")
def skill_install(
    project: Path | None = typer.Option(
        None,
        "--project",
        help="Install into DIR/.claude/skills instead of the user-level skills dir.",
    ),
    pointer: bool = typer.Option(
        False, "--pointer", help="Also write the wiki pointer block into DIR/CLAUDE.md."
    ),
    wiki: str | None = typer.Option(None, "--wiki", help=_WIKI_HELP),
    force: bool = typer.Option(
        False, "--force", help="Replace a wiki-lookup skill llm-wiki did not write."
    ),
) -> None:
    """Install the read-only wiki-lookup skill (idempotent)."""
    from llm_wiki import registry, skill

    if pointer and project is None:
        raise WikiError("--pointer needs --project DIR")
    ref = registry.resolve(wiki, project) if pointer and project else None
    target = project / ".claude" / "skills" if project else skill.user_skills_dir()
    path, changed = skill.install_skill(target, force=force)
    state = "installed" if changed else "already installed"
    typer.echo(f"{skill.SKILL_NAME} {__version__} {state} at {path}")
    if ref is not None and project is not None:
        claude_md, _ = skill.write_pointer(project, ref.name)
        typer.echo(f"pointer to wiki {ref.name} written to {claude_md}")


def main() -> None:
    app()
