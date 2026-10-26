"""Deterministic lint: the quality gate for the page contract and the caps."""

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

from rapidfuzz import fuzz

from llm_wiki.config import WikiConfig
from llm_wiki.errors import WikiError
from llm_wiki.index import GENERATED_MARK
from llm_wiki.manifest import Manifest
from llm_wiki.pages import ID_RE, SOURCELESS_TYPES, SYSTEM_FILES, Page, id_for, iter_pages
from llm_wiki.screen import find_injection, redact

Severity = Literal["error", "warn"]

LOG_RE = r"^## \[\d{4}-\d{2}-\d{2}\] [a-z-]+ \| .+$"
_LOG = re.compile(LOG_RE)
_ID = re.compile(ID_RE)
_FOOTNOTE = re.compile(r"^\[\^([^\]]+)\]:\s*(\S+)", re.M)
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_FENCE = re.compile(r"^\s*(```|~~~)")
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)")
# page lint only: a page may legitimately say "update CLAUDE.md when ..."
_WARN_INJECTION = {"agent_config"}
_EXTERNAL = ("http:", "https:", "mailto:")
_ORPHAN_EXEMPT = {"overview", "glossary", "hub"}
_SKIP_SECTIONS = {"tl;dr", "related"}
_MIN_WORDS = 12
_TITLE_RATIO = 90
_NOT_UTF8 = "not valid UTF-8"
# the check a system file feeds; an unreadable file is reported under that code
_SYSTEM_CODE = {"log.md": "log-format", "index.md": "index-too-long", "_index.md": "not-in-index"}


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


def _md_links(text: str) -> list[str]:
    """Relative ``.md`` link targets with anchors stripped."""
    targets = []
    for line in _strip_code(text):
        for m in _LINK.finditer(_CODE_SPAN.sub("", line)):
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
    """Resolve a wiki-root-relative citation; None unless it stays inside ``root/raw``."""
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


def _uncited(body: str) -> list[str]:
    """Paragraphs and bullets of >= 12 words without a footnote reference."""
    blocks: list[list[str]] = []
    cur: list[str] | None = None
    skip_level = 0  # inside a TL;DR/Related section of this heading level
    in_unverified = False

    def close() -> None:
        nonlocal cur
        if cur:
            blocks.append(cur)
        cur = None

    for line in _strip_code(body):
        stripped = line.strip()
        h = _HEADING.match(stripped)
        if h:
            close()
            in_unverified = False
            level = len(h.group(1))
            if skip_level and level <= skip_level:
                skip_level = 0
            if h.group(2).strip().strip("#").strip().lower() in _SKIP_SECTIONS:
                skip_level = level
            continue
        if not stripped:
            close()
            in_unverified = False
            continue
        if skip_level:
            continue
        if stripped.startswith(">"):
            if stripped.lstrip("> ").lower().startswith("[!unverified]"):
                close()
                in_unverified = True
            if in_unverified:
                continue
        elif in_unverified:
            in_unverified = False
        if stripped.startswith("|") or _FOOTNOTE.match(stripped):
            close()
            continue
        if _BULLET.match(line) or cur is None:
            close()
            cur = [stripped]
        else:
            cur.append(stripped)
    close()
    out = []
    for b in blocks:
        text = " ".join(b)
        if text.startswith("**TL;DR**") or "[^" in text:
            continue
        if len(_BULLET.sub("", text).split()) >= _MIN_WORDS:
            out.append(text)
    return out


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


def _routing(kind: str, page: Page) -> str | None:
    fm = page.fm
    assert fm is not None
    if kind == "to-be":
        if fm.status != "to-be":
            return f"status '{fm.status}' in a to-be section"
    elif kind == "decisions":
        if fm.type != "decision":
            return f"type '{fm.type}' in a decisions section (must be 'decision')"
        if fm.adr_status is None:
            return "decision without adr_status"
        if fm.status == "to-be" and fm.adr_status != "proposed":
            return f"to-be decision with adr_status '{fm.adr_status}' (must be 'proposed')"
    elif fm.status == "to-be":
        return f"status 'to-be' in a {kind} section"
    return None


def _cap(out: list[Finding], code: str, path: str, what: str, n: int, max_: int, warn: int) -> None:
    if n > max_:
        out.append(Finding("error", code, path, f"{n} {what} exceeds the cap of {max_}"))
    elif n >= warn:
        out.append(Finding("warn", code + "-warn", path, f"{n} {what} reaches the warning level of {warn}"))


