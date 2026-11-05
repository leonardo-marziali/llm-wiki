"""Page bundles: a Markdown source and the local images it links are one source.

A member's raw path follows from the page's path and text alone
(``raw/<page path>/<file name>``), so a raw page finds its members again in any
later run. Nothing here reads the manifest or calls an LLM.
"""

import html
import posixpath
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from llm_wiki.config import slug
from llm_wiki.convert import IMAGE_SUFFIXES, NEVER_INGEST_SUFFIXES, read_text

PAGE_SUFFIX = ".md"  # lowercase; compare through is_page
# ![[wiki embed]], ![alt](target), ![alt][label], ![label] and <img src=...>; the
# target may hold one level of parentheses or sit in <angle brackets>. The last
# two alternatives are the closing half of a plain link, ](target) and ][label];
# an embed matches first and so never reaches them, and a thumbnail's outer link
# still does.
_IMAGE_TOKEN = re.compile(
    r"!\[\[(?P<wiki>[^\]\n|]+)(?:\|[^\]\n]*)?\]\]"
    r"|!\[(?P<alt>(?:[^\[\]]|\[[^\[\]]*\]){0,1000})\]"
    r"(?:\(\s*+(?P<target><[^>\n]{1,4096}>|[^()\n]*(?:\([^()\n]*\)[^()\n]*)*)\)"
    r"|\[(?P<label>[^\]\n]*)\])?"
    r"""|<img\b[^>]*?\bsrc\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)')"""
    r"|\]\(\s*+(?P<plain><[^>\n]{1,4096}>|[^()\n]*(?:\([^()\n]*\)[^()\n]*)*)\)"
    r"|\]\[(?P<ref>[^\]\n]*)\]",
    re.IGNORECASE,
)
_DEFINITION = re.compile(
    r"^ {0,3}\[(?P<label>[^\]\n]+)\]:[ \t]*(?P<target><[^>\n]+>|\S+)", re.MULTILINE
)
_TITLE = re.compile(r"""(?<!\s)\s++(?:"[^"\n]*"|'[^'\n]*')\s*$""")
_REMOTE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]+:|/)")
_FENCE = re.compile(r"^\s*(```|~~~)")
_CODE_SPAN = re.compile(r"`[^`\n]*`")
_HEADING = re.compile(r"#{1,6} ")
_PARAGRAPH_END = re.compile(r"(?<=\n\n)")


@dataclass(frozen=True)
class Link:
    written: str  # the target as the page writes it
    path: str  # decoded and normalized, relative to the page
    name: str  # the file name it gets in the page's folder under raw/
    alt: str = ""  # its name while another link of the page clashes with it


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


def _clean_target(target: str) -> str:
    target = _TITLE.sub("", target.strip())
    return target[1:-1] if target.startswith("<") else target


def definitions(text: str) -> dict[str, str]:
    """Reference link definitions: lowercased label -> target; the first one counts."""
    out: dict[str, str] = {}
    for m in _DEFINITION.finditer(text):
        out.setdefault(m["label"].casefold(), _clean_target(m["target"]))
    return out


def _tagged_targets(text: str, defs: dict[str, str]) -> Iterator[tuple[str, bool]]:
    """Each image link target, and whether the page embeds it."""
    for m in _IMAGE_TOKEN.finditer(text):
        if m["plain"] is not None or m["ref"] is not None:
            if m["plain"] is not None:
                target = _clean_target(m["plain"])
            else:
                target = defs.get(m["ref"].casefold(), "")
            path = _local_path(target) if target else None
            if path is not None and Path(path).suffix.lower() in IMAGE_SUFFIXES:
                yield target, False
            continue
        if m["wiki"]:
            target = m["wiki"].strip()
        elif m["target"] is not None:
            target = _clean_target(m["target"])
        elif m["alt"] is not None:
            target = defs.get((m["label"] or m["alt"]).casefold(), "")
        else:
            target = html.unescape(m["dq"] if m["dq"] is not None else m["sq"])
        if target:
            yield target, True


def _targets(text: str, defs: dict[str, str]) -> Iterator[str]:
    return (target for target, _ in _tagged_targets(text, defs))


def image_links(text: str) -> list[str]:
    """Every image link target outside code, in page order, without its title."""
    return list(_targets("\n".join(_outside_code(text)), definitions(text)))


def _local_path(target: str) -> str | None:
    """A relative target, decoded and normalized; None for a remote or absolute one."""
    if _REMOTE.match(target):
        return None
    path = unquote(re.split(r"[?#]", target, maxsplit=1)[0]).replace("\\", "/")
    return posixpath.normpath(path)


def link_paths(text: str, defs: dict[str, str]) -> list[str]:
    """The relative image paths in a piece of a page, each time it links one.

    A piece may start inside a code fence, so code is not told apart here: the
    caller compares the paths to the images it knows from the whole page.
    """
    paths = (_local_path(t) for t in _targets(text, defs))
    return [p for p in paths if p]


