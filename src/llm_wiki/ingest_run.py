"""``wiki ingest``: preflight, the work list, one branch per run, finalize, the gate.

``ingest.process_source`` handles each source on the run's branch. The gate then
either merges the squashed branch into the base (accept) or deletes it and puts
every moved file back (reject, Ctrl-C or a crash): the wiki is as before the run.
"""

import json
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import typer

from llm_wiki import gitops
from llm_wiki.claude import claude_binary
from llm_wiki.config import WikiConfig, load_config, load_llm_settings
from llm_wiki.errors import WikiError
from llm_wiki.index import build_indexes
from llm_wiki.ingest import (
    STOP,
    RunContext,
    SourceOutcome,
    is_page,
    process_deleted,
    process_source,
)
from llm_wiki.init import slug
from llm_wiki.intake import (
    QUARANTINE_DIR,
    TMP_DIR,
    Prepared,
    check_paths,
    copy_paths,
    free_path,
    inbox_home,
    intake_inbox,
    move_into_raw,
    prepare_source,
    trust,
    write_note,
)
from llm_wiki.lint import Finding, lint
from llm_wiki.manifest import MANIFEST, Manifest, sha256_file
from llm_wiki.pages import read_page
from llm_wiki.registry import WikiRef

Decision = Literal["accept", "reject"]
BRANCH_PREFIX = "wiki/ingest-"
LOG = "wiki/log.md"
_DIRT_OK = ("inbox/", "raw/")  # what a run picks up; anything else must be committed
_LOG_OP = {
    "ingested": "ingest",
    "quarantined": "quarantine",
    "failed": "fail",
    "refused": "refuse",
    "deleted": "delete",
}
_MAX_TITLES = 5
_HINT = "run `wiki consolidate` or start a new wiki"
_UNVERIFIED = re.compile(r"^\s*>\s*\[!unverified\]", re.IGNORECASE | re.MULTILINE)
# terminal escapes: CSI, OSC (with its payload) and the two-character ones
_ESCAPE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]?"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _plain(text: str) -> str:
    """``text`` without terminal escapes; other control characters become spaces."""
    return _CONTROL.sub(" ", _ESCAPE.sub("", text))


@dataclass
class IngestReport:
    decision: Literal["noop", "accept", "reject"]
    base: str
    branch: str | None = None
    outcomes: list[SourceOutcome] = field(default_factory=list)
    lint_errors: list[Finding] = field(default_factory=list)
    redactions: dict[str, int] = field(default_factory=dict)
    unverified: int = 0
    low_confidence: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    stopped: str = ""  # why the run stopped before the last source
    summary: str = ""
    exit_code: int = 0


@dataclass
class _Item:
    rel: str
    origin: Path | None = None  # an inbox file, moved into raw/ when its turn comes
    made: bool = False  # the run wrote it into raw/ (--note, copied paths)


def _fresh(path: Path) -> Path:
    out, n = path, 2
    while out.exists():
        out = path.with_name(f"{path.name}-{n}")
        n += 1
    return out


def _under(rel: str, folder: str) -> bool:
    return rel == folder or rel.startswith(folder + "/")


