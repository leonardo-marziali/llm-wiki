import subprocess
from datetime import date
from pathlib import Path

import pytest

from llm_wiki import gitops, registry
from llm_wiki.config import load_config
from llm_wiki.errors import WikiError
from llm_wiki.init import init_wiki
from llm_wiki.lint import lint
from llm_wiki.manage import describe_wiki, move_wiki, rename_wiki

TODAY = date(2026, 11, 27)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


def _wiki(tmp_path: Path, name: str = "acme", description: str = "Old text."):
    return init_wiki(tmp_path / "w", name=name, description=description).ref


def _lines(root: Path, rel: str) -> list[str]:
    return (root / rel).read_text(encoding="utf-8").splitlines()


def _clean_lint(root: Path) -> None:
    report = lint(root, load_config(root), today=TODAY)
    assert report.errors == 0, [f.message for f in report.findings]


def test_describe_rewrites_every_site_and_commits(tmp_path):
    ref = _wiki(tmp_path)
    root = ref.path
    res = describe_wiki(ref, "  New   text. ", today=TODAY)
    assert load_config(root).description == "New text."
    assert _lines(root, "AGENTS.md")[2] == "New text."
    assert "**TL;DR** New text." in _lines(root, "wiki/overview.md")
    index = (root / "wiki/index.md").read_text(encoding="utf-8")
    assert index.startswith("# acme\n\nNew text.\n\n")
    assert _lines(root, "wiki/log.md")[-1] == "## [2026-11-27] describe | New text."
    assert _git(root, "log", "-1", "--format=%s").strip() == (
        "chore: describe wiki acme"
    )
    assert gitops.changed_paths(root) == []
    assert res.warnings == []
    assert res.committed is True
    assert res.ref == ref
    _clean_lint(root)


def test_describe_leaves_edited_overview_and_warns(tmp_path):
    ref = _wiki(tmp_path)
    root = ref.path
    ov = root / "wiki/overview.md"
    ov.write_text(
        ov.read_text(encoding="utf-8").replace(
            "**TL;DR** Old text.", "**TL;DR** Hand written."
        ),
        encoding="utf-8",
    )
    _git(root, "commit", "-qam", "docs: edit overview")
    before = ov.read_text(encoding="utf-8")
    res = describe_wiki(ref, "New text.", today=TODAY)
    assert ov.read_text(encoding="utf-8") == before
    assert len(res.warnings) == 1
    assert "wiki/overview.md" in res.warnings[0]
    assert _lines(root, "AGENTS.md")[2] == "New text."


def test_describe_refuses_dirty_wiki(tmp_path):
    ref = _wiki(tmp_path)
    root = ref.path
    (root / "wiki" / "open-questions.md").write_text("# Q\n", encoding="utf-8")
    before = (root / "wiki.toml").read_text(encoding="utf-8")
    with pytest.raises(WikiError, match="uncommitted changes"):
        describe_wiki(ref, "New text.", today=TODAY)
    assert (root / "wiki.toml").read_text(encoding="utf-8") == before


@pytest.mark.parametrize("text", ["   ", "Old text.", " Old   text. "])
def test_describe_refuses_empty_or_same(tmp_path, text):
    ref = _wiki(tmp_path)
    with pytest.raises(WikiError):
        describe_wiki(ref, text, today=TODAY)


