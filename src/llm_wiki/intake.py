"""Sources into raw/, then converted and screened: one ``Prepared`` per source.

Nothing here calls an LLM. A source whose text holds injection hits (or, with
``strict_secrets``, any secret) is quarantined before a plan call can see it.
"""

import json
import os
import shutil
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from llm_wiki.bundle import (
    PAGE_SUFFIX,
    Member,
    file_name,
    find_members,
    local_links,
    member_owners,
    member_rel,
    page_members,
    split_parts,
)
from llm_wiki.config import WikiConfig, slug
from llm_wiki.convert import IMAGE_SUFFIXES, Conversion, Splitter, convert
from llm_wiki.errors import RefusedSource, UnsupportedFormat, WikiError
from llm_wiki.images import near_duplicate, triage
from llm_wiki.manifest import (
    BundleMember,
    Manifest,
    SourceEntry,
    category_for,
    mime_for,
    sha256_file,
)
from llm_wiki.screen import Hit, quarantine, redact, screen_text

QUARANTINE_DIR = Path(".wiki") / "quarantine"
TMP_DIR = Path(".wiki") / "tmp"
_REASON = ".reason.json"
_MEMBERS = ".members"  # a quarantined bundle's images: a folder next to the page
_NOTE_WORDS = 6
_KEPT_FLAGS = ("trusted",)  # conversion flags are recomputed on every conversion

Kind = Literal["new", "changed", "deleted"]


@dataclass
class Prepared:
    rel: str  # the raw path
    sha: str
    kind: Kind
    # where a changed version goes when it is refused or fails (absolute)
    origin: Path | None = None
    conversion: Conversion | None = None
    parts: list[str] = field(default_factory=list)  # converted paths to plan in turn
    # per part: the raw images its plan call gets, in link order
    images: dict[str, list[Member]] = field(default_factory=dict)
    # per part: (name, the attached image it is a near-duplicate of)
    same_images: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    members: list[Member] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)  # members no link names any more
    changed_images: list[str] = field(default_factory=list)  # names, since last time
    ignored_images: list[str] = field(default_factory=list)  # why, for the report
    redactions: dict[str, int] = field(default_factory=dict)
    status: Literal["ready", "quarantined", "refused"] = "ready"
    reason: str = ""
    held: Path | None = None  # where quarantine put the raw file

    @property
    def is_image(self) -> bool:
        """The source itself is an image (not a page with images)."""
        return Path(self.rel).suffix.lower() in IMAGE_SUFFIXES

    def image_names(self, part: str) -> set[str]:
        """What a locator of ``part``'s plan may call an image."""
        names = {m.name for m in self.images.get(part, [])}
        same = self.same_images.get(part, [])
        return names | {name for name, first in same if first in names}


def source_slug(rel: str) -> str:
    """``raw/docs/My Guide.md`` -> ``docs-my-guide``."""
    stem = Path(rel).relative_to("raw").with_suffix("").as_posix()
    return slug(stem.replace("/", "-"))


def src_page_id(cfg: WikiConfig, rel: str) -> str:
    """The base id of the tool-written summary page of raw source ``rel``.

    The extension is part of the slug (R22): ``raw/design.pdf`` ->
    ``sources.src-design-pdf``. ``src_id_for`` gives the id actually in use.
    """
    section = next((s.name for s in cfg.sections if s.kind == "sources"), "sources")
    return f"{section}.src-{slug(Path(rel).relative_to('raw').as_posix())}"


def src_id_for(cfg: WikiConfig, manifest: Manifest, rel: str) -> str:
    """The src page id of ``rel``: the one its manifest entry records, else the
    base id.
    """
    entry = manifest.entries.get(rel)
    if entry is not None and entry.src_page:
        return entry.src_page
    return src_page_id(cfg, rel)


def _assign_src_page(cfg: WikiConfig, manifest: Manifest, rel: str) -> str:
    """``rel``'s recorded src page id, or a new one: the base id, with ``-2``,
    ``-3``, … when another manifest entry already owns it.
    """
    entry = manifest.entries.get(rel)
    if entry is not None and entry.src_page:
        return entry.src_page
    taken = {src_id_for(cfg, manifest, p) for p in manifest.entries if p != rel}
    base = src_page_id(cfg, rel)
    out, n = base, 2
    while out in taken:
        out = f"{base}-{n}"
        n += 1
    return out


def _raw_rel(parts: tuple[str, ...]) -> str:
    """The raw path for a file at ``parts`` (folders, then the file name)."""
    dirs = [slug(d) or "misc" for d in parts[:-1]]
    return "/".join(["raw", *dirs, file_name(parts[-1])])