class _Undo:
    """What a reject must put back: copies of moved and pre-run files, held under
    .wiki/tmp/ (listed in its held.json), and the quarantine entries the run did
    not make. Only paths the run owns are reset: any other file under inbox/ or
    raw/ is the user's and is never touched.
    """

    def __init__(self, root: Path, hold: Path, run_dir: Path):
        self.root, self.hold = root, hold
        self.skip = [p.relative_to(root).as_posix() for p in (hold, run_dir)]
        self.entries: list[tuple[Path | None, str]] = []  # (copy or None: absent, rel)
        self.owned: set[str] = set()  # inbox/ and raw/ paths this run changes
        qdir = root / QUARANTINE_DIR
        self.quarantined = {p.name for p in qdir.iterdir()} if qdir.is_dir() else set()

    @property
    def index(self) -> Path:
        return self.hold / "held.json"

    def own(self, rel: str) -> None:
        self.owned.add(rel)

    def keep(self, rel: str, dest: str | None = None) -> None:
        """Hold ``rel`` now; a rollback writes it to ``dest`` (default: ``rel``)."""
        src = self.root / rel
        copy = None
        self.hold.mkdir(parents=True, exist_ok=True)
        if src.is_file():
            copy = self.hold / str(len(self.entries))
            shutil.copy2(src, copy)
        self.entries.append((copy, dest or rel))
        self.owned |= {rel, dest or rel}
        held = [
            {"copy": c.name if c else None, "dest": d} for c, d in self.entries
        ]  # for a rollback finished by hand
        self.index.write_text(json.dumps(held, indent=2) + "\n", encoding="utf-8")

    def discard(self) -> None:
        shutil.rmtree(self.hold, ignore_errors=True)

    def _ours(self, rel: str) -> bool:
        if any(_under(rel, s) for s in self.skip):
            return False
        return rel in self.owned or not rel.startswith(_DIRT_OK)

    def rollback(self, base: str, branch: str | None) -> None:
        """Every step runs even when an earlier one fails; the first error then
        propagates, and the held copies' index stays for a rollback by hand.
        """
        root = self.root
        try:
            dirty = [p for p in gitops.changed_paths(root) if self._ours(p)]
            # a gitignored file is reset only when the run owns it
            ignored = gitops.changed_paths(root, ignored=True)
            dirty += [p for p in ignored if p in self.owned]
            gitops.restore(root, dirty)  # nothing the run left half-done survives
        finally:
            try:
                if branch is not None:
                    if gitops.current_branch(root) != base:
                        gitops.switch(root, base)
                    gitops.delete_branch(root, branch)
            finally:
                self._put_back()
        self.discard()

    def _put_back(self) -> None:
        """Drop the run's quarantine entries; move the held copies back."""
        root = self.root
        qdir = root / QUARANTINE_DIR
        if qdir.is_dir():
            for p in qdir.iterdir():
                if p.name not in self.quarantined and p.is_file():
                    p.unlink()  # its source goes back to where it came from
        for copy, rel in self.entries:
            dest = root / rel
            if copy is None:
                dest.unlink(missing_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(copy), dest)


# --- preflight ---


def _preflight_git(root: Path) -> str:
    """The base branch, after the branch and clean-tree rules."""
    top = gitops.enclosing_repo(root)
    if top is None:
        raise WikiError(f"{root} is not inside a git repository")
    own = top == Path(root).resolve()
    branch = gitops.current_branch(root)
    if branch is None:
        raise WikiError("HEAD is detached; switch to a branch first")
    if branch.startswith(BRANCH_PREFIX):
        base = "main" if own else "<base branch>"
        raise WikiError(
            f"a previous ingest left {branch} checked out; run "
            f"`git switch {base} && git branch -D {branch}` first"
        )
    if own and branch != "main":
        raise WikiError(f"ingest runs on main; the wiki is on {branch}")
    try:
        gitops.git(root, "rev-parse", "--verify", "-q", "HEAD")
    except WikiError:
        raise WikiError("the repository has no commits yet") from None
    staged = gitops.staged_paths(root)
    if staged:
        raise WikiError(
            "the repository has staged changes ("
            + ", ".join(staged[:5])
            + "); commit or unstage them first"
        )
    dirty = [p for p in gitops.changed_paths(root) if not p.startswith(_DIRT_OK)]
    if dirty:
        raise WikiError(
            "the wiki has uncommitted changes outside inbox/ and raw/ ("
            + ", ".join(dirty[:5])
            + "); commit or discard them first"
        )
    return branch


# --- the work list ---


def needs_work(manifest: Manifest, root: Path, rel: str) -> bool:
    """True when raw source ``rel`` is new, changed, deleted or unfinished.

    An ingested source with the same sha is done; a refused one is retried only
    once its file changes.
    """
    entry = manifest.entries.get(rel)
    path = Path(root) / rel
    if not path.is_file():  # a refused file sent back to the inbox is no deletion
        return (
            entry is not None
            and entry.status not in ("deleted", "quarantined")
            and not (entry.status == "failed" and "refused" in entry.flags)
        )
    if entry is None or entry.status == "deleted":
        return True
    if entry.sha256 != sha256_file(path):
        return True
    if entry.status == "ingested":
        return False
    return not (entry.status == "failed" and "refused" in entry.flags)


