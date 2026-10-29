"""``wiki lint --fix`` and ``wiki consolidate``: preflight, the jobs, one branch
per run, finalize, the gate.

``maint_job.process_job`` runs each job on the run's branch. The gate then
either merges the squashed branch into the base (accept) or deletes it (reject,
Ctrl-C or a crash): the wiki is as before the run.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import typer

from llm_wiki import gate, gitops
from llm_wiki.archive import archive_candidates, archive_unlinked, archive_waiting
from llm_wiki.claude import claude_binary
from llm_wiki.config import LlmSettings, WikiConfig, load_config, load_llm_settings
from llm_wiki.deep import run_deep
from llm_wiki.errors import STOP, ClaudeError, WikiError
from llm_wiki.gate import LOG, Decision, Undo, plain
from llm_wiki.index import build_indexes
from llm_wiki.intake import TMP_DIR
from llm_wiki.lint import (
    Finding,
    LintReport,
    cited_paths,
    flagged_pages,
    lint,
    write_report,
)
from llm_wiki.maint import RETIRED, Job, Mode, consolidate_jobs, fix_jobs, hint
from llm_wiki.maint_job import JobOutcome, MaintContext, process_job
from llm_wiki.manifest import MANIFEST, Manifest
from llm_wiki.pages import read_page
from llm_wiki.registry import WikiRef
from llm_wiki.session import is_page

_MAX_TARGETS = 5
_COMMAND: dict[Mode, str] = {
    "fix": "wiki lint --fix",
    "consolidate": "wiki consolidate",
}


@dataclass
class MaintReport:
    mode: Mode
    decision: Literal["noop", "accept", "reject"]
    base: str
    branch: str | None = None
    outcomes: list[JobOutcome] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    # retired pages that wait: id -> the files that link them and stay
    waiting: dict[str, list[str]] = field(default_factory=dict)
    left: list[Finding] = field(default_factory=list)  # left for you (Ruling 3)
    left_hints: list[str] = field(default_factory=list)  # one per ``left`` finding
    more: int = 0  # the jobs over MAX_JOBS, left for the next run
    lint_errors: list[Finding] = field(default_factory=list)
    cost_usd: float = 0.0
    stopped: str = ""  # why the run stopped before the last job
    summary: str = ""
    exit_code: int = 0


# --- the jobs ---


def _jobs(
    root: Path, cfg: WikiConfig, mode: Mode, found: LintReport, report: MaintReport
) -> list[Job]:
    if mode == "fix":
        jobs, report.left, report.more = fix_jobs(root, cfg, found)
        report.left_hints = [hint(f, cfg) for f in report.left]
        return jobs
    jobs, left, report.more = consolidate_jobs(root, cfg, found)
    report.left = [f for f, _ in left]
    report.left_hints = [why for _, why in left]
    return jobs


def _lint(
    root: Path,
    cfg: WikiConfig,
    settings: LlmSettings,
    today: date,
    run_dir: Path,
    deep: bool,
) -> tuple[LintReport, float]:
    """The lint report, with the ``--deep`` findings merged in, and their cost.

    ``--deep`` writes the report before any job runs (spec §2, R12).
    """
    found = lint(root, cfg, today=today)
    if not deep:
        return found, 0.0
    try:
        result = run_deep(root, cfg, settings, found, run_dir)
    except ClaudeError as exc:  # what the failed call billed, then exit 2
        typer.echo(f"cost ${exc.cost_usd:.2f}", err=True)
        raise
    merged = LintReport(
        sorted(
            [*found.findings, *result.findings],
            key=lambda f: (f.path, f.code, f.message),
        )
    )
    typer.echo(f"report: {write_report(root, merged, today, result)}")
    return merged, result.cost_usd


def _process(ctx: MaintContext, jobs: list[Job], report: MaintReport) -> None:
    for n, job in enumerate(jobs, 1):
        ctx.n = n
        try:
            out = process_job(ctx, job)
        except STOP as exc:  # this job is rolled back; the rest wait for a rerun
            report.outcomes.append(
                JobOutcome(job, "failed", reason=str(exc), cost_usd=exc.cost_usd)
            )
            report.stopped = str(exc)
            break
        report.outcomes.append(out)


# --- finalize ---


def _targets(job: Job) -> str:
    return ", ".join(job.targets)


def _log_lines(report: MaintReport, today: date) -> list[str]:
    day = today.isoformat()
    lines = []
    for o in report.outcomes:
        if o.status != "done":
            continue
        what = _targets(o.job)
        if report.mode == "consolidate":
            what = f"{o.job.kind} {what}"
        lines.append(f"## [{day}] {report.mode} | {what}")
    lines += [f"## [{day}] archive | {i}" for i in report.archived]
    return lines


def _refresh_manifest(root: Path, pages: list[str]) -> bool:
    """Add each live changed page to ``pages_touched`` of every raw path it
    cites; True when the manifest changed.
    """
    manifest = Manifest.load(root)
    changed = False
    for rel in pages:
        page = read_page(root, root / rel)
        if page.fm is None or page.fm.status in RETIRED:
            continue
        for raw in sorted(cited_paths(page)):
            entry = manifest.entries.get(raw)
            if entry is None or page.fm.id in entry.pages_touched:
                continue
            touched = sorted({*entry.pages_touched, page.fm.id})
            manifest.record(entry.model_copy(update={"pages_touched": touched}))
            changed = True
    if changed:
        manifest.save(root)
    return changed


def _squash_message(report: MaintReport) -> str:
    done = [o for o in report.outcomes if o.status == "done"]
    n = len(done)
    targets = [_targets(o.job) for o in done]
    if len(targets) > _MAX_TARGETS:
        targets = [*targets[:_MAX_TARGETS], "…"]
    head = f"{_COMMAND[report.mode]}: {n} job{'' if n == 1 else 's'}"
    if report.archived:
        head += f", {len(report.archived)} archived"
    return f"{head} ({', '.join(targets)})" if targets else head


def _finalize(ctx: MaintContext, base: str, report: MaintReport) -> None:
    """Archive, indexes, manifest, log, lint, then one squashed commit."""
    root, cfg = Path(ctx.root), ctx.cfg
    files = [LOG]
    if ctx.mode == "consolidate":  # after every job: a page relinked now goes too
        manifest = Manifest.load(root)
        report.archived = archive_unlinked(root, manifest)
        report.waiting = archive_waiting(root)
        if report.archived:
            manifest.save(root)
            files.append(MANIFEST)
    indexes = build_indexes(root, cfg)
    live = [
        p
        for p in gitops.changed_since(root, base)
        if is_page(p) and (root / p).is_file()
    ]
    files += [p.relative_to(root).as_posix() for p in indexes]
    if _refresh_manifest(root, live):
        files.append(MANIFEST)
    gate.append_log(root, _log_lines(report, ctx.today))
    gitops.stage_commit(root, files, f"{ctx.mode}(finalize)", force=files)
    changed = gitops.changed_since(root, base)
    pages = [p for p in changed if is_page(p) and (root / p).is_file()]
    # wiki-wide checks (system files, caps, duplicates) always run
    found = lint(root, cfg, only=[Path(p) for p in pages], today=ctx.today)
    report.lint_errors = [f for f in found.findings if f.severity == "error"]
    gitops.reset_soft(root, base)
    if changed:
        gitops.stage_commit(root, changed, _squash_message(report))


# --- the summary ---


def _ids(ids: set[str], reviewed: set[str]) -> str:
    return ", ".join(f"{i} !" if i in reviewed else i for i in sorted(ids)) or "none"


def _waiting_lines(report: MaintReport) -> list[str]:
    return [
        f"waiting: {i} (linked from {', '.join(rels)})"
        for i, rels in report.waiting.items()
    ]


def _left_lines(report: MaintReport) -> list[str]:
    if not report.left:
        return []
    lines = ["left for you:"]
    for f, why in zip(report.left, report.left_hints, strict=True):
        where = f" {f.path}" if f.path else ""
        lines.append(f"  {f.code}{where}: {why}")
    return lines


def summarize_maint(report: MaintReport) -> str:
    outs = report.outcomes
    created = {i for o in outs for i in o.pages_created}
    updated = {i for o in outs for i in o.pages_updated} - created
    reviewed = {i for o in outs for i in o.reviewed_touched}
    n = len(outs)
    head = f"{_COMMAND[report.mode]}: {n} job{'' if n == 1 else 's'}"
    lines = [f"{head} on {report.branch} (base {report.base})"]
    for o in outs:
        detail = o.summary if o.status == "done" else o.reason
        lines.append(
            f"  {o.status:<7} {o.job.kind} {_targets(o.job)}"
            + (f": {detail}" if detail else "")
        )
    lines += [
        f"created: {_ids(created, reviewed)}",
        f"updated: {_ids(updated, reviewed)}",
    ]
    if reviewed:
        lines.append("! = touched a reviewed page: check it before accepting")
    if report.mode == "consolidate":
        lines.append(f"archived: {', '.join(report.archived) or 'none'}")
        lines += _waiting_lines(report)
    reverted = sorted({p for o in outs for p in o.reverted})
    if reverted:
        lines.append(f"reverted: {', '.join(reverted)}")
    lines += _left_lines(report)
    if report.more:
        lines.append(f"{report.more} more jobs: run again")
    n_err = len(report.lint_errors)
    lines.append(f"lint: {n_err} error{'' if n_err == 1 else 's'}")
    lines += [
        f"  {f.code} {f.path}: {f.message}" if f.path else f"  {f.code}: {f.message}"
        for f in report.lint_errors
    ]
    if report.stopped:
        lines.append(f"stopped early: {report.stopped}; the remaining jobs wait")
    lines.append(f"cost ${report.cost_usd:.2f}")
    # summaries, reasons and findings carry model text: never a terminal escape
    return "\n".join(plain(line) for line in lines)


# --- the run ---


def run_maintenance(
    ref: WikiRef,
    *,
    mode: Mode,
    deep: bool = False,
    yes: bool,
    interactive: bool,
    today: date | None = None,
    decide: Callable[[str], Decision] | None = None,
    confirm: Callable[[str], bool] | None = None,
) -> MaintReport:
    """Run the fix or consolidate jobs of the wiki on a branch, then gate it.

    Everything that can refuse the run does so before any paid call. An
    interactive accept of a run whose lint failed asks ``confirm`` once more
    (default: no).
    """
    root = Path(ref.path)
    cfg = load_config(root)
    settings = load_llm_settings(root)
    claude_binary()
    if not (interactive or yes):
        raise WikiError("the gate needs a terminal; pass --yes")
    base = gate.preflight_git(root, op=mode)
    today = today or date.today()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = gate.fresh(root / ".wiki" / "runs" / f"{mode}-{stamp}")
    report = MaintReport(mode=mode, decision="noop", base=base)
    found, report.cost_usd = _lint(root, cfg, settings, today, run_dir, deep)
    jobs = _jobs(root, cfg, mode, found, report)
    # finalize's index rebuild is the fix for not-in-index (Ruling 3)
    if (
        not jobs
        and not any(f.code == "not-in-index" for f in found.findings)
        and not (mode == "consolidate" and archive_candidates(root))
    ):
        if mode == "consolidate":
            report.waiting = archive_waiting(root)
        lines = [
            f"nothing to {'fix' if mode == 'fix' else 'consolidate'}",
            *_waiting_lines(report),
            *_left_lines(report),
        ]
        if report.cost_usd > 0:  # the --deep pass ran
            lines.append(f"cost ${report.cost_usd:.2f}")
        report.summary = "\n".join(plain(line) for line in lines)
        typer.echo(report.summary)
        return report
    undo = Undo(root, gate.fresh(root / TMP_DIR / f"{mode}-{run_dir.name}"), run_dir)
    try:
        report.branch = gate.fresh_branch(root, f"wiki/{mode}-{today:%Y%m%d}")
        gitops.switch(root, report.branch, create=True)
        ctx = MaintContext(
            root=root,
            cfg=cfg,
            settings=settings,
            today=today,
            run_dir=run_dir,
            n=0,
            mode=mode,
            exclude=flagged_pages(found),
        )
        _process(ctx, jobs, report)
        report.cost_usd += sum(o.cost_usd for o in report.outcomes)
        _finalize(ctx, base, report)
        report.summary = summarize_maint(report)
        typer.echo(report.summary)
        report.decision = gate.decide(
            root,
            base,
            report.branch,
            report.summary,
            len(report.lint_errors),
            yes,
            decide,
            confirm,
        )
    except BaseException:  # Ctrl-C or a crash: the wiki goes back as it was
        gate.rollback(undo, base, report.branch)
        raise
    if report.decision == "reject":
        gate.rollback(undo, base, report.branch)
    else:
        gate.accept(root, base, report.branch, undo)
    _announce(report, base, yes)
    return report


def _announce(report: MaintReport, base: str, yes: bool) -> None:
    """Print the gate's outcome; a --yes reject exits 1."""
    if report.decision == "accept":
        typer.echo(f"accepted: {report.branch} merged into {base}")
    elif yes:
        report.exit_code = 1
        typer.echo(
            f"rejected: lint found {len(report.lint_errors)} errors and --yes "
            "accepts only a clean run"
        )
    else:
        typer.echo(f"rejected: {report.branch} deleted")
