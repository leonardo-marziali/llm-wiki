"""Wiki registry and resolution of which wiki a command operates on."""

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import platformdirs

from llm_wiki.config import WIKI_FILE, load_config
from llm_wiki.errors import ConfigError, WikiNotFound

NOT_FOUND = "no wiki found: pass --wiki NAME|PATH or run `wiki init <path>`"


@dataclass(frozen=True)
class WikiRef:
    name: str
    path: Path
    origin: Literal["registry", "env", "path"]


def registry_file() -> Path:
    home = os.environ.get("LLM_WIKI_HOME")
    base = Path(home) if home else Path(platformdirs.user_config_dir("llm-wiki"))
    return base / "wikis.toml"


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _read() -> tuple[str | None, dict[str, str]]:
    f = registry_file()
    if not f.exists():
        return None, {}
    try:
        data = tomllib.loads(f.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"invalid registry file {f}: {e}") from e
    return data.get("default"), dict(data.get("wikis", {}))


def _write(default: str | None, wikis: dict[str, str]) -> None:
    lines = []
    if default is not None:
        lines.append(f"default = {_q(default)}")
        lines.append("")
    lines.append("[wikis]")
    for name in sorted(wikis):
        lines.append(f"{_q(name)} = {_q(wikis[name])}")
    f = registry_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")


def register(name: str, path: Path, make_default: bool = False) -> None:
    default, wikis = _read()
    wikis[name] = str(Path(path).resolve())
    if make_default or default is None:
        default = name
    _write(default, wikis)


def registered_path(name: str) -> Path | None:
    """The path ``name`` is registered at, or None."""
    _, wikis = _read()
    return Path(wikis[name]) if name in wikis else None


def known_wikis() -> list[WikiRef]:
    _, wikis = _read()
    refs = [WikiRef(n, Path(p), "registry") for n, p in wikis.items()]
    seen = {r.path for r in refs}
    for entry in os.environ.get("LLM_WIKI_PATH", "").split(os.pathsep):
        if not entry:
            continue
        p = Path(entry).resolve()
        if p in seen or not (p / WIKI_FILE).is_file():
            continue
        seen.add(p)
        try:
            name = load_config(p).name
        except ConfigError:
            continue
        refs.append(WikiRef(name, p, "env"))
    return refs


def find_root(start: Path) -> Path | None:
    start = Path(start).resolve()
    for d in (start, *start.parents):
        if (d / WIKI_FILE).is_file():
            return d
    return None


def _ref_for_path(root: Path) -> WikiRef:
    root = root.resolve()
    for ref in known_wikis():
        if ref.path == root:
            return ref
    return WikiRef(load_config(root).name, root, "path")


def resolve(arg: str | None, cwd: Path) -> WikiRef:
    refs = known_wikis()
    if arg is not None:
        for ref in refs:
            if ref.name == arg:
                return ref
        candidate = (Path(cwd) / arg).resolve()
        if (candidate / WIKI_FILE).is_file():
            return _ref_for_path(candidate)
    else:
        root = find_root(cwd)
        if root is not None:
            return _ref_for_path(root)
        default, wikis = _read()
        if default in wikis and (Path(wikis[default]) / WIKI_FILE).is_file():
            return WikiRef(default, Path(wikis[default]), "registry")
    raise WikiNotFound(NOT_FOUND)