def _unclaimed(root: Path, rel: str, claimed: set[str]) -> str:
    """``rel``, or ``rel`` with ``-2``, ``-3``, … when this batch already uses it.

    ``rel`` itself may exist (the same name: a changed source); a numbered
    alternative never takes the path of an unrelated existing source.
    """
    p = Path(rel)
    out, n = rel, 2
    while out in claimed or (out != rel and (root / out).exists()):
        out = (p.parent / f"{p.stem}-{n}{p.suffix}").as_posix()
        n += 1
    claimed.add(out)
    return out


def _visible_files(top: Path) -> list[Path]:
    """Files under ``top``, skipping dot-files and dot-folders (as Manifest.scan)."""
    return sorted(
        (
            p
            for p in top.rglob("*")
            if p.is_file()
            and not any(part.startswith(".") for part in p.relative_to(top).parts)
        ),
        key=lambda p: p.relative_to(top).as_posix(),
    )


def intake_inbox(root: Path) -> list[tuple[Path, str]]:
    """Each inbox file with its raw target; an existing target is a changed source.

    Nothing is moved: each file goes to raw/ when its turn comes
    (``move_into_raw``).
    """
    inbox = Path(root) / "inbox"
    if not inbox.is_dir():
        return []
    claimed: set[str] = set()
    return [
        (p, _unclaimed(Path(root), _raw_rel(p.relative_to(inbox).parts), claimed))
        for p in _visible_files(inbox)
    ]


def free_path(path: Path) -> Path:
    """``path``, or ``<stem>-2<suffix>`` and so on when a file is there."""
    out, n = path, 2
    while out.exists():
        out = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    return out


def inbox_home(root: Path, rel: str) -> Path:
    """A free inbox path for raw source ``rel``: ``inbox/<path under raw/>``."""
    return free_path(Path(root) / "inbox" / Path(rel).relative_to("raw"))


def move_into_raw(root: Path, src: Path, rel: str) -> None:
    dest = Path(root) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), dest)


def check_paths(paths: list[Path]) -> None:
    """Refuse a path that is missing or unreadable, before anything is copied."""
    for path in map(Path, paths):
        if not path.exists():
            raise WikiError(f"no such file or folder: {path}")
        if not os.access(path, os.R_OK | (os.X_OK if path.is_dir() else 0)):
            raise WikiError(f"cannot read {path}")


def copy_paths(
    root: Path,
    paths: list[Path],
    on_copy: Callable[[str], None] | None = None,
) -> list[str]:
    """Copy files, or folders recursively, into raw/; a folder keeps its name.

    The images a Markdown file links go next to it as members and are not
    returned. ``on_copy`` gets each raw path before its copy is made, so a caller can
    take a half-made copy back after a Ctrl-C.
    """
    check_paths(paths)
    root = Path(root)
    raw = (root / "raw").resolve()
    batches = {
        path: _copy_batch(path)
        for path in map(Path, paths)
        if not path.resolve().is_relative_to(raw)
    }
    tops = [(src, top) for pairs, top in batches.values() for src, _ in pairs]
    linked = {src: find_members(src, top)[0] for src, top in tops if _is_page(src)}
    members = {target for found in linked.values() for _, target in found}
    out: list[str] = []
    claimed: set[str] = set()

    def copy(src: Path, rel: str) -> None:
        if on_copy is not None:
            on_copy(rel)
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, root / rel)

    for path in map(Path, paths):
        if path not in batches:  # already raw sources
            files = _visible_files(path) if path.is_dir() else [path]
            out += [f.resolve().relative_to(raw.parent).as_posix() for f in files]
            continue
        for src, parts in batches[path][0]:
            if src.resolve() in members:
                continue  # a member only, even when it was dropped alone too
            rel = _unclaimed(root, _raw_rel(parts), claimed)
            copy(src, rel)
            out.append(rel)
            for link, target in linked.get(src, []):
                member = member_rel(rel, link.name)
                claimed.add(member)
                copy(target, member)
    return out


def _is_page(path: Path) -> bool:
    return path.suffix.lower() == PAGE_SUFFIX


def _copy_batch(path: Path) -> tuple[list[tuple[Path, tuple[str, ...]]], Path]:
    """(each file with its parts under raw/, the tree its links may reach): a folder
    keeps its name, and a single file reaches its own folder.
    """
    if path.is_dir():
        files = _visible_files(path)
        return [(f, (path.name, *f.relative_to(path).parts)) for f in files], path
    return [(path, (path.name,))], path.parent