def _page_checks(root: Path, page: Page, ctx: dict) -> list[Finding]:
    out: list[Finding] = []
    path = "wiki/" + page.rel
    caps = ctx["caps"]

    def add(sev: Severity, code: str, msg: str) -> None:
        out.append(Finding(sev, code, path, msg))

    for target in _md_links(page.body):
        if not _resolve(page.path.parent, target).is_file():
            add("error", "broken-link", f"link target '{target}' does not exist")
    if page.line_count > caps.max_page_lines:
        add("error", "page-too-long", f"{page.line_count} lines (max {caps.max_page_lines})")
    _, counts = redact(page.body)
    if sum(counts.values()):
        add("error", "secret-in-page", "possible secret: " + ", ".join(sorted(counts)))
    hits = find_injection(page.body)
    for sev, group in (("error", [h for h in hits if h.kind not in _WARN_INJECTION]),
                       ("warn", [h for h in hits if h.kind in _WARN_INJECTION])):
        if group:
            add(sev, "injection-in-page", "; ".join(f"line {h.line} {h.kind}: {h.excerpt}" for h in group))
    if page.error is not None:
        add("error", "frontmatter-invalid", page.error)
        return out

    fm = page.fm
    assert fm is not None
    want = id_for(page.rel)
    if fm.id != want or not _ID.match(fm.id):
        add("error", "id-mismatch", f"id '{fm.id}' should be '{want}'")

    section = _section_of(page.rel, ctx["kinds"])
    if section is not None:
        link = page.rel.removeprefix(section + "/")
        if link not in ctx["index_targets"][section]:
            add("error", "not-in-index", f"missing from {section}/_index.md")
        reason = _routing(ctx["kinds"][section], page)
        if reason:
            add("error", "section-routing", reason)

    if fm.type not in _ORPHAN_EXEMPT and page.path.resolve() not in ctx["inbound"]:
        add("warn", "orphan", "no inbound link from any other page")

    if not any(line.startswith("**TL;DR**") for line in page.body.split("\n")):
        add("error", "missing-tldr", "no line starting with **TL;DR**")
    if not fm.sources and fm.type not in SOURCELESS_TYPES:
        add("error", "missing-sources", f"type '{fm.type}' needs sources")

    for m in _FOOTNOTE.finditer("\n".join(_strip_code(page.body))):
        label, target = m.group(1), m.group(2)
        p = _in_raw(root, target)
        if p is None:
            add("error", "citation-outside-raw", f"footnote [^{label}] cites '{target}' outside raw/")
        elif not p.is_file():
            add("error", "citation-missing", f"footnote [^{label}] cites missing '{target}'")

    for src in fm.sources:
        p = _in_raw(root, src.path)
        if p is None:
            add("error", "citation-outside-raw", f"source '{src.path}' resolves outside raw/")
        elif not p.is_file():
            add("error", "citation-missing", f"source '{src.path}' does not exist")
        entry = ctx["manifest"].entries.get(src.path)
        if entry is None or entry.status == "deleted":
            add("error", "sha-mismatch", f"source '{src.path}' is not in the manifest")
        elif entry.sha256 != src.sha256:
            add("error", "sha-mismatch", f"source '{src.path}' sha256 differs from the manifest")

    for text in _uncited(page.body):
        add("warn", "uncited-paragraph", f"no citation: {text[:60]}")

    if fm.status == "superseded" and not fm.superseded_by:
        add("error", "superseded-no-successor", "superseded page without superseded_by")
    if fm.review_after < ctx["today"]:
        add("warn", "stale", f"review_after {fm.review_after.isoformat()} has passed")
    return out


def _system_files(wiki: Path) -> list[Path]:
    return sorted(
        p for p in wiki.rglob("*.md")
        if p.is_file() and p.name in SYSTEM_FILES and "_archive" not in p.relative_to(wiki).parts[:-1]
    )