def _work_list(
    root: Path,
    undo: _Undo,
    paths: list[Path] | None,
    note: str | None,
    trust_name: str | None,
    today: date,
) -> list[_Item]:
    """--trust, --note, paths, the inbox, then raw/'s new, changed, deleted and
    failed sources, each once.
    """
    items: list[_Item] = []
    if trust_name:
        name = trust_name.removesuffix(".reason.json")
        if Path(name).name == name:  # trust() itself refuses anything else
            for held in (name, f"{name}.reason.json"):
                undo.keep((QUARANTINE_DIR / held).as_posix())
        rel = trust(root, trust_name)
        undo.own(rel)  # a raw path the run made: a rollback takes it back
        # the scan sees a released source with an unchanged sha as done
        items.append(_Item(rel))
    if note is not None:
        rel = write_note(root, note, today)
        undo.keep(rel, dest=f"inbox/notes/{Path(rel).name}")
        items.append(_Item(rel, made=True))
    manifest = Manifest.load(root)
    if paths:
        made: set[str] = set()

        def on_copy(rel: str) -> None:
            undo.own(rel)
            made.add(rel)

        copied = copy_paths(root, paths, on_copy=on_copy)
        items += [
            _Item(rel, made=rel in made)
            for rel in copied
            if needs_work(manifest, root, rel)
        ]
    for src, rel in intake_inbox(root):
        skip = _inbox_skip(root, manifest, src, rel)
        if skip:
            typer.echo(skip)
        else:
            items.append(_Item(rel, origin=src))
    scan = manifest.scan(root)
    items += [_Item(rel) for rel in [*scan.new, *scan.changed]]
    items += [_Item(rel) for rel in scan.deleted if needs_work(manifest, root, rel)]
    items += [_Item(rel) for rel in scan.unchanged if needs_work(manifest, root, rel)]
    seen: set[str] = set()
    out = []
    for item in items:
        if item.rel not in seen:
            seen.add(item.rel)
            undo.own(item.rel)
            out.append(item)
    return out


def _inbox_skip(root: Path, manifest: Manifest, src: Path, rel: str) -> str | None:
    """Why the inbox file src, bound for rel, stays in inbox/; None to take it."""
    target = root / rel
    entry = manifest.entries.get(rel)
    name = src.relative_to(root).as_posix()
    if (
        not target.exists()
        and entry is not None
        and entry.status == "failed"
        and "refused" in entry.flags
        and entry.sha256 == sha256_file(src)
    ):
        return f"refused before: {name} is unchanged; left in inbox/"
    if (
        target.is_file()
        and not needs_work(manifest, root, rel)
        and sha256_file(src) == sha256_file(target)
    ):
        return f"unchanged: {name} matches {rel}; left in inbox/"
    return None


def _within_source_cap(
    root: Path, cfg: WikiConfig, items: list[_Item], undo: _Undo
) -> list[_Item]:
    """Drop the new sources that would take the wiki past ``max_sources``,
    before any paid call; changed and deleted sources always run.

    A refused inbox file stays in inbox/; a file the run wrote into raw/ (a
    note, a copied path) moves to inbox/; one the user put in raw/ stays there.
    """
    manifest = Manifest.load(root)
    cap = cfg.effective_caps().max_sources
    room = cap - manifest.live()
    out: list[_Item] = []
    for item in items:
        new = not manifest.is_live(item.rel) and (
            item.origin is not None or (root / item.rel).is_file()
        )
        if not new:
            out.append(item)
            continue
        if room > 0:
            room -= 1
            out.append(item)
            continue
        if item.origin is not None:
            where = item.origin.relative_to(root).as_posix()
        elif item.made:
            dest = inbox_home(root, item.rel)
            undo.own(dest.relative_to(root).as_posix())
            move_into_raw(root, root / item.rel, dest.relative_to(root).as_posix())
            where = dest.relative_to(root).as_posix()
        else:
            where = item.rel
        typer.echo(
            f"refused: {item.rel}: a new source would exceed the cap of {cap} "
            f"sources; {_HINT}. It stays in {where}"
        )
    return out