def write_note(root: Path, text: str, today: date) -> str:
    """Save ``text`` as raw/notes/<date>-<slug of its first words>.md."""
    # the name ends up in prompts, page ids and git: never a secret value in it
    words = slug(" ".join(redact(text)[0].split()[:_NOTE_WORDS])) or "note"
    notes = Path(root) / "raw" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    base = f"{today.isoformat()}-{words}"
    name, n = f"{base}.md", 2
    while (notes / name).exists():
        name = f"{base}-{n}.md"
        n += 1
    (notes / name).write_text(text if text.endswith("\n") else text + "\n", "utf-8")
    return f"raw/notes/{name}"


def trust(root: Path, name: str) -> str:
    """Release a quarantined source back to its raw path, flagged ``trusted``.

    A raw file at that path (the last ingested version of a changed source)
    is overwritten: git keeps the old version, and the source is then changed.
    """
    root = Path(root)
    qdir = root / QUARANTINE_DIR
    name = name.removesuffix(_REASON)
    if not name or "/" in name or "\\" in name or ".." in name:
        raise WikiError(f"--trust takes a file name in {QUARANTINE_DIR}, not {name!r}")
    held, reason_file = qdir / name, qdir / f"{name}{_REASON}"
    if not (held.is_file() and reason_file.is_file()):
        raise WikiError(f"no quarantined source named {name} in {QUARANTINE_DIR}")
    try:
        reason = json.loads(reason_file.read_text(encoding="utf-8"))
        rel = reason["source"]
    except (ValueError, KeyError, TypeError) as exc:
        raise WikiError(f"{reason_file.name} is corrupt: {exc}") from None
    if reason.get("kind") != "injection":
        raise WikiError(
            f"{name} is quarantined for a secret, not injection; --trust cannot "
            "release it. Remove the secret and add the file again"
        )
    if not isinstance(rel, str) or not rel.startswith("raw/") or ".." in rel:
        raise WikiError(f"{reason_file.name} records no raw path")
    dest = root / rel
    if dest.is_dir():
        raise WikiError(f"{rel} is a folder; remove it before trusting {name}")
    manifest = Manifest.load(root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(held), dest)
    reason_file.unlink()
    _release_members(root, held)
    entry = manifest.entries.get(rel)
    if entry is None:  # e.g. the run that quarantined it was rejected
        entry = _entry(root, rel, sha256_file(dest), None, date.today())
    manifest.record(
        entry.model_copy(
            update={
                "status": "new",
                # pages built from the last ingested version make it a change
                "supersedes_sha": _last_ingested_sha(entry),
                "flags": _merged_flags(entry.flags, ["trusted"]),
                "converted": [],
            }
        )
    )
    manifest.save(root)
    return rel


def _members_folder(held: Path) -> Path:
    return held.with_name(held.name + _MEMBERS)


def held_members(root: Path, name: str) -> dict[str, str]:
    """The images held with quarantined page ``name``: their quarantine path ->
    the raw path ``trust`` puts them back at.
    """
    root = Path(root)
    folder = _members_folder(root / QUARANTINE_DIR / name)
    if not folder.is_dir():
        return {}
    return {
        f.relative_to(root).as_posix(): f"raw/{f.relative_to(folder).as_posix()}"
        for f in folder.rglob("*")
        if f.is_file()
    }


def _release_members(root: Path, held: Path) -> None:
    for kept, rel in held_members(root, held.name).items():
        move_into_raw(root, root / kept, rel)
    shutil.rmtree(_members_folder(held), ignore_errors=True)


def hold_members(
    root: Path, manifest: Manifest, held: Path, members: list[str]
) -> None:
    """Take a quarantined page's images out of raw/ with it. An image another page
    still links, or that is a source itself, stays there, and quarantine gets a copy.
    """
    owners = member_owners(root)  # the page has already left raw/
    for rel in members:
        src = root / rel
        if not src.is_file():
            continue
        dest = _members_folder(held) / Path(rel).relative_to("raw")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if owners.get(rel) or manifest.is_live(rel):
            shutil.copy2(src, dest)
        else:
            shutil.move(str(src), dest)


def removable_members(root: Path, manifest: Manifest, paths: list[str]) -> list[str]:
    """Those of ``paths`` no raw page links and that are no source themselves."""
    owners = member_owners(root)
    return [p for p in paths if not owners.get(p) and not manifest.is_live(p)]


def _merged_flags(*groups: list[str]) -> list[str]:
    out: list[str] = []
    for group in groups:
        out += [f for f in group if f not in out]
    return out


