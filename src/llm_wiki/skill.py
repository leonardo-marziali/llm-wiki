"""`wiki skill install`: the read-only wiki-lookup skill and the CLAUDE.md pointer."""

import os
from importlib import resources
from pathlib import Path

from llm_wiki import __version__
from llm_wiki.errors import WikiError

SKILL_NAME = "wiki-lookup"
STAMP = "llm-wiki-version:"
POINTER_BEGIN = "<!-- llm-wiki pointer: begin -->"
POINTER_END = "<!-- llm-wiki pointer: end -->"


def _template(name: str) -> str:
    return (resources.files("llm_wiki") / "templates" / name).read_text("utf-8")


def user_skills_dir() -> Path:
    """User-level skills dir: $CLAUDE_CONFIG_DIR/skills, else ~/.claude/skills."""
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config).expanduser() if config else Path.home() / ".claude"
    return base / "skills"


def render_skill() -> str:
    return _template("skill-wiki-lookup.md").replace("{version}", __version__)


def render_pointer(wiki_name: str) -> str:
    return _template("claude-pointer.md").replace("{wiki}", wiki_name)


def install_skill(target: Path, *, force: bool = False) -> tuple[Path, bool]:
    """Write <target>/wiki-lookup/SKILL.md; True when the file changed.

    A SKILL.md without our version stamp was written by someone else and is
    only replaced with ``force``.
    """
    path = Path(target) / SKILL_NAME / "SKILL.md"
    text = render_skill()
    if path.is_file():
        current = path.read_text(encoding="utf-8")
        if current == text:
            return path, False
        if STAMP not in current and not force:
            raise WikiError(
                f"{path} was not written by llm-wiki; pass --force to replace it"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, True


def write_pointer(project: Path, wiki_name: str) -> tuple[Path, bool]:
    """Put the pointer block in <project>/CLAUDE.md, replacing only an existing
    marked block; True when the file changed.
    """
    path = Path(project) / "CLAUDE.md"
    block = render_pointer(wiki_name)
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    start = current.find(POINTER_BEGIN)
    end = current.find(POINTER_END, start)
    if start != -1 and end != -1:
        tail = current[end + len(POINTER_END) :]
        new = current[:start] + block.rstrip("\n") + tail
    elif current:
        new = current.rstrip("\n") + "\n\n" + block
    else:
        new = block
    if new == current:
        return path, False
    path.write_text(new, encoding="utf-8")
    return path, True
