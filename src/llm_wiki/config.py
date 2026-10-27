"""wiki.toml model, profiles and deterministic serialisation."""

import json
import math
import os
import tomllib
from dataclasses import fields, replace
from importlib import resources
from pathlib import Path
from typing import Literal

import platformdirs
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from llm_wiki.caps import HARD_CAPS, Caps
from llm_wiki.errors import ConfigError

WIKI_FILE = "wiki.toml"
WARN_RATIO = 0.8

SectionKind = Literal["as-is", "to-be", "decisions", "sources"]
Profile = Literal["software", "general"]
Role = Literal["plan", "edit", "deep"]
USER_CONFIG_FILE = "config.toml"
_BUDGETS = ("plan_budget_usd", "edit_budget_usd", "deep_budget_usd")


def _positive(v: float | None) -> float | None:
    if v is not None and not (math.isfinite(v) and v > 0):
        raise ValueError("budget must be a finite number greater than 0")
    return v


class LlmSettings(BaseModel):
    """Effective per-role model ids and USD budgets for `claude -p` calls."""

    model_config = ConfigDict(extra="forbid")

    plan_model: str = "claude-sonnet-5-5"
    edit_model: str = "claude-sonnet-5-5"
    deep_model: str = "claude-opus-5-5"
    plan_budget_usd: float = 1.0
    edit_budget_usd: float = 2.0
    deep_budget_usd: float = 3.0

    _check = field_validator(*_BUDGETS)(_positive)

    def model(self, role: Role) -> str:
        return getattr(self, f"{role}_model")

    def budget(self, role: Role) -> float:
        return getattr(self, f"{role}_budget_usd")


class LlmOverrides(BaseModel):
    """The [llm] table of wiki.toml or config.toml: every field optional, None means inherit."""

    model_config = ConfigDict(extra="forbid")

    plan_model: str | None = None
    edit_model: str | None = None
    deep_model: str | None = None
    plan_budget_usd: float | None = None
    edit_budget_usd: float | None = None
    deep_budget_usd: float | None = None

    _check = field_validator(*_BUDGETS)(_positive)


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: SectionKind
    description: str


class WikiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    profile: Profile
    sections: list[Section]
    caps: dict[str, int] = {}
    strict_secrets: bool = False
    llm: LlmOverrides = LlmOverrides()

    def effective_caps(self) -> Caps:
        known = {f.name for f in fields(Caps)}
        for key, value in self.caps.items():
            if key not in known:
                raise ConfigError(f"unknown cap '{key}' in [caps]")
            hard = getattr(HARD_CAPS, key)
            if value < 1:
                raise ConfigError(f"cap {key}={value} must be at least 1")
            if value > hard:
                raise ConfigError(f"cap {key}={value} exceeds hard maximum {hard}")
        overrides = dict(self.caps)
        for key, value in self.caps.items():
            warn = "warn_" + key.removeprefix("max_")
            if key.startswith("max_") and warn in known and warn not in self.caps:
                overrides[warn] = max(1, int(value * WARN_RATIO))
        caps = replace(HARD_CAPS, **overrides)
        for key in (f.name for f in fields(Caps)):  # field order: deterministic message
            max_key = "max_" + key.removeprefix("warn_")
            if key.startswith("warn_") and max_key in known:
                warn_v, max_v = getattr(caps, key), getattr(caps, max_key)
                if warn_v > max_v:
                    raise ConfigError(f"cap {key}={warn_v} exceeds {max_key}={max_v}")
        return caps


def load_profile(name: str) -> list[Section]:
    try:
        text = (
            resources.files("llm_wiki") / "templates" / "profiles" / f"{name}.toml"
        ).read_text("utf-8")
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


def home_dir() -> Path:
    home = os.environ.get("LLM_WIKI_HOME")
    return Path(home) if home else Path(platformdirs.user_config_dir("llm-wiki"))


def user_config_file() -> Path:
    return home_dir() / USER_CONFIG_FILE


def _user_overrides() -> LlmOverrides:
    path = user_config_file()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LlmOverrides()
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid {path}: {e}") from None
    try:
        return LlmOverrides.model_validate(data.get("llm", {}))
    except ValidationError as e:
        raise ConfigError(f"invalid [llm] in {path}: {e}") from None


def load_llm_settings(wiki_root: Path | None) -> LlmSettings:
    """Defaults, then the user config.toml [llm], then the wiki's wiki.toml [llm]."""
    merged: dict = {}
    layers = [_user_overrides()]
    if wiki_root is not None:
        layers.append(load_config(wiki_root).llm)
    for layer in layers:
        merged.update(layer.model_dump(exclude_none=True))
    return LlmSettings(**merged)


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
    llm = cfg.llm.model_dump(exclude_none=True)
    if llm:
        lines += ["", "[llm]"] + [
            f"{k} = {repr(v) if k in _BUDGETS else _s(v)}" for k, v in llm.items()
        ]
    for s in cfg.sections:
        lines += [
            "",
            "[[sections]]",
            f"name = {_s(s.name)}",
            f"kind = {_s(s.kind)}",
            f"description = {_s(s.description)}",
        ]
    return "\n".join(lines) + "\n"
