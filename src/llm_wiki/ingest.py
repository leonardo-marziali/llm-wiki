"""The source stage of ``wiki ingest``: a prep commit, then for each part a plan,
a confined edit session, the post-check and one repair, then one commit.

A failing source rolls back to its prep commit and the run goes on; a usage
limit, an auth failure or a missing CLI rolls back this source and stops the run.
"""

import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from llm_wiki import claude
from llm_wiki.config import LlmSettings, WikiConfig
from llm_wiki.errors import STOP, WikiError
from llm_wiki.gitops import (
    changed_paths,
    exists_at,
    restore,
    show_text,
    stage_commit,
    unstage,
)
from llm_wiki.init import REVIEW_DAYS
from llm_wiki.intake import Prepared, source_slug, src_id_for
from llm_wiki.lint import Finding, cited_paths, lint
from llm_wiki.manifest import MANIFEST, Manifest, SourceEntry
from llm_wiki.pages import ID_RE, SOURCELESS_TYPES, SYSTEM_FILES, read_page
from llm_wiki.plan import plan_source, redact_plan, screen_plan, system_prompt
from llm_wiki.schemas import Claim, PagePlan, Plan
from llm_wiki.screen import Hit, quarantine

OPEN_QUESTIONS = "wiki/open-questions.md"
SHORT_REVIEW_DAYS = 60
SHORT_REVIEW_SECTIONS = {"conventions", "operations"}
_TOP_LEVEL = {"overview", "glossary"}
_FINALIZE_CODES = {"not-in-index"}  # finalize rebuilds the indexes after all sources
DELETE_CLAIM = "source deleted: mark its claims unverified per AGENTS.md"
VERIFY = (
    "update its sha256 for this source to the new value and refresh "
    "last_verified/review_after; any claim citing this source that the new "
    "version no longer supports goes inside `> [!unverified]` with its footnote "
    "removed"
)

OutcomeStatus = Literal["ingested", "quarantined", "failed", "refused", "deleted"]


@dataclass
class RunContext:
    root: Path
    cfg: WikiConfig
    settings: LlmSettings
    manifest: Manifest
    today: date
    run_dir: Path
    n: int  # 1-based source number, for run-dir file names


@dataclass
class SourceOutcome:
    rel: str
    title: str
    status: OutcomeStatus
    pages_created: list[str] = field(default_factory=list)
    pages_updated: list[str] = field(default_factory=list)
    reviewed_touched: list[str] = field(default_factory=list)
    reverted: list[str] = field(default_factory=list)
    contradictions: int = 0
    open_questions: int = 0
    reason: str = ""
    cost_usd: float = 0.0
    redactions: dict[str, int] = field(default_factory=dict)  # in the plans


class _Failed(Exception):
    """The source fails; its message is the outcome's reason."""


class _Injected(Exception):
    def __init__(self, hits: list[Hit]):
        super().__init__("plan-text injection")
        self.hits = hits


def page_rel(id_: str) -> str:
    """``concepts.branching`` -> ``wiki/concepts/branching.md``."""
    if id_ in _TOP_LEVEL:
        return f"wiki/{id_}.md"
    section, slug = id_.split(".", 1)
    return f"wiki/{section}/{slug}.md"


def review_after(id_: str, today: date) -> date:
    """+90 days, or +60 for conventions and operations pages."""
    short = id_.split(".", 1)[0] in SHORT_REVIEW_SECTIONS and "." in id_
    return today + timedelta(days=SHORT_REVIEW_DAYS if short else REVIEW_DAYS)


def is_page(rel: str) -> bool:
    parts = rel.split("/")
    return (
        parts[0] == "wiki"
        and rel.endswith(".md")
        and parts[-1] not in SYSTEM_FILES
        and "_archive" not in parts[1:-1]
    )


