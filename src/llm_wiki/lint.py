"""Deterministic lint: the quality gate for the page contract and the caps."""

import re
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

from rapidfuzz import fuzz

from llm_wiki.caps import Caps
from llm_wiki.config import WikiConfig
from llm_wiki.errors import WikiError
from llm_wiki.index import GENERATED_MARK
from llm_wiki.manifest import Manifest
from llm_wiki.pages import (
    ID_RE,
    SOURCELESS_TYPES,
    SYSTEM_FILES,
    Page,
    id_for,
    iter_pages,
)
from llm_wiki.screen import find_injection, redact

Severity = Literal["error", "warn"]

LOG_RE = r"^## \[\d{4}-\d{2}-\d{2}\] [a-z-]+ \| .+$"
_LOG = re.compile(LOG_RE)
_ID = re.compile(ID_RE)
_FOOTNOTE = re.compile(r"^\[\^([^\]]+)\]:\s*(\S+)", re.MULTILINE)
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s++(.*)$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_FENCE = re.compile(r"^\s*(```|~~~)")
_BACKTICKS = re.compile(r"`+")
# page lint only: a page may legitimately say "update CLAUDE.md when ..."
_WARN_INJECTION = {"agent_config"}
_EXTERNAL = ("http:", "https:", "mailto:")
_ORPHAN_EXEMPT = {"overview", "glossary", "hub"}
_SKIP_SECTIONS = {"tl;dr", "related"}
_MIN_WORDS = 12
_TITLE_RATIO = 90
_NOT_UTF8 = "not valid UTF-8"
# the check a system file feeds; an unreadable file is reported under that code
_SYSTEM_CODE = {
    "log.md": "log-format",
    "index.md": "index-too-long",
    "_index.md": "not-in-index",
}


@dataclass
class Finding:
    severity: Severity
    code: str
    path: str  # wiki-root-relative (e.g. "wiki/x/y.md") or ""
    message: str


@dataclass
class LintReport:
    findings: list[Finding]

    @property
    def errors(self) -> int:
        return sum(f.severity == "error" for f in self.findings)

    @property
    def warnings(self) -> int:
        return sum(f.severity == "warn" for f in self.findings)


def _strip_code(body: str) -> list[str]:
    """Body lines with fenced code blocks blanked out (keeps line numbers)."""
    out: list[str] = []
    fence: str | None = None
    for line in body.split("\n"):
        m = _FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)
            out.append("")
        elif fence is not None:
            if line.strip().startswith(fence):
                fence = None
            out.append("")
        else:
            out.append(line)
    return out


def _drop_code_spans(line: str) -> str:
    """``line`` without inline code spans, in linear time.

    A span opens with a run of backticks and closes at the next run of the same
    length; an opening run without a closing one is kept as text.
    """
    runs = list(_BACKTICKS.finditer(line))
    by_len: dict[int, deque[int]] = defaultdict(deque)  # run length -> run indexes
    for i, run in enumerate(runs):
        by_len[len(run.group())].append(i)
    out: list[str] = []
    pos = 0
    i = 0
    while i < len(runs):
        later = by_len[len(runs[i].group())]
        while later and later[0] <= i:
            later.popleft()
        if not later:
            i += 1
            continue
        close = later.popleft()
        out.append(line[pos : runs[i].start()])
        pos = runs[close].end()
        i = close + 1
    out.append(line[pos:])
    return "".join(out)


def _md_links(text: str) -> list[str]:
    """Relative ``.md`` link targets with anchors stripped."""
    targets = []
    for line in _strip_code(text):
        for m in _LINK.finditer(_drop_code_spans(line)):
            t = m.group(1)
            if t.lower().startswith(_EXTERNAL):
                continue
            t = unquote(t.split("#", 1)[0])
            if t.endswith(".md"):
                targets.append(t)
    return targets


def _resolve(base_dir: Path, target: str) -> Path:
    return (base_dir / target).resolve()


def _in_raw(root: Path, target: str) -> Path | None:
    """Resolve a wiki-root-relative citation; None unless inside ``root/raw``."""
    if not target.startswith("raw/"):
        return None
    p = (root / target).resolve()
    return p if p.is_relative_to((root / "raw").resolve()) else None


