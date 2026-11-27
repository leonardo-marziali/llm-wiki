"""`wiki describe`, `wiki rename` and `wiki move`: change a wiki's description, name
or path.

Rename and describe also rewrite the text `init` wrote from those values, but only
where it is still verbatim; anything edited since is left alone with a warning.
"""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from llm_wiki import gitops
from llm_wiki.config import WIKI_FILE, WikiConfig, dump_config, load_config
from llm_wiki.errors import WikiError
from llm_wiki.gate import LOG, append_log
from llm_wiki.index import GENERATED_MARK
from llm_wiki.registry import WikiRef

AGENTS = "AGENTS.md"
OVERVIEW = "wiki/overview.md"
GLOSSARY = "wiki/glossary.md"
INDEX = "wiki/index.md"


@dataclass
class ChangeResult:
    ref: WikiRef  # the wiki after the change
    # one per site left alone: "<rel>: <what> was edited; left as is"
    warnings: list[str] = field(default_factory=list)
    committed: bool = False  # False in a host repo
    host_repo: Path | None = None  # the enclosing repo when not committed


def _shown(cfg: WikiConfig) -> str:
    """The description as init rendered it: the fallback when it is empty."""
    return cfg.description or f"The {cfg.name} wiki."


def _index_header(cfg: WikiConfig) -> str:
    return f"# {cfg.name}\n\n{cfg.description}\n\n"


def _require_clean(root: Path) -> None:
    if gitops.changed_paths(root):
        raise WikiError(f"{root} has uncommitted changes: commit or discard them first")


def _replace_line(
    root: Path, rel: str, old: str, new: str, what: str, warnings: list[str]
) -> bool:
    """Replace the one line of ``rel`` equal to ``old``; True when it wrote the file.

    A missing file, or ``old`` on no line or on several, gets a warning instead.
    """
    if old == new:
        return False
    path = root / rel
    if not path.is_file():
        warnings.append(f"{rel}: missing; {what} not updated")
        return False
    lines = path.read_text(encoding="utf-8").split("\n")
    hits = [i for i, line in enumerate(lines) if line == old]
    if len(hits) != 1:
        warnings.append(f"{rel}: {what} was edited; left as is")
        return False
    lines[hits[0]] = new
    path.write_text("\n".join(lines), encoding="utf-8")
    return True


def _replace_index_header(root: Path, old: str, new: str, warnings: list[str]) -> bool:
    if old == new:
        return False
    path = root / INDEX
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    head, mark, tail = text.partition(GENERATED_MARK)
    if not mark or head != old:
        warnings.append(f"{INDEX}: header was edited; left as is")
        return False
    path.write_text(new + mark + tail, encoding="utf-8")
    return True


def _rewrite_description(
    root: Path, old: WikiConfig, new: WikiConfig, warnings: list[str]
) -> list[str]:
    """The description sites; returns the files written."""
    touched = []
    if _replace_line(root, AGENTS, _shown(old), _shown(new), "description", warnings):
        touched.append(AGENTS)
    if _replace_line(
        root,
        OVERVIEW,
        f"**TL;DR** {_shown(old)}",
        f"**TL;DR** {_shown(new)}",
        "TL;DR",
        warnings,
    ):
        touched.append(OVERVIEW)
    if _replace_index_header(root, _index_header(old), _index_header(new), warnings):
        touched.append(INDEX)
    return touched


def _save_config(root: Path, cfg: WikiConfig) -> None:
    (root / WIKI_FILE).write_text(dump_config(cfg), encoding="utf-8")


def _finish(
    root: Path,
    ref: WikiRef,
    touched: list[str],
    log_line: str,
    message: str,
    warnings: list[str],
) -> ChangeResult:
    append_log(root, [log_line])
    host = gitops.enclosing_repo(root)
    if host != root.resolve():
        return ChangeResult(ref, warnings, committed=False, host_repo=host)
    paths = list(dict.fromkeys([WIKI_FILE, *touched, LOG]))
    gitops.stage_commit(root, paths, message)
    return ChangeResult(ref, warnings, committed=True)


def describe_wiki(ref: WikiRef, text: str, today: date | None = None) -> ChangeResult:
    """Set the wiki's one-line description."""
    root = ref.path
    text = " ".join(text.split())
    if not text:
        raise WikiError("the description must not be empty")
    old = load_config(root)
    if text == old.description:
        raise WikiError(f"wiki '{old.name}' already has this description")
    _require_clean(root)
    new = old.model_copy(update={"description": text})
    _save_config(root, new)
    warnings: list[str] = []
    touched = _rewrite_description(root, old, new, warnings)
    day = (today or date.today()).isoformat()
    return _finish(
        root,
        ref,
        touched,
        f"## [{day}] describe | {text}",
        f"chore: describe wiki {new.name}",
        warnings,
    )
