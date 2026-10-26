import pytest

from llm_wiki.config import (
    LlmOverrides, WikiConfig, dump_config, home_dir, load_config, load_llm_settings, load_profile, user_config_file,
)
from llm_wiki.errors import ConfigError


def write_user(text):
    user_config_file().parent.mkdir(parents=True, exist_ok=True)
    user_config_file().write_text(text, encoding="utf-8")


def write_wiki(tmp_path, llm_block=""):
    (tmp_path / "wiki.toml").write_text(
        'name = "d"\ndescription = "d"\nprofile = "general"\n'
        + llm_block
        + '[[sections]]\nname = "concepts"\nkind = "as-is"\ndescription = "Ideas"\n',
        encoding="utf-8",
    )
    return tmp_path


def test_defaults_without_any_config():
    s = load_llm_settings(None)
    assert s.plan_model == "claude-sonnet-5-5"
    assert s.deep_model == "claude-opus-5-5"
    assert s.budget("edit") == 2.0


def test_home_dir_uses_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_WIKI_HOME", str(tmp_path / "h"))
    assert home_dir() == tmp_path / "h"
    assert user_config_file() == tmp_path / "h" / "config.toml"


def test_user_config_overrides_defaults():
    write_user('[llm]\nplan_model = "claude-haiku-5-5"\n')
    s = load_llm_settings(None)
    assert s.model("plan") == "claude-haiku-5-5"
    assert s.model("edit") == "claude-sonnet-5-5"


def test_wiki_overrides_user_config(tmp_path):
    write_user("[llm]\nplan_budget_usd = 0.5\n")
    root = write_wiki(tmp_path, "[llm]\nplan_budget_usd = 0.25\n")
    assert load_llm_settings(root).budget("plan") == 0.25


def test_non_positive_budget_is_error(tmp_path):
    write_user("[llm]\nedit_budget_usd = 0\n")
    with pytest.raises(ConfigError, match="edit_budget_usd"):
        load_llm_settings(None)


def test_unknown_llm_key_is_error_in_user_config():
    write_user('[llm]\nmodle = "x"\n')
    with pytest.raises(ConfigError, match="modle"):
        load_llm_settings(None)


def test_unknown_llm_key_is_error_in_wiki(tmp_path):
    root = write_wiki(tmp_path, '[llm]\nmodle = "x"\n')
    with pytest.raises(ConfigError, match="modle"):
        load_config(root)
    with pytest.raises(ConfigError, match="modle"):
        load_llm_settings(root)


def test_invalid_user_config_toml_is_error():
    write_user("[llm\n")
    with pytest.raises(ConfigError, match="config.toml"):
        load_llm_settings(None)


def test_dump_config_round_trips_llm(tmp_path):
    base = dict(name="d", description="d", profile="general", sections=load_profile("general"))
    cfg = WikiConfig(**base, llm=LlmOverrides(deep_model="x", edit_budget_usd=1.5))
    (tmp_path / "wiki.toml").write_text(dump_config(cfg), encoding="utf-8")
    assert load_config(tmp_path).llm == cfg.llm
    assert "[llm]" not in dump_config(WikiConfig(**base))


def test_non_table_llm_is_error(tmp_path):
    write_user("llm = 3\n")
    with pytest.raises(ConfigError, match="config.toml"):
        load_llm_settings(None)
    (user_config_file()).unlink()
    root = write_wiki(tmp_path)
    text = (root / "wiki.toml").read_text(encoding="utf-8")
    (root / "wiki.toml").write_text("llm = 3\n" + text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_llm_settings(root)