def _read(path: Path) -> str | None:
    """File text, or None if it is not valid UTF-8 or unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _skip_level_after(heading: re.Match[str], skip_level: int) -> int:
    """The TL;DR/Related skip level once ``heading`` is reached (0: not skipping)."""
    level = len(heading.group(1))
    if heading.group(2).strip().strip("#").strip().lower() in _SKIP_SECTIONS:
        return level
    return 0 if level <= skip_level else skip_level


class _Blocks:
    """Groups body lines into paragraphs and bullets that need a citation."""

    def __init__(self) -> None:
        self.blocks: list[list[str]] = []
        self.cur: list[str] | None = None
        self.skip_level = 0  # inside a TL;DR/Related section of this heading level
        self.in_unverified = False

    def close(self) -> None:
        if self.cur:
            self.blocks.append(self.cur)
        self.cur = None

    def feed(self, line: str) -> None:
        stripped = line.strip()
        h = _HEADING.match(stripped)
        if h or not stripped:
            self.close()
            self.in_unverified = False
            if h:
                self.skip_level = _skip_level_after(h, self.skip_level)
            return
        if self.skip_level:
            return
        is_quote = stripped.startswith(">")
        if is_quote and stripped.lstrip("> ").lower().startswith("[!unverified]"):
            self.close()
            self.in_unverified = True
        elif not is_quote:
            self.in_unverified = False
        if self.in_unverified:
            return
        if stripped.startswith("|") or _FOOTNOTE.match(stripped):
            self.close()
        elif _BULLET.match(line) or self.cur is None:
            self.close()
            self.cur = [stripped]
        else:
            self.cur.append(stripped)


def _uncited(body: str) -> list[str]:
    """Paragraphs and bullets of >= 12 words without a footnote reference."""
    blocks = _Blocks()
    for line in _strip_code(body):
        blocks.feed(line)
    blocks.close()
    texts = (" ".join(b) for b in blocks.blocks)
    return [
        text
        for text in texts
        if not (text.startswith("**TL;DR**") or "[^" in text)
        and len(_BULLET.sub("", text).split()) >= _MIN_WORDS
    ]


def _section_of(rel: str, kinds: dict[str, str]) -> str | None:
    if "/" not in rel:
        return None
    section = rel.split("/", 1)[0]
    return section if section in kinds else None


def _index_targets(root: Path, section: str) -> set[str]:
    path = root / "wiki" / section / "_index.md"
    if not path.is_file():
        return set()
    text = _read(path)
    if text is None or GENERATED_MARK not in text:
        return set()
    block = text.split(GENERATED_MARK, 1)[1]
    return {m.group(1) for m in _LINK.finditer(block)}


def routing_error(
    kind: str, type_: str, status: str, adr_status: str | None
) -> str | None:
    """Why a page with this type/status does not belong in a ``kind`` section."""
    if kind == "to-be":
        # history keeps its place in the section it was planned in: a
        # superseded stub, or a deprecated page that lost its sources (R18)
        if status not in ("to-be", "superseded", "deprecated"):
            return f"status '{status}' in a to-be section"
    elif kind == "decisions":
        if type_ != "decision":
            return f"type '{type_}' in a decisions section (must be 'decision')"
        if adr_status is None:
            return "decision without adr_status"
        if status == "to-be" and adr_status != "proposed":
            return f"to-be decision with adr_status '{adr_status}' (must be 'proposed')"
    elif status == "to-be":
        return f"status 'to-be' in a {kind} section"
    return None


def _routing(kind: str, page: Page) -> str | None:
    fm = page.fm
    assert fm is not None
    return routing_error(kind, fm.type, fm.status, fm.adr_status)


def _cap(
    out: list[Finding], code: str, path: str, what: str, n: int, max_: int, warn: int
) -> None:
    if n > max_:
        out.append(
            Finding("error", code, path, f"{n} {what} exceeds the cap of {max_}")
        )
    elif n >= warn:
        out.append(
            Finding(
                "warn",
                code + "-warn",
                path,
                f"{n} {what} reaches the warning level of {warn}",
            )
        )


def cited_paths(page: Page) -> set[str]:
    """The raw paths ``page`` cites: its ``sources`` and its footnote targets."""
    paths = {src.path for src in page.fm.sources} if page.fm is not None else set()
    body = "\n".join(_strip_code(page.body))
    return paths | {m.group(2) for m in _FOOTNOTE.finditer(body)}


def _wiki_rel(rel: str) -> str:
    """Wiki-root-relative path of a page given relative to ``wiki/``."""
    return "wiki/" + rel


def _all_sources_trusted(page: Page, trusted: set[str]) -> bool:
    return bool(
        page.fm and page.fm.sources and all(s.path in trusted for s in page.fm.sources)
    )


def _body_issues(page: Page, path: str, caps: Caps, trusted: set[str]) -> list[Finding]:
    """Checks that need only the page text, so they run even on a broken page.

    A page whose sources are all trusted gets injection findings as warnings.
    """
    out: list[Finding] = [
        Finding("error", "broken-link", path, f"link target '{target}' does not exist")
        for target in _md_links(page.body)
        if not _resolve(page.path.parent, target).is_file()
    ]
    if page.line_count > caps.max_page_lines:
        out.append(
            Finding(
                "error",
                "page-too-long",
                path,
                f"{page.line_count} lines (max {caps.max_page_lines})",
            )
        )
    demote = _all_sources_trusted(page, trusted)
    out += _screen_issues(page.body, path, demote)
    if page.fm is not None:
        fm = page.fm
        texts = [fm.title, fm.summary, *fm.tags, *(s.locator for s in fm.sources)]
        out += _screen_issues("\n".join(texts), path, demote, "frontmatter: ")
    return out


def _screen_issues(
    text: str, path: str, demote: bool, where: str = ""
) -> list[Finding]:
    """Secrets and injection in ``text``; ``where`` prefixes the messages."""
    out: list[Finding] = []
    _, counts = redact(text)
    if sum(counts.values()):
        out.append(
            Finding(
                "error",
                "secret-in-page",
                path,
                f"{where}possible secret: " + ", ".join(sorted(counts)),
            )
        )
    hits = find_injection(text)
    groups: tuple[tuple[Severity, list], ...] = (
        (
            "warn" if demote else "error",
            [h for h in hits if h.kind not in _WARN_INJECTION],
        ),
        ("warn", [h for h in hits if h.kind in _WARN_INJECTION]),
    )
    out += [
        Finding(
            sev,
            "injection-in-page",
            path,
            where + "; ".join(f"line {h.line} {h.kind}: {h.excerpt}" for h in group),
        )
        for sev, group in groups
        if group
    ]
    return out


def _placement_issues(page: Page, path: str, ctx: dict) -> list[Finding]:
    """Id, section index and routing, inbound links, TL;DR and sources."""
    fm = page.fm
    assert fm is not None
    out: list[Finding] = []
    want = id_for(page.rel)
    if fm.id != want or not _ID.match(fm.id):
        out.append(
            Finding("error", "id-mismatch", path, f"id '{fm.id}' should be '{want}'")
        )
    section = _section_of(page.rel, ctx["kinds"])
    if section is not None:
        if page.rel.removeprefix(section + "/") not in ctx["index_targets"][section]:
            out.append(
                Finding(
                    "error", "not-in-index", path, f"missing from {section}/_index.md"
                )
            )
        reason = _routing(ctx["kinds"][section], page)
        if reason:
            out.append(Finding("error", "section-routing", path, reason))
    if fm.type not in _ORPHAN_EXEMPT and page.path.resolve() not in ctx["inbound"]:
        out.append(
            Finding("warn", "orphan", path, "no inbound link from any other page")
        )
    if not any(line.startswith("**TL;DR**") for line in page.body.split("\n")):
        out.append(
            Finding("error", "missing-tldr", path, "no line starting with **TL;DR**")
        )
    # a deprecated page may outlive its deleted sources
    sourceless_ok = fm.type in SOURCELESS_TYPES or fm.status == "deprecated"
    if not fm.sources and not sourceless_ok:
        out.append(
            Finding("error", "missing-sources", path, f"type '{fm.type}' needs sources")
        )
    return out


def _footnote_issues(root: Path, page: Page, path: str) -> list[Finding]:
    out: list[Finding] = []
    for m in _FOOTNOTE.finditer("\n".join(_strip_code(page.body))):
        label, target = m.group(1), m.group(2)
        p = _in_raw(root, target)
        if p is None:
            msg = f"footnote [^{label}] cites '{target}' outside raw/"
            out.append(Finding("error", "citation-outside-raw", path, msg))
        elif not p.is_file():
            msg = f"footnote [^{label}] cites missing '{target}'"
            out.append(Finding("error", "citation-missing", path, msg))
    return out


def _source_issues(
    root: Path, page: Page, path: str, manifest: Manifest
) -> list[Finding]:
    assert page.fm is not None
    out: list[Finding] = []
    for src in page.fm.sources:
        p = _in_raw(root, src.path)
        if p is None:
            msg = f"source '{src.path}' resolves outside raw/"
            out.append(Finding("error", "citation-outside-raw", path, msg))
        elif not p.is_file():
            msg = f"source '{src.path}' does not exist"
            out.append(Finding("error", "citation-missing", path, msg))
        entry = manifest.entries.get(src.path)
        if entry is None or entry.status == "deleted":
            msg = f"source '{src.path}' is not in the manifest"
            out.append(Finding("error", "sha-mismatch", path, msg))
        elif entry.sha256 != src.sha256:
            msg = f"source '{src.path}' sha256 differs from the manifest"
            out.append(Finding("error", "sha-mismatch", path, msg))
    return out


def _freshness_issues(page: Page, path: str, today: date) -> list[Finding]:
    """Uncited paragraphs, superseded pages without a successor, stale pages."""
    fm = page.fm
    assert fm is not None
    out: list[Finding] = [
        Finding("warn", "uncited-paragraph", path, f"no citation: {text[:60]}")
        for text in _uncited(page.body)
    ]
    if fm.status == "superseded" and not fm.superseded_by:
        msg = "superseded page without superseded_by"
        out.append(Finding("error", "superseded-no-successor", path, msg))
    if fm.review_after < today:
        msg = f"review_after {fm.review_after.isoformat()} has passed"
        out.append(Finding("warn", "stale", path, msg))
    return out


def _page_checks(root: Path, page: Page, ctx: dict) -> list[Finding]:
    path = _wiki_rel(page.rel)
    out = _body_issues(page, path, ctx["caps"], ctx["trusted"])
    if page.error is not None:
        return [*out, Finding("error", "frontmatter-invalid", path, page.error)]
    return [
        *out,
        *_placement_issues(page, path, ctx),
        *_footnote_issues(root, page, path),
        *_source_issues(root, page, path, ctx["manifest"]),
        *_freshness_issues(page, path, ctx["today"]),
    ]


def _system_files(wiki: Path) -> list[Path]:
    return sorted(
        p
        for p in wiki.rglob("*.md")
        if p.is_file()
        and p.name in SYSTEM_FILES
        and "_archive" not in p.relative_to(wiki).parts[:-1]
    )


def _inbound(pages: list[Page]) -> set[Path]:
    """Targets of links between pages; system files (indexes, log) do not count."""
    inbound: set[Path] = set()
    for p in pages:
        here = p.path.resolve()
        inbound |= {_resolve(p.path.parent, t) for t in _md_links(p.body)} - {here}
    return inbound


def _select(root: Path, pages: list[Page], only: list[Path] | None) -> list[Page]:
    """The pages named by ``only`` (in order, deduplicated), or all pages."""
    if only is None:
        return pages
    by_path = {p.path.resolve(): p for p in pages}
    selected: list[Page] = []
    for o in only:
        page = by_path.get((root / o).resolve())
        if page is None:
            raise WikiError(f"not a wiki page: {o}")
        if page not in selected:
            selected.append(page)
    return selected


def _duplicate_ids(valid: list[Page]) -> list[Finding]:
    out: list[Finding] = []
    first_id: dict[str, str] = {}
    for p in valid:
        assert p.fm is not None
        if p.fm.id in first_id:
            msg = f"id '{p.fm.id}' also used by wiki/{first_id[p.fm.id]}"
            out.append(Finding("error", "duplicate-id", _wiki_rel(p.rel), msg))
        else:
            first_id[p.fm.id] = p.rel
    return out


def _duplicate_titles(valid: list[Page]) -> list[Finding]:
    out: list[Finding] = []
    titles = [(p, p.fm.title) for p in valid if p.fm is not None]
    for i, (a, a_title) in enumerate(titles):
        for b, b_title in titles[i + 1 :]:
            ratio = fuzz.token_sort_ratio(a_title.casefold(), b_title.casefold())
            if ratio >= _TITLE_RATIO:
                msg = f"title '{b_title}' is close to '{a_title}' (wiki/{a.rel})"
                out.append(Finding("warn", "duplicate-title", _wiki_rel(b.rel), msg))
    return out


def _system_file_checks(
    root: Path, wiki: Path
) -> tuple[list[Finding], dict[Path, str | None]]:
    """Broken links in system files, plus each system file's text (None: unreadable)."""
    out: list[Finding] = []
    system_text: dict[Path, str | None] = {}
    for f in _system_files(wiki):
        rel = f.relative_to(root).as_posix()
        text = system_text[f] = _read(f)
        if text is None:
            code = _SYSTEM_CODE.get(f.name, "broken-link")
            out.append(Finding("error", code, rel, _NOT_UTF8))
            continue
        if f.name == "open-questions.md":  # entries come from source text
            out += _screen_issues(text, rel, demote=False)
        out += [
            Finding("error", "broken-link", rel, f"link target '{t}' does not exist")
            for t in _md_links(text)
            if not _resolve(f.parent, t).is_file()
        ]
    return out, system_text