def _branch_name(root: Path, today: date, first: str) -> str:
    stem = slug(Path(first).stem)[:40].strip("-") or "source"
    return _fresh_branch(root, f"{BRANCH_PREFIX}{today:%Y%m%d}-{stem}")


def _fresh_branch(root: Path, name: str) -> str:
    out, n = name, 2
    while gitops.branch_exists(root, out):
        out = f"{name}-{n}"
        n += 1
    return out


# --- processing and finalize ---


def _prepare(ctx: RunContext, item: _Item, undo: _Undo) -> Prepared:
    root = Path(ctx.root)
    prepared = prepare_source(root, ctx.cfg, ctx.manifest, item.rel, ctx.today)
    if prepared.kind == "changed" or prepared.status == "refused":
        # a refused or failed new version goes back where the user sees it,
        # the old version comes back, and the next run retries it (R13, R25);
        # a refused file that is never committed goes there too (R26)
        origin = item.origin
        prepared.origin = free_path(origin) if origin else inbox_home(root, item.rel)
        undo.own(prepared.origin.relative_to(root).as_posix())
    return prepared


def _process(
    ctx: RunContext, items: list[_Item], report: IngestReport, undo: _Undo
) -> None:
    root = Path(ctx.root)
    redactions: Counter[str] = Counter()
    for n, item in enumerate(items, 1):
        ctx.n = n
        if item.origin is not None:
            move_into_raw(root, item.origin, item.rel)
        if not needs_work(ctx.manifest, root, item.rel):
            continue  # an inbox copy of a source the wiki already holds
        try:
            if (root / item.rel).exists():
                prepared = _prepare(ctx, item, undo)
                redactions.update(prepared.redactions)
                out = process_source(ctx, prepared)
            else:
                out = process_deleted(ctx, item.rel)
        except STOP as exc:  # this source is rolled back; the rest stay pending
            notes = getattr(exc, "__notes__", [])  # e.g. where a new version went
            report.outcomes.append(
                SourceOutcome(
                    rel=item.rel,
                    title=item.rel,
                    status="failed",
                    reason="; ".join([str(exc), *notes]),
                    cost_usd=exc.cost_usd,
                )
            )
            report.stopped = str(exc)
            break
        redactions.update(out.redactions)
        report.outcomes.append(out)
    report.redactions = dict(redactions)
    report.cost_usd = sum(o.cost_usd for o in report.outcomes)


def _log_lines(outcomes: list[SourceOutcome], today: date) -> list[str]:
    lines = []
    for o in outcomes:
        text = " ".join(_plain(o.title).split()) if o.status == "ingested" else o.rel
        lines.append(f"## [{today.isoformat()}] {_LOG_OP[o.status]} | {text or o.rel}")
    return lines


def _append_log(root: Path, lines: list[str]) -> None:
    if not lines:
        return
    path = root / LOG
    text = path.read_text(encoding="utf-8") if path.is_file() else "# Log\n"
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "\n".join(lines) + "\n", encoding="utf-8")


def _finalize(ctx: RunContext, base: str, report: IngestReport) -> None:
    """Indexes, log, manifest, lint, then one squashed commit on the branch."""
    root, cfg = Path(ctx.root), ctx.cfg
    indexes = build_indexes(root, cfg)
    _append_log(root, _log_lines(report.outcomes, ctx.today))
    ctx.manifest.save(root)
    files = [p.relative_to(root).as_posix() for p in indexes] + [LOG, MANIFEST]
    gitops.stage_commit(root, files, "ingest(finalize)", force=files)
    changed = gitops.changed_since(root, base)
    pages = [p for p in changed if is_page(p) and (root / p).is_file()]
    # wiki-wide checks (system files, caps, duplicates) always run
    found = lint(root, cfg, only=[Path(p) for p in pages], today=ctx.today)
    report.lint_errors = [f for f in found.findings if f.severity == "error"]
    _page_stats(root, pages, report)
    gitops.reset_soft(root, base)
    if changed:
        gitops.stage_commit(root, changed, _squash_message(report.outcomes))


