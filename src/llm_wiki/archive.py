"""Deterministic archiving for ``wiki consolidate`` (Ruling 5): a deprecated or
superseded page that no live page links to moves to its folder's ``_archive/``
by ``git mv``. No model is involved.
"""

from collections import defaultdict
from contextlib import suppress
from pathlib import Path

from llm_wiki import gitops
from llm_wiki.index import GENERATED_MARK
from llm_wiki.lint import is_flagged, link_targets
from llm_wiki.maint import RETIRED, live_linkers
from llm_wiki.manifest import Manifest
from llm_wiki.pages import id_for, iter_pages

ARCHIVE = "_archive"
_INDEXES = {"index.md", "_index.md"}
_ALWAYS_LINKED = {"overview", "glossary"}  # the rebuilt index.md links them


def _link_text(path: Path) -> str:
    """The text whose links stay: an index's generated part is rebuilt without
    the archived pages, its hand-written header is kept.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    return text.split(GENERATED_MARK, 1)[0] if path.name in _INDEXES else text


def _linked_from(root: Path) -> dict[Path, set[Path]]:
    """Each link target -> the wiki files outside ``_archive/`` that link it."""
    wiki = root / "wiki"
    out: dict[Path, set[Path]] = defaultdict(set)
    for f in sorted(wiki.rglob("*.md")):
        if not f.is_file() or ARCHIVE in f.relative_to(wiki).parts[:-1]:
            continue
        here = f.resolve()
        for target in link_targets(f.parent, _link_text(f)) - {here}:
            out[target].add(here)
    return out


def _candidates(root: Path) -> dict[str, str]:
    """id -> wiki-root-relative path of every page ``archive_unlinked`` moves.

    A candidate is retired and has no live linker (``maint.live_linkers``, R10:
    the predicate relink jobs use). It still waits while any file that stays
    links it (a retired page that is not archived, open-questions.md, an index
    header): archiving it would break that link. A flagged page stays in sight
    of lint (Ruling 4), and index.md always links the overview and glossary.
    """
    picked: dict[Path, tuple[str, str]] = {}
    for p in iter_pages(root):
        id_ = id_for(p.rel)
        if (
            p.fm is not None
            and p.fm.status in RETIRED
            and id_ not in _ALWAYS_LINKED
            and not is_flagged(p)
            and not live_linkers(root, id_)
        ):
            picked[p.path.resolve()] = (id_, f"wiki/{p.rel}")
    linked_from = _linked_from(root)
    while blocked := [t for t in picked if linked_from[t] - set(picked)]:
        for t in blocked:
            del picked[t]
    return dict(sorted(picked.values()))


def archive_candidates(root: Path) -> list[str]:
    """The ids ``archive_unlinked`` would archive now."""
    return list(_candidates(Path(root)))


def _dest(root: Path, rel: str) -> str:
    """``wiki/<section>/_archive/<slug>.md`` (``wiki/_archive/`` for a top-level
    page), with ``-2``, ``-3`` … when the name is taken.
    """
    folder, name = rel.rsplit("/", 1)
    stem = name.removesuffix(".md")
    dest, n = f"{folder}/{ARCHIVE}/{name}", 2
    while (root / dest).exists():
        dest = f"{folder}/{ARCHIVE}/{stem}-{n}.md"
        n += 1
    return dest


def archive_unlinked(root: Path, manifest: Manifest) -> list[str]:
    """Move every candidate to ``_archive/`` with ``git mv`` and commit the moves;
    drop the archived ids from every ``pages_touched`` of ``manifest`` (the
    caller saves it). Returns the archived ids.
    """
    root = Path(root)
    found = _candidates(root)
    moved: list[str] = []
    made: list[Path] = []  # the _archive/ folders this call creates
    try:
        for rel in found.values():
            dest = _dest(root, rel)
            folder = (root / dest).parent
            if not folder.is_dir():
                folder.mkdir()
                made.append(folder)
            gitops.move(root, rel, dest)
            moved += [rel, dest]
        if moved:
            message = f"consolidate(archive): {', '.join(found)}"
            gitops.stage_commit(root, moved, message, force=moved)
    except BaseException:  # Ctrl-C before the commit: no move is left half-done
        gitops.restore(root, moved)
        for folder in made:
            with suppress(OSError):
                folder.rmdir()
        raise
    gone = set(found)
    for entry in list(manifest.entries.values()):
        kept = [i for i in entry.pages_touched if i not in gone]
        if len(kept) < len(entry.pages_touched):
            manifest.record(entry.model_copy(update={"pages_touched": kept}))
    return list(found)