def _content(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _under(rel: str, folder: str | None) -> bool:
    return folder is not None and (rel == folder or rel.startswith(folder + "/"))


def _part_info(prepared: Prepared, part: str) -> tuple[str, str]:
    """(the prompt's part line, the run-file suffix)."""
    if len(prepared.parts) <= 1:
        return "the whole source", ""
    n = prepared.parts.index(part) + 1
    return f"part {n} of {len(prepared.parts)} (cite it as `#p{n}`)", f"-p{n}"


def _reads(prepared: Prepared, part: str) -> list[str]:
    """The converted files the edit session reads for ``part``."""
    conv = prepared.conversion
    assert conv is not None
    extra = conv.outputs[1 + conv.parts :] if conv.parts > 1 else conv.outputs[1:]
    # never a raw/ original, not even an image: only the plan's screened claims
    # may reach the pages
    return [part, *extra]


def build_edit_prompt(
    ctx: RunContext,
    prepared: Prepared,
    plan: Plan,
    part: str,
    findings: list[Finding] | None = None,
    verify: list[str] | None = None,
) -> str:
    """The edit session's stdin prompt: everything that varies per source and run.

    ``verify`` holds the pages a changed source backs that the plan leaves out:
    each gets the new sha and fresh dates, and loses what the new version no
    longer supports (R21).
    """
    root = Path(ctx.root)
    today = ctx.today.isoformat()
    label, _ = _part_info(prepared, part)
    src_id = src_id_for(ctx.cfg, ctx.manifest, prepared.rel)
    src_action = "update" if (root / page_rel(src_id)).is_file() else "create"
    reads = "\n".join(f"  - `{r}`" for r in _reads(prepared, part))
    allowed = [
        f"- `{page_rel(p.id)}`: {p.action} `{p.id}`; last_verified: {today}, "
        f"review_after: {review_after(p.id, ctx.today).isoformat()}"
        for p in plan.pages
    ]
    allowed += [
        f"- `{page_rel(id_)}`: verify `{id_}` against the new version: {VERIFY}; "
        f"last_verified: {today}, "
        f"review_after: {review_after(id_, ctx.today).isoformat()}"
        for id_ in verify or []
    ]
    allowed += [
        (
            f"- `{page_rel(src_id)}`: {src_action} the source page `{src_id}` "
            "(type source-summary, status as-is, title and summary from the plan's "
            f"source_title and source_summary); last_verified: {today}, "
            f"review_after: {review_after(src_id, ctx.today).isoformat()}"
        ),
        f"- `{OPEN_QUESTIONS}`: append entries only",
    ]
    blocks = [
        f"Today is {today}.",
        "\n".join(
            [
                "## Source",
                "",
                (
                    f"- raw path: {prepared.rel} "
                    f"(cite it as `[^n]: {prepared.rel} — <locator>`)"
                ),
                f"- sha256: {prepared.sha} (copy it exactly into `sources`)",
                f"- part: {label}",
                f"- read:\n{reads}",
            ]
        ),
        "## Allowed files\n\nChange only these files:\n\n" + "\n".join(allowed),
        "## Plan\n\n```json\n" + plan.model_dump_json(indent=2) + "\n```",
    ]
    return _with_repair(blocks, findings)


def _with_repair(blocks: list[str], findings: list[Finding] | None) -> str:
    if findings:
        blocks.append(
            "## Lint errors to fix\n\n"
            "This is a repair of the edit just made. Fix every error below in the "
            "allowed files and change nothing else:\n\n"
            + "\n".join(f"- `{f.path}` {f.code}: {f.message}" for f in findings)
        )
    return "\n\n".join(blocks) + "\n"


def _valid_id(id_: str) -> bool:
    return bool(re.fullmatch(ID_RE, id_)) and (id_ in _TOP_LEVEL or id_.count(".") == 1)


def deleted_plan(ctx: RunContext, rel: str) -> Plan | None:
    """The plan for deleted source ``rel``, built without a plan call: each page
    it backs that still cites it, as an update. None when no such page is left.

    A page that loses its last source is planned ``deprecated`` (AGENTS.md
    "Updates", rule 5); so is the source page. A malformed id in the manifest or
    a page whose frontmatter cannot be read fails the source (``_Failed``).
    """
    root = Path(ctx.root)
    src_id = src_id_for(ctx.cfg, ctx.manifest, rel)
    pages: list[PagePlan] = []
    for id_ in ctx.manifest.pages_for(rel):
        if not _valid_id(id_):
            raise _Failed(f"the manifest lists a malformed page id {id_!r} for {rel}")
        path = root / page_rel(id_)
        if not path.is_file():  # a missing page needs no edit
            continue
        page = read_page(root, path)
        fm = page.fm
        if fm is None:
            raise _Failed(
                f"{page_rel(id_)}: cannot read its frontmatter ({page.error}); "
                "fix the page, then run ingest again"
            )
        if id_ != src_id and rel not in cited_paths(page):
            continue  # it never cited the source: the edit may not touch it
        others = [s for s in fm.sources if s.path != rel]
        orphaned = (
            len(others) < len(fm.sources)
            and not others
            and fm.type not in SOURCELESS_TYPES
        )
        pages.append(
            PagePlan(
                id=id_,
                action="update",
                type=fm.type,
                status="deprecated" if orphaned or id_ == src_id else fm.status,
                adr_status=fm.adr_status,
                title=fm.title,
                summary=fm.summary,
                claims=[
                    Claim(text=DELETE_CLAIM, source=rel, locator="", confidence="low")
                ],
                reason=f"its source {rel} was deleted",
            )
        )
    if not pages:
        return None
    return Plan(
        source_title=rel,
        source_summary=f"{rel} was deleted from raw/.",
        pages=pages,
        contradictions=[],
        open_questions=[f"{rel} was deleted; are its facts still valid?"],
    )


def build_delete_prompt(
    ctx: RunContext,
    rel: str,
    plan: Plan,
    findings: list[Finding] | None = None,
) -> str:
    """The edit session's stdin prompt for a deleted source: no source to read."""
    root = Path(ctx.root)
    today = ctx.today.isoformat()
    src_id = src_id_for(ctx.cfg, ctx.manifest, rel)

    def dates(id_: str) -> str:
        after = review_after(id_, ctx.today).isoformat()
        return f"last_verified: {today}, review_after: {after}"

    allowed = [
        f"- `{page_rel(p.id)}`: update `{p.id}`"
        + (" (the source page)" if p.id == src_id else "")
        + f"; status: {p.status}; {dates(p.id)}"
        for p in plan.pages
    ]
    if src_id not in {p.id for p in plan.pages} and (root / page_rel(src_id)).is_file():
        allowed.append(
            f"- `{page_rel(src_id)}`: update `{src_id}` (the source page); "
            f"status: deprecated; {dates(src_id)}"
        )
    allowed.append(f"- `{OPEN_QUESTIONS}`: append entries only")
    blocks = [
        f"Today is {today}.",
        "\n".join(
            [
                "## Deleted source",
                "",
                f"- raw path: {rel} (deleted from raw/: there is nothing to read)",
                (
                    '- follow AGENTS.md "Updates, changed and deleted sources", '
                    "rules 4 and 5, on every allowed page: keep each claim that "
                    "cites this path inside `> [!unverified]` without footnotes, "
                    "remove the path from `sources` and its footnote markers and "
                    "definitions, lower `confidence`, and set the status given below"
                ),
            ]
        ),
        "## Allowed files\n\nChange only these files:\n\n" + "\n".join(allowed),
        "## Plan\n\n```json\n" + plan.model_dump_json(indent=2) + "\n```",
    ]
    return _with_repair(blocks, findings)


class _Source:
    """One ready source between its prep commit and its outcome."""

    def __init__(
        self,
        ctx: RunContext,
        prepared: Prepared,
        out: SourceOutcome,
        prep: str | None,
    ):
        self.ctx, self.prepared, self.out = ctx, prepared, out
        # the prep commit's sha: the only commit ever amended (None: no prep
        # commit, as for a deleted source)
        self.prep = prep
        self.root = Path(ctx.root)
        self.src_id = src_id_for(ctx.cfg, ctx.manifest, prepared.rel)
        self.src_existed = (self.root / page_rel(self.src_id)).is_file()
        self.allowed = {OPEN_QUESTIONS}
        if self.src_existed or prepared.kind != "deleted":  # never created then
            self.allowed.add(page_rel(self.src_id))
        self.oq_before: bytes | None = None  # open-questions.md before an edit
        self.planned: dict[str, str] = {}  # id -> its first planned action
        self.reviewed: set[str] = set()  # ids whose page was reviewed before
        self.verified: set[str] = set()  # unplanned pages a changed source backs
        run_dir = Path(ctx.run_dir).resolve()
        root = self.root.resolve()
        # the run's own files are never strays, gitignored or not
        self.run_rel = (
            run_dir.relative_to(root).as_posix()
            if run_dir.is_relative_to(root)
            else None
        )
        # what was already uncommitted (e.g. inbox files of later sources), with
        # its bytes so a stray edit to it can be undone; None: it was missing.
        # Gitignored files count too: a stray may hide there (m6)
        self.baseline = {
            p: _content(self.root / p)
            for p in changed_paths(self.root, ignored=True)
            if not _under(p, self.run_rel)
        }

    def run_part(self, part: str) -> None:
        ctx, prepared = self.ctx, self.prepared
        plan, errors, cost = plan_source(
            self.root,
            ctx.cfg,
            ctx.settings,
            prepared,
            part,
            ctx.today,
            ctx.run_dir,
            manifest=ctx.manifest,
            also_allowed=set(self.planned),  # pages earlier parts planned
        )
        self.out.cost_usd += cost
        if plan is None:
            raise _Failed("plan rejected: " + "; ".join(errors))
        plan, counts = redact_plan(plan)
        for kind, n in counts.items():
            self.out.redactions[kind] = self.out.redactions.get(kind, 0) + n
        trusted = "trusted" in ctx.manifest.entries[prepared.rel].flags
        hits = [] if trusted else screen_plan(plan)
        if hits:
            raise _Injected(hits)
        _, suffix = _part_info(prepared, part)
        verify = self._to_verify(plan)
        self.apply(
            plan,
            lambda found: build_edit_prompt(ctx, prepared, plan, part, found, verify),
            suffix,
            verify,
        )

    def _to_verify(self, plan: Plan) -> list[str]:
        """The pages a changed source backs and still cites that no plan of this
        source has covered yet (R21).
        """
        if self.prepared.kind != "changed":
            return []
        rel = self.prepared.rel
        skip = {self.src_id, *self.planned, *self.verified, *(p.id for p in plan.pages)}
        out = []
        for id_ in self.ctx.manifest.pages_for(rel):
            if id_ in skip or not _valid_id(id_):
                continue
            path = self.root / page_rel(id_)
            if path.is_file() and rel in cited_paths(read_page(self.root, path)):
                out.append(id_)
        return out

    def apply(
        self,
        plan: Plan,
        prompt: Callable[[list[Finding] | None], str],
        suffix: str,
        verify: list[str] | None = None,
    ) -> None:
        """The edit session, the post-check, then one repair of any lint errors."""
        self._record(plan, verify or [])
        self._edit(prompt(None), suffix, "edit")
        errors_found = self._check()
        if errors_found:
            self._edit(prompt(errors_found), suffix, "repair")
            errors_found = self._check()
        if errors_found:
            raise _Failed(
                "lint errors after the repair: "
                + "; ".join(f"{f.path} {f.code}: {f.message}" for f in errors_found)
            )

    def _record(self, plan: Plan, verify: list[str]) -> None:
        if not self.planned:
            self.out.title = plan.source_title
        self.out.contradictions += len(plan.contradictions)
        self.out.open_questions += len(plan.open_questions)
        for p in plan.pages:
            self.planned.setdefault(p.id, p.action)
            self.allowed.add(page_rel(p.id))
        self.verified.update(verify)
        self.allowed.update(page_rel(i) for i in verify)
        for id_ in [*(p.id for p in plan.pages), *verify, self.src_id]:
            path = self.root / page_rel(id_)
            if path.is_file():
                fm = read_page(self.root, path).fm
                if fm is not None and fm.review_status == "reviewed":
                    self.reviewed.add(id_)

    def _edit(self, prompt: str, suffix: str, kind: str) -> None:
        self.oq_before = _content(self.root / OPEN_QUESTIONS)
        call = claude.make_call(
            "edit",
            self.ctx.settings,
            prompt=prompt,
            system_prompt=system_prompt(self.root, "edit.md"),
            cwd=self.root,
        )
        result = claude.run(call)
        self.out.cost_usd += result.cost_usd
        run_dir = Path(self.ctx.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        name = f"{self.ctx.n}-{source_slug(self.prepared.rel)}{suffix}.{kind}.json"
        (run_dir / name).write_text(
            json.dumps(result.raw, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _changed(self) -> list[str]:
        """Paths changed since the prep commit, beyond what was uncommitted then."""
        now = {
            p
            for p in changed_paths(self.root, ignored=True)
            if p not in self.baseline and not _under(p, self.run_rel)
        }
        now |= {
            p
            for p, data in self.baseline.items()
            if _content(self.root / p) != data  # changed, deleted or recreated
        }
        return sorted(now)

    def _revert(self, paths: list[str]) -> None:
        """Committed paths go back to the prep commit, the others to their bytes."""
        restore(self.root, [p for p in paths if p not in self.baseline])
        for p in paths:
            if p not in self.baseline:
                continue
            path, data = self.root / p, self.baseline[p]
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

    def _check(self) -> list[Finding]:
        """Revert stray changes, then the errors on the changed and planned pages.

        A changed .gitignore goes back first, so it cannot hide a stray.
        """
        ignores = [p for p in self._changed() if Path(p).name == ".gitignore"]
        self._revert(ignores)
        changed = sorted({*self._changed(), *ignores})
        strays = [p for p in changed if p not in self.allowed]
        self._revert([p for p in strays if p not in ignores])
        self.out.reverted = sorted({*self.out.reverted, *strays})
        wanted = sorted(p for p in self.allowed if is_page(p))
        findings = [
            Finding("error", "missing-page", p, "planned page was not written")
            for p in wanted
            if not (self.root / p).is_file()
        ]
        findings += self._open_questions_check()
        pages = [p for p in changed if p in self.allowed and is_page(p)]
        if self.prepared.kind == "deleted":
            # an untouched planned page still cites the deleted file: lint it too
            pages = wanted
            findings += self._deleted_citations(wanted)
        else:  # an untouched verify page keeps the old sha: lint it too
            pages = sorted({*pages, *(page_rel(i) for i in self.verified)})
        pages = [p for p in pages if (self.root / p).is_file()]
        if pages:
            report = lint(
                self.root,
                self.ctx.cfg,
                only=[Path(p) for p in pages],
                today=self.ctx.today,
            )
            findings += [
                f
                for f in report.findings
                if f.severity == "error"
                and f.path in pages
                and f.code not in _FINALIZE_CODES
            ]
        return findings

    def _open_questions_check(self) -> list[Finding]:
        """open-questions.md may only grow: anything else is put back (R19)."""
        before = self.oq_before
        path = self.root / OPEN_QUESTIONS
        now = _content(path)
        if before is None or (now is not None and now.startswith(before)):
            return []
        path.write_bytes(before)
        msg = (
            "may only grow: append entries, never edit or remove existing ones; "
            "the earlier text was put back"
        )
        return [Finding("error", "open-questions-append-only", OPEN_QUESTIONS, msg)]

    def _deleted_citations(self, pages: list[str]) -> list[Finding]:
        """Planned pages whose ``sources`` or footnotes still cite the deleted file."""
        rel = self.prepared.rel
        msg = f"still cites the deleted {rel} in `sources` or a footnote"
        return [
            Finding("error", "cites-deleted-source", p, msg)
            for p in pages
            if (self.root / p).is_file()
            and rel in cited_paths(read_page(self.root, self.root / p))
        ]

    def undo(self) -> None:
        """Put the wiki back to the prep commit and the files uncommitted then."""
        self._revert(self._changed())

    def commit(self) -> None:
        ctx, rel = self.ctx, self.prepared.rel
        assert self.prepared.conversion is not None
        staged = [p for p in self._changed() if p in self.allowed]
        entry = ctx.manifest.entries[rel]
        touched = {*entry.pages_touched, *self.planned, self.src_id}
        ctx.manifest.record(
            entry.model_copy(
                update={
                    "status": "ingested",
                    "pages_touched": sorted(touched),
                    "last_ingested": ctx.today,
                    "converter": self.prepared.conversion.converter,
                }
            )
        )
        ctx.manifest.save(self.root)
        stage_commit(self.root, [*staged, MANIFEST], f"ingest: {self.out.title}")
        created = {i for i, a in self.planned.items() if a == "create"}
        updated = {i for i, a in self.planned.items() if a == "update"}
        updated |= {i for i in self.verified if page_rel(i) in staged}
        (updated if self.src_existed else created).add(self.src_id)
        self.out.pages_created = sorted(created)
        self.out.pages_updated = sorted(updated - created)
        self.out.reviewed_touched = sorted(
            i for i in self.reviewed if page_rel(i) in staged
        )
        self.out.status = "ingested"

    def commit_deleted(self) -> None:
        """One commit: the edited pages, the raw file's deletion, the manifest."""
        ctx, rel = self.ctx, self.prepared.rel
        staged = [p for p in self._changed() if p in self.allowed]
        ctx.manifest.mark_deleted(rel)
        ctx.manifest.save(self.root)
        stage_commit(self.root, [*staged, rel, MANIFEST], f"ingest(delete): {rel}")
        self.out.pages_updated = sorted(
            i for i in {*self.planned, self.src_id} if page_rel(i) in staged
        )
        self.out.reviewed_touched = sorted(
            i for i in self.reviewed if page_rel(i) in staged
        )
        self.out.status = "deleted"


def _set_status(ctx: RunContext, rel: str, **update) -> None:
    entry = ctx.manifest.entries[rel]
    ctx.manifest.record(entry.model_copy(update=update))
    ctx.manifest.save(Path(ctx.root))


def _entry_at(root: Path, rev: str, rel: str) -> SourceEntry | None:
    """``rel``'s manifest entry in commit ``rev``, when its raw file was there."""
    text = show_text(root, rev, MANIFEST)
    if text is None or not exists_at(root, rev, rel):
        return None
    try:
        data = json.loads(text)["sources"][rel]
        return SourceEntry.model_validate(data)
    except (ValueError, KeyError, TypeError):  # ValidationError is a ValueError
        return None


def _quarantine_plan(
    ctx: RunContext, prepared: Prepared, hits: list[Hit], prep: str
) -> str:
    """Move the raw file to quarantine and take it and its conversion out of the
    prep commit, so the injected text never reaches the wiki or its history.

    A changed source gets its last ingested version back, raw file and manifest
    entry, so the pages it backs keep valid citations.
    """
    root, rel = Path(ctx.root), prepared.rel
    assert prepared.conversion is not None
    reason = "plan-text injection: " + ", ".join(sorted({h.kind for h in hits}))
    old = _entry_at(root, f"{prep}^", rel) if prepared.kind == "changed" else None
    record: dict = {
        "source": rel,
        "sha256": prepared.sha,
        "kind": "injection",
        "reason": reason,
        "hits": [
            {"file": "plan", "kind": h.kind, "line": h.line, "excerpt": h.excerpt}
            for h in hits
        ],
    }
    if old is not None:
        record["restored_sha256"] = old.sha256
    quarantine(root, root / rel, record)
    outputs = prepared.conversion.outputs
    for out in outputs:
        (root / out).unlink(missing_ok=True)
    if old is not None:
        restore(root, [rel], rev=f"{prep}^")
        ctx.manifest.record(old)
        ctx.manifest.save(root)
        reason += "; kept the last ingested version"
    else:
        _set_status(ctx, rel, status="quarantined", converted=[])
    stage_commit(root, [rel, *outputs, MANIFEST], f"ingest(prep): {rel}", amend=prep)
    return reason


def _record_unready(ctx: RunContext, prepared: Prepared) -> str:
    """Settle the tree for a quarantined or refused source; returns the reason.

    A changed source gets its last ingested version back, raw file and manifest
    entry, so the pages it backs keep valid citations; a refused new version
    moves to ``prepared.origin`` (set by the caller). A new refused source is
    kept in raw/ flagged ``refused``: it is retried only once its file changes.
    """
    root, rel = Path(ctx.root), prepared.rel
    old = _entry_at(root, "HEAD", rel) if prepared.kind == "changed" else None
    if old is not None:
        moved = ""
        if prepared.status == "refused" and prepared.origin is not None:
            # the refused version is the user's only copy: never drop it
            dest = Path(prepared.origin)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(root / rel), dest)
            moved = f"; the new version is back at {dest.relative_to(root).as_posix()}"
        restore(root, [rel])
        ctx.manifest.record(old)
        if prepared.held is not None:  # the reason file records the restore
            reason_file = prepared.held.with_name(prepared.held.name + ".reason.json")
            record = json.loads(reason_file.read_text(encoding="utf-8"))
            record["restored_sha256"] = old.sha256
            reason_file.write_text(
                json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        ctx.manifest.save(root)
        return prepared.reason + "; kept the last ingested version" + moved
    if prepared.status == "refused":
        entry = ctx.manifest.entries[rel]
        flags = [*entry.flags, *(["refused"] if "refused" not in entry.flags else [])]
        _set_status(ctx, rel, flags=flags)
    return prepared.reason


def process_deleted(ctx: RunContext, rel: str) -> SourceOutcome:
    """Record a source removed from raw/, its deletion committed with the edit.

    No plan call: ``deleted_plan`` lists the pages the source backs, and an edit
    session marks their claims unverified (AGENTS.md "Updates", rules 4 and 5).
    A failed edit rolls back and leaves the deletion pending for the next run.
    """
    root = Path(ctx.root)
    out = SourceOutcome(rel=rel, title=rel, status="deleted")
    try:
        plan = deleted_plan(ctx, rel)
    except _Failed as exc:  # nothing changed yet: the deletion stays pending
        out.status, out.reason = "failed", str(exc)
        return out
    if plan is None:  # it backs no page: only the manifest and the log change
        try:
            ctx.manifest.mark_deleted(rel)
            ctx.manifest.save(root)
            stage_commit(root, [rel, MANIFEST], f"ingest(delete): {rel}")
        except WikiError as exc:  # e.g. a GitError: the deletion stays pending
            _drop_uncommitted(ctx, [rel, MANIFEST], [MANIFEST])
            out.status, out.reason = "failed", str(exc)
        return out
    prepared = Prepared(rel=rel, sha=ctx.manifest.entries[rel].sha256, kind="deleted")
    source = _Source(ctx, prepared, out, None)
    try:
        source.apply(plan, lambda found: build_delete_prompt(ctx, rel, plan, found), "")
        source.commit_deleted()
    except STOP as exc:
        exc.cost_usd += out.cost_usd
        source.undo()
        raise
    except (_Failed, WikiError) as exc:  # a ClaudeError, a GitError, …
        out.cost_usd += getattr(exc, "cost_usd", 0.0)
        source.undo()
        _drop_uncommitted(ctx, [rel, MANIFEST], [])
        out.status, out.reason = "failed", str(exc)
        out.pages_updated, out.reviewed_touched = [], []
    return out


def _drop_uncommitted(ctx: RunContext, staged: list[str], files: list[str]) -> None:
    """Unstage ``staged`` and put ``files`` back to HEAD after a failed commit;
    the in-memory manifest follows the file.
    """
    root = Path(ctx.root)
    unstage(root, staged)
    restore(root, files)
    ctx.manifest.entries = Manifest.load(root).entries


def process_source(ctx: RunContext, prepared: Prepared) -> SourceOutcome:
    """Ingest one prepared source on the checked-out branch.

    ``ctx.manifest`` already holds the entry that ``prepare_source`` recorded.
    """
    root = Path(ctx.root)
    rel = prepared.rel
    out = SourceOutcome(rel=rel, title=rel, status="failed")
    prep_message = f"ingest(prep): {rel}"
    ctx.manifest.save(root)
    outputs = prepared.conversion.outputs if prepared.conversion else []
    try:
        if prepared.status != "ready":  # screened out or refused: record it, no LLM
            out.status, out.reason = prepared.status, _record_unready(ctx, prepared)
            stage_commit(root, [rel, MANIFEST], prep_message, allow_empty=True)
            return out
        # always a commit of its own: a later amend can never rewrite another one
        prep = stage_commit(
            root, [rel, *outputs, MANIFEST], prep_message, allow_empty=True
        )
    except WikiError as exc:  # no prep commit: the source stays pending in raw/
        _drop_uncommitted(ctx, [rel, *outputs, MANIFEST], [*outputs, MANIFEST])
        out.status, out.reason = "failed", str(exc)
        return out
    source = _Source(ctx, prepared, out, prep)
    try:
        for part in prepared.parts:
            source.run_part(part)
        source.commit()
    except _Injected as exc:
        source.undo()
        out.status, out.reason = (
            "quarantined",
            _quarantine_plan(ctx, prepared, exc.hits, prep),
        )
        return out
    except STOP as exc:
        exc.cost_usd += out.cost_usd
        _fail(ctx, source, str(exc))
        raise
    except (_Failed, WikiError) as exc:  # a ClaudeError, a GitError, a refused image
        out.cost_usd += getattr(exc, "cost_usd", 0.0)
        _fail(ctx, source, str(exc))
    return out


def _fail(ctx: RunContext, source: _Source, reason: str) -> None:
    """Roll back to the prep commit and record the source as failed in it."""
    source.undo()
    root = Path(ctx.root)
    ctx.manifest.entries = Manifest.load(root).entries  # the prep commit's
    _set_status(ctx, source.prepared.rel, status="failed")
    assert source.prep is not None
    stage_commit(
        Path(ctx.root),
        [MANIFEST],
        f"ingest(prep): {source.prepared.rel}",
        amend=source.prep,
    )
    out = source.out
    out.status, out.reason = "failed", reason
    out.pages_created, out.pages_updated, out.reviewed_touched = [], [], []
