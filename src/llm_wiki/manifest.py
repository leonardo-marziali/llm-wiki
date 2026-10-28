"""Hash-based registry of raw sources, used by ingest, lint and status."""

import hashlib
import json
import mimetypes
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from llm_wiki.errors import ConfigError

MANIFEST = ".wiki/manifest.json"
_CHUNK = 1024 * 1024

SourceStatus = Literal["new", "changed", "ingested", "deleted", "quarantined", "failed"]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def mime_for(p: Path) -> str:
    return mimetypes.guess_type(p.name)[0] or "application/octet-stream"


def category_for(rel: str) -> str:
    """First directory under raw/, or "misc" for files directly in raw/."""
    parts = rel.split("/")
    return parts[1] if len(parts) > 2 else "misc"


class SourceEntry(BaseModel):
    path: str
    sha256: str
    size: int
    mime: str
    category: str
    first_seen: date
    last_ingested: date | None = None
    converter: str | None = None
    converted: list[str] = []
    pages_touched: list[str] = []
    status: SourceStatus
    supersedes_sha: str | None = None
    flags: list[str] = []
    redactions: dict[str, int] = {}
    src_page: str | None = None  # the id of its src- page, fixed once assigned


@dataclass
class ScanResult:
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


class Manifest:
    def __init__(self, entries: dict[str, SourceEntry] | None = None) -> None:
        self.entries: dict[str, SourceEntry] = entries or {}

    @classmethod
    def load(cls, root: Path) -> "Manifest":
        f = Path(root) / MANIFEST
        if not f.is_file():
            return cls()
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ConfigError(f"{MANIFEST} is corrupt: top level must be an object")
            if data.get("version") != 1:
                raise ConfigError(
                    f"{MANIFEST}: unsupported version {data.get('version')!r}"
                )
            return cls(
                {
                    k: SourceEntry.model_validate(v)
                    for k, v in data.get("sources", {}).items()
                }
            )
        except ConfigError:
            raise
        except (ValueError, ValidationError, AttributeError, OSError) as e:
            raise ConfigError(f"{MANIFEST} is corrupt: {e}") from None

    def scan(self, root: Path) -> ScanResult:
        root = Path(root)
        res = ScanResult()
        seen: set[str] = set()
        raw = root / "raw"
        files = sorted(p for p in raw.rglob("*") if p.is_file()) if raw.is_dir() else []
        for p in files:
            rel_parts = p.relative_to(raw).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            rel = p.relative_to(root).as_posix()
            seen.add(rel)
            entry = self.entries.get(rel)
            if entry is None or entry.status == "deleted":
                res.new.append(rel)
            elif entry.sha256 != sha256_file(p):
                res.changed.append(rel)
            else:
                res.unchanged.append(rel)
        for rel, entry in self.entries.items():
            # quarantine moves the file out of raw/, so it is not a deletion
            if rel not in seen and entry.status not in ("deleted", "quarantined"):
                res.deleted.append(rel)
        res.new.sort()
        res.changed.sort()
        res.unchanged.sort()
        res.deleted.sort()
        return res

    def record(self, entry: SourceEntry) -> None:
        self.entries[entry.path] = entry

    def mark_deleted(self, path: str) -> None:
        if path not in self.entries:
            raise ConfigError(f"{MANIFEST}: no entry for {path}")
        self.entries[path] = self.entries[path].model_copy(update={"status": "deleted"})

    def live(self) -> int:
        """Sources counted against ``max_sources``: not deleted or quarantined."""
        return sum(
            e.status not in ("deleted", "quarantined") for e in self.entries.values()
        )

    def is_live(self, path: str) -> bool:
        entry = self.entries.get(path)
        return entry is not None and entry.status not in ("deleted", "quarantined")

    def pages_for(self, path: str) -> list[str]:
        entry = self.entries.get(path)
        return list(entry.pages_touched) if entry else []

    def save(self, root: Path) -> None:
        f = Path(root) / MANIFEST
        f.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "sources": {k: e.model_dump(mode="json") for k, e in self.entries.items()},
        }
        f.write_text(
            json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
