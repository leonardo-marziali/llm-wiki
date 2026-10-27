"""Read-only summary of a wiki: counts against caps, staleness, pending work."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from llm_wiki.config import load_config
from llm_wiki.manifest import Manifest
from llm_wiki.pages import iter_pages
from llm_wiki.registry import WikiRef

LOG_LINES = 5
LOG_NOT_UTF8 = "error: log.md not valid UTF-8"


@dataclass
class WikiStatus:
    name: str
    path: str
    description: str
    sources: int
    max_sources: int
    pages: int
    max_pages: int
    sections: dict[str, int]
    stale: list[str]
    inbox: int
    quarantined: int
    pending: dict[str, int]
    last_log: list[str]


def _count_files(d: Path, skip: tuple[str, ...]) -> int:
    if not d.is_dir():
        return 0
    return sum(
        1
        for p in d.iterdir()
        if p.is_file() and not p.name.endswith(skip) and p.name != ".gitkeep"
    )


def wiki_status(ref: WikiRef, today: date | None = None) -> WikiStatus:
    today = today or date.today()
    root = ref.path
    cfg = load_config(root)
    caps = cfg.effective_caps()
    manifest = Manifest.load(root)
    scan = manifest.scan(root)

    sections = {s.name: 0 for s in cfg.sections}
    stale: list[str] = []
    pages = 0
    for page in iter_pages(root):
        if page.fm is None:
            continue
        pages += 1
        parts = page.rel.split("/")
        if len(parts) > 1 and parts[0] in sections:
            sections[parts[0]] += 1
        if page.fm.review_after < today:
            stale.append(page.fm.id)

    log = root / "wiki" / "log.md"
    entries = []
    if log.is_file():
        try:
            entries = [
                ln
                for ln in log.read_text(encoding="utf-8").splitlines()
                if ln.startswith("## [")
            ]
        except UnicodeDecodeError:
            entries = [LOG_NOT_UTF8]
        except OSError as e:
            entries = [f"error: log.md unreadable: {e.strerror or e}"]

    return WikiStatus(
        name=ref.name,
        path=str(root),
        description=cfg.description,
        sources=sum(1 for e in manifest.entries.values() if e.status != "deleted"),
        max_sources=caps.max_sources,
        pages=pages,
        max_pages=caps.max_pages,
        sections=sections,
        stale=stale,
        inbox=_count_files(root / "inbox", ()),
        quarantined=_count_files(root / ".wiki" / "quarantine", (".reason.json",)),
        pending={
            "new": len(scan.new),
            "changed": len(scan.changed),
            "deleted": len(scan.deleted),
        },
        last_log=entries[-LOG_LINES:],
    )
