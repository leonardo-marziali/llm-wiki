import subprocess
from datetime import date
from pathlib import Path

import pytest

from llm_wiki import gitops
from llm_wiki.config import load_config
from llm_wiki.errors import WikiError
from llm_wiki.init import init_wiki
from llm_wiki.lint import lint
from llm_wiki.manage import describe_wiki

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
    (root / "inbox" / "x.md").write_text("x", encoding="utf-8")
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