def _cap_checks(
    caps: Caps, manifest: Manifest, valid: list[Page], kinds: dict[str, str]
) -> list[Finding]:
    out: list[Finding] = []
    live = manifest.live()
    _cap(out, "cap-sources", "", "sources", live, caps.max_sources, caps.warn_sources)
    _cap(out, "cap-pages", "", "pages", len(valid), caps.max_pages, caps.warn_pages)
    per_section: dict[str, int] = defaultdict(int)
    for p in valid:
        section = _section_of(p.rel, kinds)
        if section is not None:
            per_section[section] += 1
    for s in kinds:
        _cap(
            out,
            "cap-section",
            f"wiki/{s}",
            f"pages in section '{s}'",
            per_section[s],
            caps.max_section_pages,
            caps.warn_section_pages,
        )
    return out


def _index_length(index_text: str | None, caps: Caps) -> list[Finding]:
    if index_text is None:
        return []
    n = len(index_text.splitlines())
    if n <= caps.max_index_lines:
        return []
    msg = f"{n} lines (max {caps.max_index_lines})"
    return [Finding("error", "index-too-long", "wiki/index.md", msg)]


def _log_format(log_text: str | None) -> list[Finding]:
    if log_text is None:
        return []
    return [
        Finding(
            "error",
            "log-format",
            "wiki/log.md",
            f"line {n}: expected '## [YYYY-MM-DD] op | text'",
        )
        for n, line in enumerate(log_text.splitlines(), 1)
        if line.startswith("## ") and not _LOG.match(line)
    ]


