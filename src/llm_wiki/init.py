"""`wiki init`: create the wiki skeleton (spec §3), commit it and register it."""

import re
import shutil
from dataclasses import dataclass
from datetime import date, timedelta
from importlib import resources
from pathlib import Path
from typing import Literal

from llm_wiki import gitops
from llm_wiki.config import WIKI_FILE, Profile, Section, WikiConfig, dump_config, load_config, load_profile
from llm_wiki.errors import GitError, WikiError
from llm_wiki.index import build_indexes
from llm_wiki.lint import lint
from llm_wiki.manifest import Manifest
from llm_wiki.pages import Frontmatter, render_frontmatter
from llm_wiki.registry import WikiRef, register, registered_path

REVIEW_DAYS = 90
NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")  # one kebab-case id segment
GITKEEPS = ("inbox", "raw", ".wiki/converted", ".wiki/reports")
_KIND_RULE = {
    "as-is": "any status except `to-be`",
    "to-be": "`status: to-be` only",
    "decisions": "`type: decision` with `adr_status`",
    "sources": "`src-<slug>.md` summaries, never `to-be`",
}


@dataclass
class InitResult:
    ref: WikiRef
    created: bool


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _template(rel: str) -> str:
    return (resources.files("llm_wiki") / "templates" / rel).read_text(encoding="utf-8")


def _render(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def sections_table(sections: list[Section]) -> str:
    rows = [
        "| Section | Kind | Allowed pages | What belongs here |",
        "| ------- | ---- | ------------- | ----------------- |",
    ]
    for s in sections:
        rows.append(f"| `{s.name}/` | {s.kind} | {_KIND_RULE[s.kind]} | {_cell(s.description)} |")
    return "\n".join(rows)


def _top_page(page_id: Literal["overview", "glossary"], title: str, summary: str, body: str, today: date) -> str:
    fm = Frontmatter(
        id=page_id, title=title, type=page_id, status="as-is", summary=summary,
        confidence="high", review_status="draft",
        last_verified=today, review_after=today + timedelta(days=REVIEW_DAYS),
    )
    return render_frontmatter(fm) + body


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _create(root: Path, cfg: WikiConfig, today: date) -> None:
    values = {
        "name": cfg.name,
        "description": cfg.description or f"The {cfg.name} wiki.",
        "sections_table": sections_table(cfg.sections),
    }
    for d in GITKEEPS:
        _write(root, f"{d}/.gitkeep", "")
    for s in cfg.sections:
        (root / "wiki" / s.name).mkdir(parents=True, exist_ok=True)
    _write(root, WIKI_FILE, dump_config(cfg))
    _write(root, "AGENTS.md", _render(_template("AGENTS.md"), values))
    _write(root, "CLAUDE.md", _template("CLAUDE.md"))
    _write(root, ".gitignore", _template("gitignore"))
    _write(root, "wiki/overview.md", _top_page(
        "overview", "Overview", f"Entry point of the {cfg.name} wiki.",
        _render(_template("pages/overview.md"), values), today))
    _write(root, "wiki/glossary.md", _top_page(
        "glossary", "Glossary", f"Terms used in the {cfg.name} wiki.",
        _render(_template("pages/glossary.md"), values), today))
    _write(root, "wiki/open-questions.md", "# Open questions\n")
    _write(root, "wiki/log.md", f"# Log\n\n## [{today.isoformat()}] init | {cfg.name}\n")
    Manifest().save(root)
    build_indexes(root, cfg)


def _clean(root: Path, created_dir: bool) -> None:
    """Undo a failed init; ``root`` was missing or empty when init started."""
    if created_dir:
        shutil.rmtree(root, ignore_errors=True)
        return
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _check_name_free(name: str, root: Path) -> None:
    """Refuse a name another live wiki is registered under (a stale entry may be taken over)."""
    other = registered_path(name)
    if other is not None and other.resolve() != root and (other / WIKI_FILE).is_file():
        raise WikiError(f"wiki name '{name}' is already registered at {other}; pass --name")


def init_wiki(
    path: Path,
    name: str | None = None,
    profile: Profile = "software",
    description: str = "",
    today: date | None = None,
) -> InitResult:
    root = Path(path).resolve()
    if (root / WIKI_FILE).is_file():
        cfg = load_config(root)
        if name is not None and name != cfg.name:
            raise WikiError(f"{root} is already the wiki '{cfg.name}'; drop --name or pass --name {cfg.name}")
        _check_name_free(cfg.name, root)
        register(cfg.name, root)
        return InitResult(WikiRef(cfg.name, root, "registry"), created=False)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise WikiError(f"{root} is not empty and not a wiki")
    if name is None:
        name = slug(root.name)
        if not name:
            raise WikiError(f"cannot derive a wiki name from '{root.name}': pass --name")
    if not NAME_RE.fullmatch(name):
        raise WikiError(f"invalid wiki name '{name}': use lowercase letters, digits and single hyphens, "
                        f"e.g. '{slug(name) or 'my-wiki'}'")
    _check_name_free(name, root)
    cfg = WikiConfig(name=name, description=" ".join(description.split()), profile=profile,
                     sections=load_profile(profile))
    if not gitops.has_identity(root):
        raise GitError("configure git user.name and user.email")
    today = today or date.today()

    created_dir = not root.exists()
    root.mkdir(parents=True, exist_ok=True)
    try:
        _create(root, cfg, today)
        report = lint(root, cfg, today=today)
        if report.errors:
            details = "; ".join(f"{f.code} {f.path}: {f.message}" for f in report.findings
                                if f.severity == "error")
            raise WikiError(f"new wiki fails lint: {details}")
        gitops.init_repo(root)
        gitops.commit_all(root, f"chore: init wiki {name}")
    except BaseException:
        _clean(root, created_dir)
        raise
    register(name, root)
    return InitResult(WikiRef(name, root, "registry"), created=True)
