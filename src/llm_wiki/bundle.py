"""Page bundles: a Markdown source and the local images it links are one source.

A member's raw path follows from the page's path and text alone
(``raw/<page path>/<file name>``), so a raw page finds its members again in any
later run. Nothing here reads the manifest or calls an LLM.
"""

import posixpath
import re
from collections import defaultdict
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from llm_wiki.config import slug
from llm_wiki.convert import IMAGE_SUFFIXES, NEVER_INGEST_SUFFIXES, read_text

PAGE_SUFFIX = ".md"  # lowercase; compare through is_page
# ![alt](target), the target with one level of parentheses or in <angle brackets>
_IMAGE_LINK = re.compile(
    r"!\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^()\n]*(?:\([^()\n]*\)[^()\n]*)*)\)"
)
_TITLE = re.compile(r"""\s+(?:"[^"\n]*"|'[^'\n]*')\s*$""")
_REMOTE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]+:|/)")
_FENCE = re.compile(r"^\s*(```|~~~)")
_CODE_SPAN = re.compile(r"`[^`\n]*`")
_HEADING_START = re.compile(r"^(?=#{1,6} )", re.MULTILINE)
_PARAGRAPH_END = re.compile(r"(?<=\n\n)")


@dataclass(frozen=True)
class Link:
    written: str  # the target as the page writes it
    path: str  # decoded and normalized, relative to the page
    name: str  # the file name it gets in the page's folder under raw/


@dataclass(frozen=True)
class Member:
    link: str  # Link.path
    path: str  # the raw path
    name: str  # what a locator calls it


def is_page(name: str | Path) -> bool:
    return Path(name).suffix.lower() == PAGE_SUFFIX


def file_name(name: str) -> str:
    """``My Diagram.PNG`` -> ``my-diagram.png``; a credential suffix stays whole."""
    lower = name.lower()
    suffix = Path(lower).suffix
    if not re.fullmatch(r"\.[a-z0-9]+", suffix):
        suffix = ""  # "notes.final draft" has no extension
    suffix = next((s for s in NEVER_INGEST_SUFFIXES if lower.endswith(s)), suffix)
    stem = name[: len(name) - len(suffix)] if suffix else name
    return (slug(stem) or "file") + suffix.lower()


def _outside_code(text: str) -> Iterator[str]:
    fence: str | None = None
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)
        elif fence is not None:
            if line.strip().startswith(fence):
                fence = None
        else:
            yield _CODE_SPAN.sub("", line)


def image_links(text: str) -> list[str]:
    """Every image link target outside code, in page order, without its title."""
    out = []
    for line in _outside_code(text):
        for m in _IMAGE_LINK.finditer(line):
            target = _TITLE.sub("", m.group(1).strip())
            out.append(target[1:-1] if target.startswith("<") else target)
    return [t for t in out if t]


def local_links(text: str) -> tuple[list[Link], list[str]]:
    """The relative image links, each once, and why the other links are ignored."""
    links: dict[str, Link] = {}
    names: set[str] = set()
    ignored: list[str] = []
    for target in image_links(text):
        if _REMOTE.match(target):
            ignored.append(f"remote: {target}")
            continue
        path = unquote(re.split(r"[?#]", target, maxsplit=1)[0]).replace("\\", "/")
        path = posixpath.normpath(path)
        if Path(path).suffix.lower() not in IMAGE_SUFFIXES:
            ignored.append(f"not an image type: {target}")
        elif path not in links:
            links[path] = Link(
                target, path, _free_name(file_name(Path(path).name), names)
            )
    return list(links.values()), ignored


def _free_name(name: str, taken: set[str]) -> str:
    p = Path(name)
    out, n = name, 2
    while out in taken:
        out = f"{p.stem}-{n}{p.suffix}"
        n += 1
    taken.add(out)
    return out


def member_rel(page_rel: str, name: str) -> str:
    """Where member ``name`` of raw page ``page_rel`` lives."""
    return f"{Path(page_rel).with_suffix('').as_posix()}/{name}"


def find_members(page: Path, tree: Path) -> tuple[list[tuple[Link, Path]], list[str]]:
    """(the links of ``page`` that resolve to a file inside ``tree``, in link order;
    why the others are ignored). The page's own folder comes first: that is where
    an earlier run placed them.
    """
    links, ignored = local_links(read_text(page))
    top = tree.resolve()
    found: list[tuple[Link, Path]] = []
    for link in links:
        candidates = (
            page.parent / slug(page.stem) / link.name,
            page.parent / link.path,
            page.parent / link.written,
        )
        for candidate in candidates:
            target = candidate.resolve()
            if target.is_file() and target.is_relative_to(top):
                found.append((link, target))
                break
        else:
            ignored.append(f"missing: {link.written}")
    return found, ignored