def lint(
    root: Path,
    cfg: WikiConfig,
    only: list[Path] | None = None,
    today: date | None = None,
    trusted: set[str] | None = None,
) -> LintReport:
    """Lint the wiki at ``root``.

    ``only`` limits per-page checks to those pages (absolute or relative to
    ``root``); wiki-wide checks always run. A path that is not a page raises
    WikiError. ``trusted`` holds the raw paths whose injection findings are only
    warnings; None reads them from the manifest's ``trusted`` flag.
    """
    root = Path(root).resolve()
    wiki = root / "wiki"
    caps = cfg.effective_caps()
    manifest = Manifest.load(root)
    pages = list(iter_pages(root))
    kinds = {s.name: s.kind for s in cfg.sections}
    if trusted is None:
        trusted = {p for p, e in manifest.entries.items() if "trusted" in e.flags}
    ctx = {
        "caps": caps,
        "trusted": trusted,
        "kinds": kinds,
        "manifest": manifest,
        "inbound": _inbound(pages),
        "today": today or date.today(),
        "index_targets": {s: _index_targets(root, s) for s in kinds},
    }
    findings: list[Finding] = []
    for p in _select(root, pages, only):
        findings += _page_checks(root, p, ctx)

    # wiki-wide checks
    valid = [p for p in pages if p.fm is not None]
    system_findings, system_text = _system_file_checks(root, wiki)
    findings += [
        *_duplicate_ids(valid),
        *_duplicate_titles(valid),
        *system_findings,
        *_cap_checks(caps, manifest, valid, kinds),
        *_index_length(system_text.get(wiki / "index.md"), caps),
        *_log_format(system_text.get(wiki / "log.md")),
    ]
    findings.sort(key=lambda f: (f.path, f.code, f.message))
    return LintReport(findings)


def write_report(root: Path, report: LintReport, today: date) -> Path:
    """Write ``.wiki/reports/lint-YYYY-MM-DD.md`` grouped by severity, then code."""
    path = Path(root) / ".wiki" / "reports" / f"lint-{today.isoformat()}.md"
    lines = [
        f"# Lint report {today.isoformat()}",
        "",
        f"{report.errors} errors, {report.warnings} warnings",
    ]
    for sev in ("error", "warn"):
        group = [f for f in report.findings if f.severity == sev]
        if not group:
            continue
        lines += ["", f"## {sev}"]
        for code in sorted({f.code for f in group}):
            lines += ["", f"### {code}", ""]
            lines += [
                f"- `{f.path or '(wiki)'}`: {f.message}"
                for f in group
                if f.code == code
            ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