def _page_stats(root: Path, pages: list[str], report: IngestReport) -> None:
    for rel in pages:
        page = read_page(root, root / rel)
        report.unverified += len(_UNVERIFIED.findall(page.body))
        if page.fm is not None and page.fm.confidence == "low":
            report.low_confidence.append(page.fm.id)


def _squash_message(outcomes: list[SourceOutcome]) -> str:
    n = len(outcomes)
    titles = [" ".join(_plain(o.title).split()) for o in outcomes]
    if len(titles) > _MAX_TITLES:
        titles = [*titles[:_MAX_TITLES], "…"]
    noun = "source" if n == 1 else "sources"
    return f"wiki ingest: {n} {noun} ({', '.join(titles)})"


# --- the gate ---


def _ids(ids: set[str], reviewed: set[str]) -> str:
    return ", ".join(f"{i} !" if i in reviewed else i for i in sorted(ids)) or "none"


def summarize(report: IngestReport) -> str:
    outs = report.outcomes
    created = {i for o in outs for i in o.pages_created}
    updated = {i for o in outs for i in o.pages_updated} - created
    reviewed = {i for o in outs for i in o.reviewed_touched}
    noun = "source" if len(outs) == 1 else "sources"
    lines = [f"wiki ingest: {len(outs)} {noun} on {report.branch} (base {report.base})"]
    for o in outs:
        detail = o.title if o.status == "ingested" else o.reason
        lines.append(f"  {o.status:<11} {o.rel}" + (f": {detail}" if detail else ""))
    lines += [
        f"created: {_ids(created, reviewed)}",
        f"updated: {_ids(updated, reviewed)}",
    ]
    if reviewed:
        lines.append("! = touched a reviewed page: check it before accepting")
    lines += [
        f"contradictions: {sum(o.contradictions for o in outs)}",
        f"open questions: {sum(o.open_questions for o in outs)}",
        f"unverified blocks: {report.unverified}",
        f"low confidence: {', '.join(sorted(report.low_confidence)) or 'none'}",
    ]
    if report.redactions:
        counts = ", ".join(f"{k} {v}" for k, v in sorted(report.redactions.items()))
        lines.append(f"redactions: {counts}")
    reverted = sorted({p for o in outs for p in o.reverted})
    if reverted:
        lines.append(f"reverted: {', '.join(reverted)}")
    n = len(report.lint_errors)
    lines.append(f"lint: {n} error{'' if n == 1 else 's'}")
    lines += [
        f"  {f.code} {f.path}: {f.message}" if f.path else f"  {f.code}: {f.message}"
        for f in report.lint_errors
    ]
    if report.stopped:
        lines.append(
            f"stopped early: {report.stopped}; the remaining sources stay pending"
        )
    lines.append(f"cost ${report.cost_usd:.2f}")
    # titles, reasons and findings carry model text: never a terminal escape
    return "\n".join(_plain(line) for line in lines)


def _show_diff(root: Path, base: str, branch: str) -> None:
    # the pager is inherited: no capture
    subprocess.run(
        ["git", "diff", f"{base}...{branch}", "--", "."], cwd=root, check=False
    )


def _prompt_decide(root: Path, base: str, branch: str) -> Callable[[str], Decision]:
    def decide(summary: str) -> Decision:
        while True:
            answer = typer.prompt("accept, reject or diff? [a/r/d]").strip().lower()
            if answer in ("a", "accept"):
                return "accept"
            if answer in ("r", "reject"):
                return "reject"
            if answer in ("d", "diff"):
                _show_diff(root, base, branch)

    return decide


def _accept(root: Path, base: str, branch: str, undo: _Undo) -> None:
    """Merge the branch; if that is cut short, say how to finish it by hand."""
    try:
        gitops.switch(root, base)
        gitops.ff_merge(root, branch)
        gitops.delete_branch(root, branch)
    except BaseException:
        typer.echo(
            f"error: the accept stopped before it finished. To finish it, run in "
            f"{root}:\n"
            f"  git switch {base} && git merge --ff-only {branch} && "
            f"git branch -D {branch}\n"
            f"then delete {undo.hold} (copies held only for a reject)",
            err=True,
        )
        raise
    undo.discard()