def local_links(text: str) -> tuple[list[Link], list[str]]:
    """The relative image links, each once, and why the other links are ignored.

    A name follows from the link, not from its place in the page: links that share
    a file name each get their folder's name in front, so editing the page out of
    one link can never hand the other one its picture. Embeds are named first and
    only against each other, so a plain link can never rename an embed that a run
    has already placed.
    """
    written: dict[str, str] = {}
    embedded: dict[str, None] = {}  # in the order the page embeds them
    ignored: list[str] = []
    readable = "\n".join(_outside_code(text))
    for target, is_embed in _tagged_targets(readable, definitions(text)):
        path = _local_path(target)
        if path is None:
            ignored.append(f"remote: {target}")
        elif Path(path).suffix.lower() not in IMAGE_SUFFIXES:
            ignored.append(f"not an image type: {target}")
        else:
            written.setdefault(path, target)
            if is_embed:
                embedded[path] = None
    plain = {path: file_name(Path(path).name) for path in written}
    alts = {path: _folder_name(path, plain[path]) for path in written}
    uses = Counter(plain[path] for path in embedded)
    taken: set[str] = set()
    names: dict[str, str] = {}
    for path in embedded:
        name = plain[path] if uses[plain[path]] == 1 else alts[path]
        names[path] = _free_name(name, taken)
    linked = sorted(set(written) - set(embedded))  # sorted, so order never matters
    uses = Counter(plain[path] for path in linked)
    for path in linked:
        free = uses[plain[path]] == 1 and plain[path] not in taken
        names[path] = _free_name(plain[path] if free else alts[path], taken)
    # an alt another link is named never finds that link's picture
    alts = {p: names[p] if a in taken else a for p, a in alts.items()}
    return [Link(t, p, names[p], alts[p]) for p, t in written.items()], ignored


def _folder_name(path: str, name: str) -> str:
    folder = slug(Path(path).parent.as_posix())
    return f"{folder}-{name}" if folder else name


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


def _inside(candidate: Path, top: Path) -> Path | None:
    """``candidate`` resolved, if it is a file inside ``top``. Link text is untrusted:
    a name git or the OS refuses (a NUL, a very long one) is simply not there.
    """
    try:
        target = candidate.resolve()
        return target if target.is_file() and target.is_relative_to(top) else None
    except (OSError, ValueError):
        return None


def find_members(
    page: Path, tree: Path, *, in_raw: bool = False
) -> tuple[list[tuple[Link, Path]], list[str]]:
    """(the links of ``page`` that resolve to a file inside ``tree``, in link order;
    why the others are ignored).

    In a source tree the link's own target comes first, then the page's folder,
    where a failed run or the source cap may have put it. In raw/ the page's folder
    comes first: that is where a run placed them.
    """
    links, ignored = local_links(read_text(page))
    top = tree.resolve()
    folder = page.parent / slug(page.stem)
    found: list[tuple[Link, Path]] = []
    for link in links:
        placed = (
            [folder / link.alt, folder / link.name]
            if link.alt
            else [folder / link.name]
        )
        real = [page.parent / link.path, page.parent / link.written]
        for candidate in [*placed, *real] if in_raw else [*real, *placed]:
            if target := _inside(candidate, top):
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
    found, ignored = find_members(page, root / "raw", in_raw=True)
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


Count = Callable[[str], set[str]]  # the images to send a piece of text links


def _cut_after_images(text: str, count: Count, max_images: int) -> int:
    """The offset just after the link that fills ``max_images``."""
    seen: set[str] = set()
    for m in _IMAGE_TOKEN.finditer(text):
        seen |= count(m.group(0))
        if len(seen) >= max_images:
            return m.end()
    return len(text)


def _fit(text: str, count: Count, max_images: int, limit: int) -> list[str]:
    """``text`` in pieces that each fit one part: paragraphs, then lines, then a cut
    between two links or, for a long line of plain text, at the character limit.
    """
    over_images = len(count(text)) > max_images
    if len(text) <= limit and not over_images:
        return [text]
    for pieces in (_PARAGRAPH_END.split(text), text.splitlines(keepends=True)):
        pieces = [p for p in pieces if p]
        if len(pieces) > 1:
            return [f for p in pieces for f in _fit(p, count, max_images, limit)]
    if over_images:
        cut = _cut_after_images(text, count, max_images)
        return [
            *_fit(text[:cut], count, max_images, limit),
            *_fit(text[cut:], count, max_images, limit),
        ]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _heading_blocks(body: str) -> list[str]:
    """``body`` cut before each heading outside a code fence."""
    blocks: list[str] = []
    current = ""
    fence: str | None = None
    for line in body.splitlines(keepends=True):
        m = _FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)
        elif fence is not None and line.strip().startswith(fence):
            fence = None
        elif fence is None and _HEADING.match(line) and current:
            blocks.append(current)
            current = ""
        current += line
    return [*blocks, current] if current else blocks


def split_parts(
    body: str, sent: Collection[str], max_images: int, limit: int
) -> list[str]:
    """``body`` in parts of at most ``limit`` characters and ``max_images`` new images
    to send: runs of headings, cut finer only when one heading holds too much.

    ``sent`` holds the ``Link.path`` of each image to send. An image counts in the
    first part that links it, since it is sent only there.
    """
    defs = definitions(body)

    def count(text: str) -> set[str]:
        return set(link_paths(text, defs)) & set(sent)

    parts: list[str] = []
    current = ""
    in_current: set[str] = set()
    earlier: set[str] = set()
    for block in _heading_blocks(body):
        for piece in _fit(block, count, max_images, limit):
            new = count(piece) - earlier
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