def lint(root: Path, cfg: WikiConfig, only: list[Path] | None = None, today: date | None = None) -> LintReport:
    """Lint the wiki at ``root``.

    ``only`` limits per-page checks to those pages (absolute or relative to
    ``root``); wiki-wide checks always run. A path that is not a page raises
    WikiError.
    """
    root = Path(root).resolve()
    wiki = root / "wiki"
    caps = cfg.effective_caps()
    manifest = Manifest.load(root)
    pages = list(iter_pages(root))
    kinds = {s.name: s.kind for s in cfg.sections}
    findings: list[Finding] = []

    # inbound links between pages; system files (indexes, log) do not count
    inbound: set[Path] = set()
    for p in pages:
        for target in _md_links(p.body):
            dest = _resolve(p.path.parent, target)
            if dest != p.path.resolve():
                inbound.add(dest)

    ctx = {
        "caps": caps,
        "kinds": kinds,
        "manifest": manifest,
        "inbound": inbound,
        "today": today or date.today(),
        "index_targets": {s: _index_targets(root, s) for s in kinds},
    }
    selected = pages
    if only is not None:
        by_path = {p.path.resolve(): p for p in pages}
        selected = []
        for o in only:
            page = by_path.get((root / o).resolve())
            if page is None:
                raise WikiError(f"not a wiki page: {o}")
            if page not in selected:
                selected.append(page)
    for p in selected:
        findings += _page_checks(root, p, ctx)

    # wiki-wide checks
    valid = [p for p in pages if p.fm is not None]
    first_id: dict[str, str] = {}
    for p in valid:
        assert p.fm is not None
        if p.fm.id in first_id:
            findings.append(Finding("error", "duplicate-id", "wiki/" + p.rel,
                                    f"id '{p.fm.id}' also used by wiki/{first_id[p.fm.id]}"))
        else:
            first_id[p.fm.id] = p.rel
    for i, a in enumerate(valid):
        for b in valid[i + 1:]:
            assert a.fm is not None and b.fm is not None
            if fuzz.token_sort_ratio(a.fm.title.casefold(), b.fm.title.casefold()) >= _TITLE_RATIO:
                findings.append(Finding("warn", "duplicate-title", "wiki/" + b.rel,
                                        f"title '{b.fm.title}' is close to '{a.fm.title}' (wiki/{a.rel})"))

    system_text: dict[Path, str | None] = {}
    for f in _system_files(wiki):
        rel = f.relative_to(root).as_posix()
        text = system_text[f] = _read(f)
        if text is None:
            findings.append(Finding("error", _SYSTEM_CODE.get(f.name, "broken-link"), rel, _NOT_UTF8))
            continue
        for target in _md_links(text):
            if not _resolve(f.parent, target).is_file():
                findings.append(Finding("error", "broken-link", rel, f"link target '{target}' does not exist"))

    live = sum(e.status != "deleted" for e in manifest.entries.values())
    _cap(findings, "cap-sources", "", "sources", live, caps.max_sources, caps.warn_sources)
    _cap(findings, "cap-pages", "", "pages", len(valid), caps.max_pages, caps.warn_pages)
    per_section: dict[str, int] = defaultdict(int)
    for p in valid:
        section = _section_of(p.rel, kinds)
        if section is not None:
            per_section[section] += 1
    for s in kinds:
        _cap(findings, "cap-section", f"wiki/{s}", f"pages in section '{s}'", per_section[s],
             caps.max_section_pages, caps.warn_section_pages)

    index = wiki / "index.md"
    index_text = system_text.get(index)
    if index_text is not None:
        n = len(index_text.splitlines())
        if n > caps.max_index_lines:
            findings.append(Finding("error", "index-too-long", "wiki/index.md",
                                    f"{n} lines (max {caps.max_index_lines})"))
    log = wiki / "log.md"
    log_text = system_text.get(log)
    if log_text is not None:
        for n, line in enumerate(log_text.splitlines(), 1):
            if line.startswith("## ") and not _LOG.match(line):
                findings.append(Finding("error", "log-format", "wiki/log.md",
                                        f"line {n}: expected '## [YYYY-MM-DD] op | text'"))

    findings.sort(key=lambda f: (f.path, f.code, f.message))
    return LintReport(findings)


def write_report(root: Path, report: LintReport, today: date) -> Path:
    """Write ``.wiki/reports/lint-YYYY-MM-DD.md`` grouped by severity, then code."""
    path = Path(root) / ".wiki" / "reports" / f"lint-{today.isoformat()}.md"
    lines = [f"# Lint report {today.isoformat()}", "", f"{report.errors} errors, {report.warnings} warnings"]
    for sev in ("error", "warn"):
        group = [f for f in report.findings if f.severity == sev]
        if not group:
            continue
        lines += ["", f"## {sev}"]
        for code in sorted({f.code for f in group}):
            lines += ["", f"### {code}", ""]
            lines += [f"- `{f.path or '(wiki)'}`: {f.message}" for f in group if f.code == code]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
