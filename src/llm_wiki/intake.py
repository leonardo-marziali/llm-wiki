"""Sources into raw/, then converted and screened: one ``Prepared`` per source.

Nothing here calls an LLM. A source whose text holds injection hits (or, with
``strict_secrets``, any secret) is quarantined before a plan call can see it.
"""

import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from llm_wiki.config import WikiConfig
from llm_wiki.convert import (
    IMAGE_SUFFIXES,
    NEVER_INGEST_SUFFIXES,
    Conversion,
    convert,
)
from llm_wiki.errors import RefusedSource, UnsupportedFormat, WikiError
from llm_wiki.init import slug
from llm_wiki.manifest import (
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
_NOTE_WORDS = 6
_KEPT_FLAGS = ("trusted",)  # conversion flags are recomputed on every conversion

Kind = Literal["new", "changed", "deleted"]


@dataclass
class Prepared:
    rel: str  # the raw path
    sha: str
    kind: Kind
    origin: Path | None = None  # the inbox or note file to restore on reject
    conversion: Conversion | None = None
    parts: list[str] = field(default_factory=list)  # converted paths to plan in turn
    image: Path | None = None  # the raw image, sent to the plan call
    redactions: dict[str, int] = field(default_factory=dict)
    status: Literal["ready", "quarantined", "refused"] = "ready"
    reason: str = ""
    held: Path | None = None  # where quarantine put the raw file


def source_slug(rel: str) -> str:
    """``raw/docs/My Guide.md`` -> ``docs-my-guide``."""
    stem = Path(rel).relative_to("raw").with_suffix("").as_posix()
    return slug(stem.replace("/", "-"))


def src_page_id(cfg: WikiConfig, rel: str) -> str:
    """The id of the tool-written summary page of raw source ``rel``."""
    section = next((s.name for s in cfg.sections if s.kind == "sources"), "sources")
    return f"{section}.src-{source_slug(rel)}"


def _file_name(name: str) -> str:
    """``My Diagram.PNG`` -> ``my-diagram.png``; a credential suffix stays whole."""
    lower = name.lower()
    suffix = Path(lower).suffix
    if not re.fullmatch(r"\.[a-z0-9]+", suffix):
        suffix = ""  # "notes.final draft" has no extension
    suffix = next((s for s in NEVER_INGEST_SUFFIXES if lower.endswith(s)), suffix)
    stem = name[: len(name) - len(suffix)] if suffix else name
    return (slug(stem) or "file") + suffix.lower()


def _raw_rel(parts: tuple[str, ...]) -> str:
    """The raw path for a file at ``parts`` (folders, then the file name)."""
    dirs = [slug(d) or "misc" for d in parts[:-1]]
    return "/".join(["raw", *dirs, _file_name(parts[-1])])


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


def move_into_raw(root: Path, src: Path, rel: str) -> None:
    dest = Path(root) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), dest)


def copy_paths(root: Path, paths: list[Path]) -> list[str]:
    """Copy files, or folders recursively, into raw/; a folder keeps its name."""
    root = Path(root)
    raw = (root / "raw").resolve()
    out: list[str] = []
    claimed: set[str] = set()
    for path in map(Path, paths):
        if not path.exists():
            raise WikiError(f"no such file or folder: {path}")
        if path.resolve().is_relative_to(raw):  # already raw sources
            files = _visible_files(path) if path.is_dir() else [path]
            out += [f.resolve().relative_to(raw.parent).as_posix() for f in files]
            continue
        if path.is_dir():
            pairs = [
                (f, (path.name, *f.relative_to(path).parts))
                for f in _visible_files(path)
            ]
        else:
            pairs = [(path, (path.name,))]
        for src, parts in pairs:
            rel = _unclaimed(root, _raw_rel(parts), claimed)
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            out.append(rel)
    return out


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
        update={"status": kind, "supersedes_sha": supersedes}
    )
    prepared = Prepared(rel=rel, sha=sha, kind=kind)
    try:
        conv = convert(root, raw_path, sha, cfg.effective_caps())
    except (RefusedSource, UnsupportedFormat) as exc:
        manifest.record(entry.model_copy(update={"status": "failed"}))
        prepared.status, prepared.reason = "refused", str(exc)
        return prepared
    counts, hits, leaks = _screen_outputs(root, conv.outputs)
    trusted = "trusted" in entry.flags
    why, reason = _quarantine_reason(trusted, cfg.strict_secrets, counts, hits, leaks)
    prepared.redactions = dict(counts)
    if reason:
        prepared.held = quarantine(
            root,
            raw_path,
            {"source": rel, "sha256": sha, "kind": why, "reason": reason, "hits": hits},
        )
        for out in conv.outputs:
            (root / out).unlink(missing_ok=True)
        manifest.record(
            entry.model_copy(
                update={"status": "quarantined", "redactions": dict(counts)}
            )
        )
        prepared.status, prepared.reason = "quarantined", reason
        return prepared
    manifest.record(
        entry.model_copy(
            update={
                "converter": conv.converter,
                "converted": conv.outputs,
                "flags": _merged_flags(entry.flags, conv.flags),
                "redactions": dict(counts),
            }
        )
    )
    prepared.conversion = conv
    prepared.parts = _parts(conv)
    if raw_path.suffix.lower() in IMAGE_SUFFIXES:
        prepared.image = raw_path
    return prepared
