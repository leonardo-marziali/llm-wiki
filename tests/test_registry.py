import os
from pathlib import Path

import pytest

from llm_wiki.config import WIKI_FILE, WikiConfig, dump_config, load_profile
from llm_wiki.errors import ConfigError, WikiNotFound
from llm_wiki.registry import find_root, known_wikis, register, registry_file, resolve


def make_wiki(root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    cfg = WikiConfig(
        name=name,
        description="d",
        profile="general",
        sections=load_profile("general"),
    )
    (root / WIKI_FILE).write_text(dump_config(cfg))
    return root


def test_register_and_resolve_by_name(tmp_path):
    root = make_wiki(tmp_path / "a", "alpha")
    register("alpha", root)
    assert registry_file().exists()
    ref = resolve("alpha", tmp_path)
    assert (ref.name, ref.path, ref.origin) == ("alpha", root, "registry")


def test_first_registered_is_default(tmp_path):
    a = make_wiki(tmp_path / "a", "alpha")
    b = make_wiki(tmp_path / "b", "beta")
    register("alpha", a)
    register("beta", b)
    assert resolve(None, tmp_path / "elsewhere").name == "alpha"
    register("beta", b, make_default=True)
    assert resolve(None, tmp_path).name == "beta"


def test_reregister_updates_path(tmp_path):
    a = make_wiki(tmp_path / "a", "alpha")
    a2 = make_wiki(tmp_path / "a2", "alpha")
    register("alpha", a)
    register("alpha", a2)
    assert [r.path for r in known_wikis()] == [a2]


def test_resolve_by_path(tmp_path):
    root = make_wiki(tmp_path / "p", "pathy")
    ref = resolve(str(root), tmp_path)
    assert (ref.name, ref.path, ref.origin) == ("pathy", root, "path")


def test_resolve_from_cwd_inside_wiki(tmp_path):
    root = make_wiki(tmp_path / "w", "wiki-w")
    cwd = root / "wiki" / "conventions"
    cwd.mkdir(parents=True)
    assert find_root(cwd) == root
    assert resolve(None, cwd).path == root


def test_env_path_discovery(tmp_path, monkeypatch):
    root = make_wiki(tmp_path / "e", "envy")
    monkeypatch.setenv("LLM_WIKI_PATH", os.pathsep.join([str(root), str(tmp_path / "nope")]))
    refs = known_wikis()
    assert [(r.name, r.origin) for r in refs] == [("envy", "env")]


def test_registry_entry_wins_over_env_duplicate(tmp_path, monkeypatch):
    root = make_wiki(tmp_path / "e", "envy")
    register("reg-name", root)
    monkeypatch.setenv("LLM_WIKI_PATH", str(root))
    refs = known_wikis()
    assert [(r.name, r.origin) for r in refs] == [("reg-name", "registry")]


def test_resolve_nothing_raises_wikinotfound(tmp_path):
    with pytest.raises(WikiNotFound, match="wiki init") as e:
        resolve(None, tmp_path)
    assert str(e.value) == "no wiki found: pass --wiki NAME|PATH or run `wiki init <path>`"


def test_broken_env_wiki_is_skipped(tmp_path, monkeypatch):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / WIKI_FILE).write_text("not = [valid")
    good = make_wiki(tmp_path / "good", "good")
    monkeypatch.setenv("LLM_WIKI_PATH", os.pathsep.join([str(bad), str(good)]))
    assert [r.name for r in known_wikis()] == ["good"]
    assert resolve("good", tmp_path).path == good
    with pytest.raises(ConfigError):
        resolve(str(bad), tmp_path)


def test_non_bmp_names_round_trip(tmp_path):
    root = make_wiki(tmp_path / "wiki-\U0001F600", "smile-\U0001F600")
    register("smile-\U0001F600", root)
    assert resolve("smile-\U0001F600", tmp_path).path == root


def test_corrupt_registry_raises_config_error():
    registry_file().parent.mkdir(parents=True)
    registry_file().write_text("[wikis\n")
    with pytest.raises(ConfigError, match="wikis.toml"):
        known_wikis()


def test_dead_default_raises_wikinotfound(tmp_path):
    root = make_wiki(tmp_path / "a", "alpha")
    register("alpha", root)
    (root / WIKI_FILE).unlink()
    with pytest.raises(WikiNotFound, match="wiki init"):
        resolve(None, tmp_path / "elsewhere")


def test_malformed_env_wiki_does_not_break_resolve(tmp_path, monkeypatch):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / WIKI_FILE).write_text("not = [valid", encoding="utf-8")
    good = make_wiki(tmp_path / "good", "good")
    other = make_wiki(tmp_path / "other", "other")
    register("other", other)
    monkeypatch.setenv("LLM_WIKI_PATH", os.pathsep.join([str(bad), str(good)]))
    assert resolve(None, good).name == "good"  # by cwd
    assert resolve(None, tmp_path / "elsewhere").name == "other"  # by default
    assert resolve("other", tmp_path).name == "other"  # by name
    assert resolve("good", tmp_path).origin == "env"


def test_explicit_arg_matching_nothing_raises(tmp_path):
    root = make_wiki(tmp_path / "a", "alpha")
    register("alpha", root)
    with pytest.raises(WikiNotFound, match="wiki init"):
        resolve("nope", root)


def test_registered_wiki_by_path_returns_registry_origin(tmp_path):
    root = make_wiki(tmp_path / "a", "alpha")
    register("alpha", root)
    assert resolve(str(root), tmp_path).origin == "registry"


def test_registered_wiki_by_cwd_returns_registry_origin(tmp_path):
    root = make_wiki(tmp_path / "a", "alpha")
    register("alpha", root)
    sub = root / "wiki"
    sub.mkdir()
    ref = resolve(None, sub)
    assert (ref.name, ref.path, ref.origin) == ("alpha", root, "registry")


def test_env_var_unset_by_fixture():
    assert "LLM_WIKI_PATH" not in os.environ
