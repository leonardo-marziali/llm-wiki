"""Page model and frontmatter contract shared by index, search, lint and init."""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

PageType = Literal[
    "concept",
    "component",
    "system",
    "integration",
    "environment",
    "process",
    "convention",
    "decision",
    "requirement",
    "howto",
    "reference",
    "glossary",
    "source-summary",
    "overview",
    "hub",
]
Status = Literal["as-is", "to-be", "deprecated", "superseded"]
AdrStatus = Literal["proposed", "accepted", "rejected", "superseded"]
Confidence = Literal["stated", "high", "medium", "low"]
ReviewStatus = Literal["draft", "reviewed"]

SOURCELESS_TYPES = {"overview", "glossary", "hub"}
SYSTEM_FILES = {"index.md", "log.md", "open-questions.md", "_index.md"}
ID_RE = r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)*$"

_SHA_RE = re.compile(r"[0-9a-f]{64}")


class SourceRef(BaseModel):
    path: str
    sha256: str
    locator: str

    @field_validator("path")
    @classmethod
    def _path_in_raw(cls, v: str) -> str:
        if not v.startswith("raw/"):
            raise ValueError("source path must start with 'raw/'")
        if ".." in v.split("/"):
            raise ValueError("source path must not contain '..' segments")
        return v

    @field_validator("sha256")
    @classmethod
    def _sha_hex(cls, v: str) -> str:
        if not _SHA_RE.fullmatch(v):
            raise ValueError("sha256 must be 64 hex characters")
        return v


class Frontmatter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    type: PageType
    status: Status
    adr_status: AdrStatus | None = None
    summary: str
    sources: list[SourceRef] = []
    confidence: Confidence
    review_status: ReviewStatus
    last_verified: date
    review_after: date
    supersedes: list[str] = []
    superseded_by: str | None = None
    related: list[str] = []
    tags: list[str] = []

    @model_validator(mode="after")
    def _review_after_follows(self) -> "Frontmatter":
        if self.review_after <= self.last_verified:
            raise ValueError("review_after must be later than last_verified")
        return self


@dataclass
class Page:
    path: Path
    rel: str
    fm: Frontmatter | None
    body: str
    error: str | None
    line_count: int


def split_frontmatter(text: str) -> tuple[dict | None, str]:
    """Split a leading ``---`` block from the body; raise ValueError on bad YAML."""
    lines = text.split("\n")
    if lines[0].rstrip("\r") != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") == "---":
            raw = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            try:
                data = yaml.safe_load(raw)
            except yaml.YAMLError as exc:
                raise ValueError(f"malformed frontmatter YAML: {exc}") from exc
            if data is None:
                data = {}
            if not isinstance(data, dict):
                raise ValueError("frontmatter must be a YAML mapping")
            return data, body
    return None, text


def id_for(rel: str) -> str:
    """Derive the page id from a POSIX path relative to wiki/."""
    return rel.removesuffix(".md").replace("/", ".")


def _format_errors(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "frontmatter"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def read_page(root: Path, path: Path) -> Page:
    rel = path.relative_to(root / "wiki").as_posix()
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return Page(
            path=path, rel=rel, fm=None, body="", error="not valid UTF-8", line_count=0
        )
    except OSError as exc:
        return Page(
            path=path,
            rel=rel,
            fm=None,
            body="",
            error=f"unreadable: {exc}",
            line_count=0,
        )
    line_count = len(text.splitlines())
    fm: Frontmatter | None = None
    error: str | None = None
    body = text
    try:
        data, body = split_frontmatter(text)
        if data is None:
            error = "missing frontmatter"
        else:
            fm = Frontmatter.model_validate(data)
    except ValueError as exc:
        # ValidationError is a ValueError subclass.
        error = _format_errors(exc) if isinstance(exc, ValidationError) else str(exc)
    return Page(
        path=path, rel=rel, fm=fm, body=body, error=error, line_count=line_count
    )


def iter_pages(root: Path) -> Iterator[Page]:
    wiki = root / "wiki"
    paths = [
        p
        for p in wiki.rglob("*.md")
        if p.is_file()
        and p.name not in SYSTEM_FILES
        and "_archive" not in p.relative_to(wiki).parts[:-1]
    ]
    for p in sorted(paths, key=lambda p: p.relative_to(wiki).as_posix()):
        yield read_page(root, p)


def render_frontmatter(fm: Frontmatter) -> str:
    data = fm.model_dump(mode="json")
    body = yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return f"---\n{body}---\n"
