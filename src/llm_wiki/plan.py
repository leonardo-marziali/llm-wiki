"""Deterministic validation of an ingest plan before any edit session runs."""

import re
from collections import defaultdict
from pathlib import Path

from llm_wiki.config import WikiConfig
from llm_wiki.lint import routing_error
from llm_wiki.manifest import Manifest
from llm_wiki.pages import ID_RE, SYSTEM_FILES, iter_pages
from llm_wiki.schemas import PagePlan, Plan

_ID = re.compile(ID_RE)
_TOP_LEVEL = {"overview", "glossary"}  # pages outside any section; update only
_SYSTEM_IDS = {name.removesuffix(".md") for name in SYSTEM_FILES}
_HINT = "run `wiki consolidate` or start a new wiki"


def _page_file(root: Path, id_: str) -> Path:
    if id_ in _TOP_LEVEL:
        return root / "wiki" / f"{id_}.md"
    section, slug = id_.split(".", 1)
    return root / "wiki" / section / f"{slug}.md"


def _id_errors(p: PagePlan, kinds: dict[str, str]) -> list[str]:
    """Why ``p.id`` cannot name a plannable page (empty: it can)."""
    if p.id in _SYSTEM_IDS or p.id.startswith("_"):
        return [f"{p.id}: system pages are maintained by the tool, not planned"]
    if not _ID.match(p.id):
        return [f"{p.id}: id must be lowercase words joined by '-' and '.'"]
    if p.id in _TOP_LEVEL:
        if p.action != "update":
            return [f"{p.id}: this page can only be updated, not created"]
        return []
    section, _, slug = p.id.partition(".")
    if not slug or "." in slug:
        return [f"{p.id}: id must be '<section>.<slug>' (or overview/glossary)"]
    kind = kinds.get(section)
    if kind is None:
        return [f"{p.id}: unknown section '{section}'"]
    if kind == "sources":
        return [f"{p.id}: section '{section}' holds source pages written by the tool"]
    return []


def _existence_error(p: PagePlan, root: Path) -> str | None:
    exists = _page_file(root, p.id).is_file()
    if p.action == "create" and exists:
        return f"{p.id}: action is create but the page already exists"
    if p.action == "update" and not exists:
        return f"{p.id}: action is update but the page does not exist"
    return None


def _cite_ok(root: Path, manifest: Manifest, path: str) -> bool:
    entry = manifest.entries.get(path)
    if entry is None or entry.status in ("deleted", "quarantined"):
        return False
    raw = (root / "raw").resolve()
    target = (root / path).resolve()
    return target.is_relative_to(raw) and target.is_file()


def _cap_errors(plan: Plan, root: Path, cfg: WikiConfig, kinds: dict) -> list[str]:
    """Creates are blocked once a cap would be exceeded; updates never are."""
    caps = cfg.effective_caps()
    # counted as lint counts: pages with valid frontmatter, per section by folder
    current = [pg for pg in iter_pages(root) if pg.fm is not None]
    per_section: dict[str, int] = defaultdict(int)
    for pg in current:
        if "/" in pg.rel:
            per_section[pg.rel.split("/", 1)[0]] += 1
    creates = [p for p in plan.pages if p.action == "create"]
    errors: list[str] = []
    if creates and len(current) + len(creates) > caps.max_pages:
        ids = ", ".join(p.id for p in creates)
        errors.append(
            f"{ids}: {len(current)} pages + {len(creates)} creates exceeds the "
            f"cap of {caps.max_pages}; {_HINT}"
        )
    by_section: dict[str, list[str]] = defaultdict(list)
    for p in creates:
        section = p.id.split(".", 1)[0]
        if section in kinds:
            by_section[section].append(p.id)
    for section, ids in by_section.items():
        if per_section[section] + len(ids) > caps.max_section_pages:
            errors.append(
                f"{', '.join(ids)}: {per_section[section]} pages + {len(ids)} "
                f"creates exceeds the cap of {caps.max_section_pages} in section "
                f"'{section}'; {_HINT}"
            )
    return errors


def _page_errors(
    p: PagePlan,
    *,
    root: Path,
    kinds: dict[str, str],
    source: str,
    manifest: Manifest,
    allowed_updates: set[str] | None,
) -> list[str]:
    errors = _id_errors(p, kinds)
    if errors:
        return errors
    if (err := _existence_error(p, root)) is not None:
        errors.append(err)
    kind = kinds.get(p.id.split(".", 1)[0])
    if kind is not None and (
        reason := routing_error(kind, p.type, p.status, p.adr_status)
    ):
        errors.append(f"{p.id}: {reason}")
    errors += [
        f"{p.id}: claim cites '{c.source}', not this source or a known raw file"
        for c in p.claims
        if c.source != source and not _cite_ok(root, manifest, c.source)
    ]
    outside = allowed_updates is not None and p.id not in allowed_updates
    if p.action == "update" and outside:
        errors.append(
            f"{p.id}: a changed source may only update pages it already "
            "backs; this page is not one of them"
        )
    return errors


def validate_plan(
    plan: Plan,
    *,
    root: Path,
    cfg: WikiConfig,
    source: str,
    manifest: Manifest,
    allowed_updates: set[str] | None,
) -> list[str]:
    """Human-readable problems with ``plan``; an empty list means it may proceed.

    ``allowed_updates`` is None for a new source; for a changed source it holds
    the ids of the pages the source already backs.
    """
    kinds = {s.name: s.kind for s in cfg.sections}
    max_pages = cfg.effective_caps().max_pages_per_ingest
    errors: list[str] = []
    if len(plan.pages) > max_pages:
        errors.append(
            f"plan has {len(plan.pages)} pages, more than the cap of {max_pages}"
        )
    seen: set[str] = set()
    for p in plan.pages:
        if p.id in seen:
            errors.append(f"{p.id}: planned more than once")
            continue
        seen.add(p.id)
        errors += _page_errors(
            p,
            root=Path(root),
            kinds=kinds,
            source=source,
            manifest=manifest,
            allowed_updates=allowed_updates,
        )
    return errors + _cap_errors(plan, Path(root), cfg, kinds)