def _rollback(undo: _Undo, base: str, branch: str | None) -> None:
    """Reject; if that is cut short, say how to finish it by hand."""
    try:
        undo.rollback(base, branch)
    except BaseException:
        drop = f" && git branch -D {branch}" if branch else ""
        typer.echo(
            f"error: the rollback stopped before it finished. To finish it, run in "
            f"{undo.root}:\n"
            f"  git switch {base}{drop}\n"
            f"then move each file listed in {undo.index} from that folder back "
            "to its dest (a null copy: delete the dest)",
            err=True,
        )
        raise


# --- the run ---


def run_ingest(
    ref: WikiRef,
    *,
    paths: list[Path] | None = None,
    note: str | None = None,
    trust: str | None = None,
    yes: bool = False,
    interactive: bool,
    today: date | None = None,
    decide: Callable[[str], Decision] | None = None,
    confirm: Callable[[str], bool] | None = None,
) -> IngestReport:
    """Ingest every pending source of the wiki on a branch, then gate it.

    Everything that can refuse the run does so before any paid call. An
    interactive accept of a run whose lint failed asks ``confirm`` once more
    (default: no).
    """
    root = Path(ref.path)
    cfg: WikiConfig = load_config(root)
    settings = load_llm_settings(root)
    claude_binary()
    if not (interactive or yes):
        raise WikiError("the gate needs a terminal; pass --yes")
    base = _preflight_git(root)
    check_paths(paths or [])  # a typo stops the run before any intake step
    today = today or date.today()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = _fresh(root / ".wiki" / "runs" / stamp)
    undo = _Undo(root, _fresh(root / TMP_DIR / f"ingest-{run_dir.name}"), run_dir)
    report = IngestReport(decision="noop", base=base)
    try:
        # inbox/ and raw/ only, by preflight; gitignored ones too (R23)
        for rel in gitops.changed_paths(root, ignored=True):
            if rel.startswith(_DIRT_OK):
                undo.keep(rel)
        items = _work_list(root, undo, paths, note, trust, today)
        items = _within_source_cap(root, cfg, items, undo)
        if not items:
            undo.discard()
            typer.echo("nothing to do")
            return report
        report.branch = _branch_name(root, today, items[0].rel)
        gitops.switch(root, report.branch, create=True)
        ctx = RunContext(
            root=root,
            cfg=cfg,
            settings=settings,
            manifest=Manifest.load(root),
            today=today,
            run_dir=run_dir,
            n=0,
        )
        _process(ctx, items, report, undo)
        _finalize(ctx, base, report)
        report.summary = summarize(report)
        typer.echo(report.summary)
        report.decision = _gate(root, base, report.branch, report, yes, decide, confirm)
    except BaseException:  # Ctrl-C or a crash: the wiki goes back as it was
        _rollback(undo, base, report.branch)
        raise
    if report.decision == "reject":
        _rollback(undo, base, report.branch)
    else:
        _accept(root, base, report.branch, undo)
    _announce(report, base, yes)
    return report


def _gate(
    root: Path,
    base: str,
    branch: str,
    report: IngestReport,
    yes: bool,
    decide: Callable[[str], Decision] | None,
    confirm: Callable[[str], bool] | None,
) -> Decision:
    """Accept or reject the finished branch: --yes takes only a clean run."""
    if yes:
        return "reject" if report.lint_errors else "accept"
    gate = decide or _prompt_decide(root, base, branch)
    # anything but an explicit accept rejects
    accepted = gate(report.summary) == "accept"
    if accepted and report.lint_errors:
        n = len(report.lint_errors)
        ask = confirm or (lambda q: typer.confirm(q, default=False))
        accepted = ask(f"lint has {n} error{'' if n == 1 else 's'}; accept anyway?")
    return "accept" if accepted else "reject"


def _announce(report: IngestReport, base: str, yes: bool) -> None:
    """Print the gate's outcome; a --yes reject exits 1."""
    if report.decision == "accept":
        typer.echo(f"accepted: {report.branch} merged into {base}")
    elif yes:
        report.exit_code = 1
        typer.echo(
            f"rejected: lint found {len(report.lint_errors)} errors and --yes "
            "accepts only a clean run; the inbox files are back"
        )
    else:
        typer.echo(f"rejected: {report.branch} deleted; the inbox files are back")
