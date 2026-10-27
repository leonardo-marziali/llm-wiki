import json

import pytest

from llm_wiki.caps import HARD_CAPS
from llm_wiki.config import (
    WIKI_FILE,
    WikiConfig,
    dump_config,
    load_config,
    load_profile,
)
from llm_wiki.errors import ConfigError


def write_toml(tmp_path, caps=None, **overrides):
    """Write a valid wiki.toml (with overrides) into tmp_path; return the directory."""
    fields = {"name": "demo", "description": "A demo wiki", "profile": "general"}
    fields.update(overrides)
    lines = [f"{k} = {json.dumps(v)}" for k, v in fields.items() if isinstance(v, str)]
    lines += [
        f"{k} = {str(v).lower()}" for k, v in fields.items() if isinstance(v, bool)
    ]
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
    assert (HARD_CAPS.max_sources, HARD_CAPS.max_pages, HARD_CAPS.max_page_lines) == (
        100,
        500,
        400,
    )
    assert HARD_CAPS.max_pages_per_ingest == 15
    assert HARD_CAPS.split_chars == 80_000


def test_profiles_sections():
    assert [s.name for s in load_profile("software")] == [
        "architecture",
        "components",
        "conventions",
        "operations",
        "concepts",
        "requirements",
        "decisions",
        "sources",
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
    caps = load_config(
        write_toml(tmp_path, caps={"max_pages": 200, "warn_pages": 150})
    ).effective_caps()
    assert (caps.max_pages, caps.warn_pages) == (200, 150)


def test_no_caps_gives_hard_caps(tmp_path):
    assert load_config(write_toml(tmp_path)).effective_caps() == HARD_CAPS


def test_raising_cap_is_error(tmp_path):
    cfg = load_config(write_toml(tmp_path, caps={"max_pages": 501}))
    with pytest.raises(ConfigError, match=r"max_pages.*500"):
        cfg.effective_caps()


def test_unknown_cap_key_is_error(tmp_path):
    cfg = load_config(write_toml(tmp_path, caps={"max_bogus": 1}))
    with pytest.raises(ConfigError, match="max_bogus"):
        cfg.effective_caps()


def test_missing_file_is_error(tmp_path):
    with pytest.raises(ConfigError, match=r"wiki\.toml"):
        load_config(tmp_path)


def test_invalid_config_is_error(tmp_path):
    (tmp_path / WIKI_FILE).write_text('name = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_dump_load_roundtrip(tmp_path):
    root = write_toml(
        tmp_path, caps={"max_pages": 200}, name='we"ird\\ name', strict_secrets=True
    )
    cfg = load_config(root)
    out = tmp_path / "out"
    out.mkdir()
    (out / WIKI_FILE).write_text(dump_config(cfg), encoding="utf-8")
    assert load_config(out) == cfg
    assert dump_config(load_config(out)) == dump_config(cfg)


def test_profile_alias():
    from typing import get_args

    from llm_wiki.config import Profile, WikiConfig

    assert get_args(Profile) == ("software", "general")
    assert WikiConfig.model_fields["profile"].annotation == Profile


def test_unknown_top_level_key_is_error(tmp_path):
    path = write_toml(tmp_path, strict_secret=True)
    with pytest.raises(ConfigError, match="strict_secret"):
        load_config(path)


def test_unknown_section_key_is_error(tmp_path):
    root = write_toml(tmp_path)
    p = root / WIKI_FILE
    p.write_text(p.read_text(encoding="utf-8") + 'kidn = "as-is"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="kidn"):
        load_config(root)


@pytest.mark.parametrize("value", [0, -1])
def test_cap_below_one_is_error(tmp_path, value):
    cfg = load_config(write_toml(tmp_path, caps={"max_pages": value}))
    with pytest.raises(ConfigError, match="max_pages"):
        cfg.effective_caps()


def test_warn_cap_below_one_is_error(tmp_path):
    cfg = load_config(write_toml(tmp_path, caps={"warn_pages": 0}))
    with pytest.raises(ConfigError, match="warn_pages"):
        cfg.effective_caps()


@pytest.mark.parametrize(
    "caps",
    [
        {"max_pages": 100, "warn_pages": 200},  # explicit warn above lowered max
        {"warn_sources": 60, "max_sources": 50},
        {"max_section_pages": 50, "warn_section_pages": 51},
    ],
)
def test_warn_above_max_is_error(tmp_path, caps):
    warn = next(k for k in caps if k.startswith("warn_"))
    cfg = load_config(write_toml(tmp_path, caps=caps))
    with pytest.raises(ConfigError, match=warn):
        cfg.effective_caps()


def test_warn_equal_to_max_is_ok(tmp_path):
    caps = load_config(
        write_toml(tmp_path, caps={"max_pages": 100, "warn_pages": 100})
    ).effective_caps()
    assert caps.warn_pages == caps.max_pages == 100


def test_derived_warn_never_zero(tmp_path, make_wiki):
    from datetime import date

    from llm_wiki.lint import lint
    from llm_wiki.manifest import Manifest

    assert (
        WikiConfig(
            name="d",
            description="d",
            profile="general",
            sections=[],
            caps={"max_sources": 1},
        )
        .effective_caps()
        .warn_sources
        == 1
    )
    root = make_wiki(tmp_path)
    m = Manifest.load(root)
    for e in list(m.entries):
        m.mark_deleted(e)
    m.save(root)
    cfg = load_config(root).model_copy(update={"caps": {"max_sources": 1}})
    report = lint(root, cfg, today=date(2026, 10, 26))
    assert "cap-sources-warn" not in {f.code for f in report.findings}
