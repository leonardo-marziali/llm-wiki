"""wiki.toml model, profiles and deterministic serialisation."""

import json
import tomllib
from dataclasses import fields, replace
from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from llm_wiki.caps import HARD_CAPS, Caps
from llm_wiki.errors import ConfigError

WIKI_FILE = "wiki.toml"
WARN_RATIO = 0.8

SectionKind = Literal["as-is", "to-be", "decisions", "sources"]


class Section(BaseModel):
    name: str
    kind: SectionKind
    description: str


class WikiConfig(BaseModel):
    name: str
    description: str
    profile: Literal["software", "general"]
    sections: list[Section]
    caps: dict[str, int] = {}
    strict_secrets: bool = False

    def effective_caps(self) -> Caps:
        known = {f.name for f in fields(Caps)}
        for key, value in self.caps.items():
            if key not in known:
                raise ConfigError(f"unknown cap '{key}' in [caps]")
            hard = getattr(HARD_CAPS, key)
            if value > hard:
                raise ConfigError(f"cap {key} = {value} exceeds the hard limit {hard}; caps can only be lowered")
        overrides = dict(self.caps)
        for key, value in self.caps.items():
            warn = "warn_" + key.removeprefix("max_")
            if key.startswith("max_") and warn in known and warn not in self.caps:
                overrides[warn] = int(value * WARN_RATIO)
        return replace(HARD_CAPS, **overrides)


def load_profile(name: str) -> list[Section]:
    try:
        text = (resources.files("llm_wiki") / "templates" / "profiles" / f"{name}.toml").read_text("utf-8")
    except (FileNotFoundError, NotADirectoryError):
        raise ConfigError(f"unknown profile '{name}'") from None
    return [Section(**s) for s in tomllib.loads(text)["sections"]]


def load_config(root: Path) -> WikiConfig:
    path = Path(root) / WIKI_FILE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{WIKI_FILE} not found in {root}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid {WIKI_FILE} in {root}: {e}") from None
    try:
        return WikiConfig(**data)
    except ValidationError as e:
        raise ConfigError(f"invalid {WIKI_FILE} in {root}: {e}") from None


def _s(value: str) -> str:
    # JSON string escapes are a valid subset of TOML basic-string escapes.
    return json.dumps(value, ensure_ascii=False)


def dump_config(cfg: WikiConfig) -> str:
    lines = [
        f"name = {_s(cfg.name)}",
        f"description = {_s(cfg.description)}",
        f"profile = {_s(cfg.profile)}",
        f"strict_secrets = {'true' if cfg.strict_secrets else 'false'}",
    ]
    if cfg.caps:
        lines += ["", "[caps]"] + [f"{k} = {v}" for k, v in cfg.caps.items()]
    for s in cfg.sections:
        lines += [
            "",
            "[[sections]]",
            f"name = {_s(s.name)}",
            f"kind = {_s(s.kind)}",
            f"description = {_s(s.description)}",
        ]
    return "\n".join(lines) + "\n"