def _entry(
    root: Path, rel: str, sha: str, old: SourceEntry | None, today: date
) -> SourceEntry:
    """A fresh manifest entry for ``rel``, keeping what an older one recorded."""
    path = root / rel
    return SourceEntry(
        path=rel,
        sha256=sha,
        size=path.stat().st_size,
        mime=mime_for(path),
        category=category_for(rel),
        first_seen=old.first_seen if old else today,
        last_ingested=old.last_ingested if old else None,
        pages_touched=list(old.pages_touched) if old else [],
        status="new",
        flags=[f for f in (old.flags if old else []) if f in _KEPT_FLAGS],
        src_page=old.src_page if old else None,
    )


def _last_ingested_sha(old: SourceEntry | None) -> str | None:
    """The sha whose pages the wiki holds for this path, or None if it holds none.

    An entry that is not ``ingested`` (new, changed, quarantined, failed) keeps
    that sha in ``supersedes_sha``; an older failed entry without one was
    ingested at its own sha when ``last_ingested`` is set.
    """
    if old is None or old.status == "deleted":
        return None
    if old.status == "ingested":
        return old.sha256
    return old.supersedes_sha or (old.sha256 if old.last_ingested else None)


def _kind(old: SourceEntry | None) -> tuple[Kind, str | None]:
    """("new" | "changed", the superseded sha): changed only against pages the
    wiki already holds for this path.
    """
    last = _last_ingested_sha(old)
    return ("changed", last) if last else ("new", None)


def _screen_outputs(
    root: Path, outputs: list[str]
) -> tuple[Counter[str], list[dict], list[str]]:
    """Redact every text output in place; return counts, injection hits, leaks."""
    counts: Counter[str] = Counter()
    hits: list[dict] = []
    leaks: list[str] = []
    for rel in outputs:
        path = root / rel
        text = path.read_text(encoding="utf-8")
        res = screen_text(text, tmp_dir=root / TMP_DIR)
        if res.text != text:
            path.write_text(res.text, encoding="utf-8")
        counts += res.redactions
        hits += [_hit_dict(rel, h) for h in res.injection]
        leaks += [r for r in res.gitleaks if r not in leaks]
    return counts, hits, leaks


def _hit_dict(rel: str, h: Hit) -> dict:
    return {"file": rel, "kind": h.kind, "line": h.line, "excerpt": h.excerpt}


def _parts(conv: Conversion) -> list[str]:
    """The converted files to plan in turn: the pN parts, else the primary."""
    if conv.parts > 1:
        return conv.outputs[1 : 1 + conv.parts]
    return conv.outputs[:1]


def _quarantine_reason(
    trusted: bool,
    strict: bool,
    counts: Counter[str],
    hits: list[dict],
    leaks: list[str],
) -> tuple[str, str]:
    """(kind, why) the screened source must be quarantined; ("", "") to proceed.

    Trust releases only the injection check: secrets quarantine regardless.
    """
    if hits and not trusted:
        return "injection", "injection: " + ", ".join(sorted({h["kind"] for h in hits}))
    # gitleaks ran on the redacted text, so a finding is a secret still in it
    found = [r for r in leaks if r != "gitleaks-error"]
    if found:
        return "secrets", "secret left after redaction (gitleaks): " + ", ".join(found)
    if strict and counts:
        return "secrets", "secret redacted (strict_secrets): " + ", ".join(
            sorted(counts)
        )
    return "", ""


@dataclass
class _Triaged:
    sent: list[Member] = field(default_factory=list)
    same: dict[str, Member] = field(default_factory=dict)  # link -> the one sent
    dropped: list[str] = field(default_factory=list)


def _triage_members(root: Path, members: list[Member]) -> _Triaged:
    """Which members go to the model: not tiny or unreadable ones, and each
    near-duplicate once.
    """
    out = _Triaged()
    hashes: dict[str, str] = {}
    by_link = {m.link: m for m in members}
    for member in members:
        try:
            info = triage(root / member.path)
        except RefusedSource:
            out.dropped.append(f"unreadable: {member.name}")
            continue
        if info.reason:
            out.dropped.append(f"{info.reason}: {member.name}")
        elif first := near_duplicate(info.phash, hashes):
            out.same[member.link] = by_link[first]
        else:
            hashes[member.link] = info.phash
            out.sent.append(member)
    return out


def _splitter(triaged: _Triaged, max_images: int) -> Splitter | None:
    """How to cut a page with more images to send than one plan call takes."""
    if len(triaged.sent) <= max_images:
        return None
    sent = {m.link for m in triaged.sent}
    return lambda body, limit: split_parts(body, sent, max_images, limit)