def test_describe_in_host_repo_does_not_commit(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    _git(host, "init", "-q")
    ref = init_wiki(host / "w", name="acme", description="Old text.").ref
    _git(host, "add", "-A")
    _git(host, "commit", "-qm", "chore: add wiki")
    head = _git(host, "rev-parse", "HEAD")
    res = describe_wiki(ref, "New text.", today=TODAY)
    assert res.committed is False
    assert res.host_repo == host.resolve()
    assert _git(host, "rev-parse", "HEAD") == head
    assert load_config(ref.path).description == "New text."


def test_describe_replaces_fallback_description(tmp_path):
    ref = _wiki(tmp_path, description="")
    root = ref.path
    assert _lines(root, "AGENTS.md")[2] == "The acme wiki."
    res = describe_wiki(ref, "X.", today=TODAY)
    assert _lines(root, "AGENTS.md")[2] == "X."
    assert "**TL;DR** X." in _lines(root, "wiki/overview.md")
    assert (
        (root / "wiki/index.md")
        .read_text(encoding="utf-8")
        .startswith("# acme\n\nX.\n\n")
    )
    assert res.warnings == []
    _clean_lint(root)


def test_rename_rewrites_every_name_site(tmp_path):
    ref = _wiki(tmp_path, description="")
    root = ref.path
    res = rename_wiki(ref, "beta", today=TODAY)
    assert res.ref.name == "beta"
    assert load_config(root).name == "beta"
    agents = _lines(root, "AGENTS.md")
    assert agents[0] == "# beta: wiki rules"
    assert agents[2] == "The beta wiki."
    overview = _lines(root, "wiki/overview.md")
    assert "summary: Entry point of the beta wiki." in overview
    assert "**TL;DR** The beta wiki." in overview
    glossary = _lines(root, "wiki/glossary.md")
    assert "summary: Terms used in the beta wiki." in glossary
    assert "**TL;DR** Terms used across the beta wiki." in glossary
    index = (root / "wiki/index.md").read_text(encoding="utf-8")
    assert index.startswith("# beta\n\n\n\n")
    assert registry.registered_path("beta") == root
    assert registry.registered_path("acme") is None
    assert _lines(root, "wiki/log.md")[-1] == "## [2026-11-27] rename | acme → beta"
    assert _git(root, "log", "-1", "--format=%s").strip() == (
        "chore: rename wiki acme to beta"
    )
    assert gitops.changed_paths(root) == []
    assert res.warnings == []
    _clean_lint(root)


def test_rename_keeps_custom_description(tmp_path):
    ref = _wiki(tmp_path, description="Ours.")
    root = ref.path
    res = rename_wiki(ref, "beta", today=TODAY)
    assert _lines(root, "AGENTS.md")[2] == "Ours."
    assert "**TL;DR** Ours." in _lines(root, "wiki/overview.md")
    assert (
        (root / "wiki/index.md")
        .read_text(encoding="utf-8")
        .startswith("# beta\n\nOurs.\n\n")
    )
    assert res.warnings == []


def test_rename_moves_default(tmp_path):
    ref = _wiki(tmp_path)
    rename_wiki(ref, "beta", today=TODAY)
    found = registry.resolve(None, tmp_path)
    assert (found.name, found.path) == ("beta", ref.path)


def test_rename_refuses_bad_taken_or_same(tmp_path):
    ref = _wiki(tmp_path)
    init_wiki(tmp_path / "other", name="other")
    before = (ref.path / "wiki.toml").read_text(encoding="utf-8")
    for bad in ("Bad Name", "other", "acme"):
        with pytest.raises(WikiError):
            rename_wiki(ref, bad, today=TODAY)
    assert (ref.path / "wiki.toml").read_text(encoding="utf-8") == before
    assert gitops.changed_paths(ref.path) == []


def test_rename_env_wiki_skips_registry(tmp_path, monkeypatch):
    root = _wiki(tmp_path).path
    registry.registry_file().unlink()
    monkeypatch.setenv("LLM_WIKI_PATH", str(root))
    ref = registry.resolve("acme", tmp_path)
    assert ref.origin == "env"
    res = rename_wiki(ref, "beta", today=TODAY)
    assert res.ref.origin == "env"
    assert load_config(root).name == "beta"
    assert not registry.registry_file().exists()


def test_move_to_missing_path(tmp_path):
    ref = _wiki(tmp_path)
    target = tmp_path / "a" / "b"
    moved = move_wiki(ref, target)
    assert moved == registry.WikiRef("acme", target.resolve(), "registry")
    assert not ref.path.exists()
    assert (target / "wiki.toml").is_file()
    assert registry.registered_path("acme") == target.resolve()
    assert _git(target, "status", "--porcelain") == ""
    assert registry.resolve("acme", tmp_path).path == target.resolve()


def test_move_into_empty_dir(tmp_path):
    ref = _wiki(tmp_path)
    target = tmp_path / "empty"
    target.mkdir()
    move_wiki(ref, target)
    assert (target / "wiki.toml").is_file()
    assert registry.registered_path("acme") == target.resolve()


def test_move_refuses_non_empty_target(tmp_path):
    ref = _wiki(tmp_path)
    target = tmp_path / "full"
    target.mkdir()
    (target / "x").write_text("x", encoding="utf-8")
    with pytest.raises(WikiError, match="not empty"):
        move_wiki(ref, target)
    assert (ref.path / "wiki.toml").is_file()


@pytest.mark.parametrize("sub", ["sub", "sub/deeper", "."])
def test_move_refuses_inside_itself_or_same(tmp_path, sub):
    ref = _wiki(tmp_path)
    with pytest.raises(WikiError):
        move_wiki(ref, ref.path / sub)
    assert (ref.path / "wiki.toml").is_file()
    assert not (ref.path / "sub").exists()


def test_move_refuses_host_repo_wiki(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    _git(host, "init", "-q")
    ref = init_wiki(host / "w", name="acme").ref
    with pytest.raises(WikiError, match=r"git mv.*wiki init"):
        move_wiki(ref, tmp_path / "elsewhere")
    assert (ref.path / "wiki.toml").is_file()


def test_move_refuses_env_wiki(tmp_path, monkeypatch):
    root = _wiki(tmp_path).path
    registry.registry_file().unlink()
    monkeypatch.setenv("LLM_WIKI_PATH", str(root))
    ref = registry.resolve("acme", tmp_path)
    with pytest.raises(WikiError, match="LLM_WIKI_PATH"):
        move_wiki(ref, tmp_path / "elsewhere")
    assert (root / "wiki.toml").is_file()


def test_describe_allows_pending_inbox_and_leaves_it_out(tmp_path):
    ref = _wiki(tmp_path)
    root = ref.path
    (root / "inbox" / "x.md").write_text("x", encoding="utf-8")
    describe_wiki(ref, "New text.", today=TODAY)
    assert gitops.changed_paths(root) == ["inbox/x.md"]


def test_describe_refuses_leftover_op_branch(tmp_path):
    ref = _wiki(tmp_path)
    _git(ref.path, "switch", "-qc", "wiki/ingest-x")
    with pytest.raises(WikiError, match="wiki/ingest-x"):
        describe_wiki(ref, "New text.", today=TODAY)
    assert load_config(ref.path).description == "Old text."


def test_describe_leaves_duplicated_line_and_warns(tmp_path):
    ref = _wiki(tmp_path)
    root = ref.path
    agents = root / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8") + "\nOld text.\n")
    _git(root, "commit", "-qam", "docs: repeat")
    before = agents.read_text(encoding="utf-8")
    res = describe_wiki(ref, "New text.", today=TODAY)
    assert agents.read_text(encoding="utf-8") == before
    assert [w for w in res.warnings if w.startswith("AGENTS.md")] != []


def test_rename_takes_over_stale_entry(tmp_path):
    import shutil

    ref = _wiki(tmp_path)
    gone = init_wiki(tmp_path / "gone", name="beta").ref.path
    shutil.rmtree(gone)
    res = rename_wiki(ref, "beta", today=TODAY)
    assert registry.registered_path("beta") == ref.path
    assert registry.registered_path("acme") is None
    assert res.ref.name == "beta"


def test_rename_to_its_registry_key(tmp_path):
    ref = _wiki(tmp_path)
    registry.rename("acme", "foo")
    ref = registry.resolve("foo", tmp_path)
    res = rename_wiki(ref, "foo", today=TODAY)
    assert load_config(ref.path).name == "foo"
    assert registry.registered_path("foo") == ref.path
    assert res.ref.name == "foo"


def test_rename_commit_failure_rolls_back(tmp_path, monkeypatch):
    from llm_wiki.errors import GitError

    ref = _wiki(tmp_path)
    root = ref.path

    def boom(*a, **k):
        raise GitError("hook refused")

    monkeypatch.setattr(gitops, "stage_commit", boom)
    with pytest.raises(GitError):
        rename_wiki(ref, "beta", today=TODAY)
    assert load_config(root).name == "acme"
    assert gitops.changed_paths(root) == []
    assert registry.registered_path("acme") == root
    assert registry.registered_path("beta") is None


def test_move_unregistered_wiki_leaves_registry_alone(tmp_path):
    ref = _wiki(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(ref.path), str(clone))
    cref = registry.resolve(str(clone), tmp_path)
    assert cref.origin == "path"
    moved = move_wiki(cref, tmp_path / "d")
    assert moved.origin == "path"
    assert registry.registered_path("acme") == ref.path
