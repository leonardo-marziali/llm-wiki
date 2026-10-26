import json

import pytest

from llm_wiki.caps import HARD_CAPS
from llm_wiki.config import WIKI_FILE, dump_config, load_config, load_profile
from llm_wiki.errors import ConfigError


def write_toml(tmp_path, caps=None, **overrides):
    """Write a valid wiki.toml into tmp_path (with overrides) and return the directory."""
    fields = {"name": "demo", "description": "A demo wiki", "profile": "general"}
    fields.update(overrides)
    lines = [f"{k} = {json.dumps(v)}" for k, v in fields.items() if isinstance(v, str)]
    lines += [f"{k} = {str(v).lower()}" for k, v in fields.items() if isinstance(v, bool)]
    if caps:
        lines.append("[caps]")
        lines += [f"{k} = {v}" for k, v in caps.items()]
    lines += [
        "[[sections]]",
        'name = "concepts"',
        'kind = "as-is"',
        'description = "Ideas"',
    ]
    (tmp_path / WIKI_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


def test_hard_caps_values():
    assert (HARD_CAPS.max_sources, HARD_CAPS.max_pages, HARD_CAPS.max_page_lines) == (100, 500, 400)
    assert HARD_CAPS.max_pages_per_ingest == 15 and HARD_CAPS.split_chars == 80_000


def test_profiles_sections():
    assert [s.name for s in load_profile("software")] == [
        "architecture", "components", "conventions", "operations",
        "concepts", "requirements", "decisions", "sources",
    ]
    assert {s.name: s.kind for s in load_profile("general")}["proposals"] == "to-be"


def test_unknown_profile_is_error():
    with pytest.raises(ConfigError, match="nope"):
        load_profile("nope")


def test_lowering_cap_applies(tmp_path):
    caps = load_config(write_toml(tmp_path, caps={"max_pages": 200})).effective_caps()
    assert caps.max_pages == 200
    assert caps.warn_pages == 160


def test_explicit_warn_wins(tmp_path):
    caps = load_config(write_toml(tmp_path, caps={"max_pages": 200, "warn_pages": 150})).effective_caps()
    assert (caps.max_pages, caps.warn_pages) == (200, 150)


def test_no_caps_gives_hard_caps(tmp_path):
    assert load_config(write_toml(tmp_path)).effective_caps() == HARD_CAPS


def test_raising_cap_is_error(tmp_path):
    with pytest.raises(ConfigError, match="max_pages.*500"):
        load_config(write_toml(tmp_path, caps={"max_pages": 501})).effective_caps()


def test_unknown_cap_key_is_error(tmp_path):
    with pytest.raises(ConfigError, match="max_bogus"):
        load_config(write_toml(tmp_path, caps={"max_bogus": 1})).effective_caps()


def test_missing_file_is_error(tmp_path):
    with pytest.raises(ConfigError, match="wiki.toml"):
        load_config(tmp_path)


def test_invalid_config_is_error(tmp_path):
    (tmp_path / WIKI_FILE).write_text('name = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_dump_load_roundtrip(tmp_path):
    root = write_toml(tmp_path, caps={"max_pages": 200}, name='we"ird\\ name', strict_secrets=True)
    cfg = load_config(root)
    out = tmp_path / "out"
    out.mkdir()
    (out / WIKI_FILE).write_text(dump_config(cfg), encoding="utf-8")
    assert load_config(out) == cfg
    assert dump_config(load_config(out)) == dump_config(cfg)