def page_members(root: Path, rel: str) -> tuple[list[Member], list[str]]:
    """The members of raw source ``rel`` as they are in raw/, and the ignored links."""
    root = Path(root)
    page = root / rel
    if not is_page(rel) or not page.is_file():
        return [], []
    found, ignored = find_members(page, root / "raw")
    base = root.resolve()
    return [
        Member(link.path, target.relative_to(base).as_posix(), link.name)
        for link, target in found
    ], ignored


def _raw_pages(root: Path) -> Iterator[str]:
    raw = root / "raw"
    if not raw.is_dir():
        return
    for p in sorted(raw.rglob("*")):
        rel = p.relative_to(raw)
        visible = not any(part.startswith(".") for part in rel.parts)
        if visible and is_page(p.name) and p.is_file():
            yield p.relative_to(root).as_posix()


def member_owners(root: Path) -> dict[str, list[str]]:
    """Raw path -> the raw pages it is a member of, including the paths in a page's
    folder that a link names but that hold no file yet.
    """
    root = Path(root)
    owners: dict[str, list[str]] = defaultdict(list)
    for rel in _raw_pages(root):
        links, _ = local_links(read_text(root / rel))
        members, _ = page_members(root, rel)
        paths = {member_rel(rel, link.name) for link in links}
        for path in sorted(paths | {m.path for m in members}):
            owners[path].append(rel)
    return dict(owners)


def inbox_members(
    inbox: Path, pairs: list[tuple[Path, str]]
) -> dict[Path, list[tuple[Path, str]]]:
    """Page file -> its linked images in ``inbox`` as (file, raw path), for the
    (file, raw path) ``pairs`` that ``intake_inbox`` gives.
    """
    out: dict[Path, list[tuple[Path, str]]] = {}
    for src, rel in pairs:
        if not is_page(rel):
            continue
        found, _ = find_members(src, inbox)
        if found:
            out[src] = [(p, member_rel(rel, link.name)) for link, p in found]
    return out


def _to_send(text: str, sent: Collection[str]) -> set[str]:
    return {link.path for link in local_links(text)[0] if link.path in sent}


def _cut_after_images(text: str, sent: Collection[str], max_images: int) -> int:
    """The offset just after the link that fills ``max_images``."""
    seen: set[str] = set()
    for m in _IMAGE_LINK.finditer(text):
        seen |= _to_send(m.group(0), sent)
        if len(seen) >= max_images:
            return m.end()
    return len(text)


def _fit(text: str, sent: Collection[str], max_images: int, limit: int) -> list[str]:
    """``text`` in pieces that each fit one part: paragraphs, then lines, then a cut
    between two links or, for a long line of plain text, at the character limit.
    """
    over_images = len(_to_send(text, sent)) > max_images
    if len(text) <= limit and not over_images:
        return [text]
    for pieces in (_PARAGRAPH_END.split(text), text.splitlines(keepends=True)):
        pieces = [p for p in pieces if p]
        if len(pieces) > 1:
            return [f for p in pieces for f in _fit(p, sent, max_images, limit)]
    if over_images:
        cut = _cut_after_images(text, sent, max_images)
        return [
            *_fit(text[:cut], sent, max_images, limit),
            *_fit(text[cut:], sent, max_images, limit),
        ]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def split_parts(
    body: str, sent: Collection[str], max_images: int, limit: int
) -> list[str]:
    """``body`` in parts of at most ``limit`` characters and ``max_images`` new images
    to send: runs of headings, cut finer only when one heading holds too much.

    ``sent`` holds the ``Link.path`` of each image to send. An image counts in the
    first part that links it, since it is sent only there.
    """
    parts: list[str] = []
    current = ""
    in_current: set[str] = set()
    earlier: set[str] = set()
    for block in filter(None, _HEADING_START.split(body)):
        for piece in _fit(block, sent, max_images, limit):
            new = _to_send(piece, sent) - earlier
            fits = len(in_current | new) <= max_images
            if current and not (fits and len(current) + len(piece) <= limit):
                parts.append(current)
                earlier |= in_current
                current, in_current = "", set()
                new -= earlier
            current += piece
            in_current |= new
    if current:
        parts.append(current)
    return parts