def _attach_images(root: Path, prepared: Prepared, triaged: _Triaged) -> None:
    """Give each part the images it links first, in link order."""
    to_send = {m.link: m for m in triaged.sent}
    attached: set[str] = set()
    for part in prepared.parts:
        links, _ = local_links((root / part).read_text(encoding="utf-8"))
        images = [to_send[ln.path] for ln in links if ln.path in to_send]
        images = [m for m in images if m.link not in attached]
        attached |= {m.link for m in images}
        same = [
            (next(m.name for m in prepared.members if m.link == ln.path), first.name)
            for ln in links
            if (first := triaged.same.get(ln.path))
        ]
        if images:
            prepared.images[part] = images
        if same:
            prepared.same_images[part] = same
    # a link a part cut made unreadable (say, inside a split code fence)
    rest = [m for m in triaged.sent if m.link not in attached]
    if rest:
        prepared.images.setdefault(prepared.parts[-1], []).extend(rest)


def _changed_images(old: SourceEntry | None, current: list[BundleMember]) -> list[str]:
    """The names of the members new or different since the last ingest."""
    if old is None or old.members is None:
        return []
    known = {m.path: m.sha256 for m in old.members}
    return [Path(m.path).name for m in current if known.get(m.path) != m.sha256]


def prepare_source(
    root: Path, cfg: WikiConfig, manifest: Manifest, rel: str, today: date
) -> Prepared:
    """Convert and screen raw source ``rel`` and record it in ``manifest``.

    A refused or unsupported source is recorded as ``failed`` and returned with
    ``status="refused"``: the run goes on with the next source.
    """
    root = Path(root)
    raw_path = root / rel
    sha = sha256_file(raw_path)
    old = manifest.entries.get(rel)
    kind, supersedes = _kind(old)
    entry = _entry(root, rel, sha, old, today).model_copy(
        update={
            "status": kind,
            "supersedes_sha": supersedes,
            "src_page": _assign_src_page(cfg, manifest, rel),
        }
    )
    prepared = Prepared(rel=rel, sha=sha, kind=kind)
    caps = cfg.effective_caps()
    prepared.members, ignored = page_members(root, rel)
    triaged = _triage_members(root, prepared.members)
    prepared.ignored_images = [redact(i)[0] for i in [*ignored, *triaged.dropped]]
    try:
        split = _splitter(triaged, caps.max_images_per_part)
        conv = convert(root, raw_path, sha, caps, split)
    except (RefusedSource, UnsupportedFormat) as exc:
        manifest.record(entry.model_copy(update={"status": "failed"}))
        prepared.status, prepared.reason = "refused", str(exc)
        return prepared
    counts, hits, leaks = _screen_outputs(root, conv.outputs)
    trusted = "trusted" in entry.flags
    why, reason = _quarantine_reason(trusted, cfg.strict_secrets, counts, hits, leaks)
    prepared.redactions = dict(counts)
    if reason:
        paths = [m.path for m in prepared.members]
        prepared.held = quarantine(
            root,
            raw_path,
            {
                "source": rel,
                "sha256": sha,
                "kind": why,
                "reason": reason,
                "hits": hits,
                "members": paths,
            },
        )
        hold_members(root, manifest, prepared.held, paths)
        for out in conv.outputs:
            (root / out).unlink(missing_ok=True)
        manifest.record(
            entry.model_copy(
                update={"status": "quarantined", "redactions": dict(counts)}
            )
        )
        prepared.status, prepared.reason = "quarantined", reason
        return prepared
    members = None
    if raw_path.suffix.lower() == PAGE_SUFFIX:
        members = [
            BundleMember(path=m.path, sha256=sha256_file(root / m.path))
            for m in prepared.members
        ]
        current = {m.path for m in members}
        stale = [m.path for m in (old.members if old else None) or []]
        stale = [p for p in stale if p not in current]
        prepared.pruned = removable_members(root, manifest, stale)
        for path in prepared.pruned:
            (root / path).unlink(missing_ok=True)
        prepared.changed_images = _changed_images(old, members)
    manifest.record(
        entry.model_copy(
            update={
                "members": members,
                "converter": conv.converter,
                "converted": conv.outputs,
                "flags": _merged_flags(entry.flags, conv.flags),
                "redactions": dict(counts),
            }
        )
    )
    prepared.conversion = conv
    prepared.parts = _parts(conv)
    _attach_images(root, prepared, triaged)
    if prepared.is_image:
        name = Path(rel).name
        prepared.images[prepared.parts[0]] = [Member("", rel, name)]
    return prepared
