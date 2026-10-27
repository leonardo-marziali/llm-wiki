import json
from datetime import date

from conftest import PAGE_REL, page_text
from typer.testing import CliRunner

from llm_wiki.cli import app
from llm_wiki.registry import WikiRef
from llm_wiki.status import wiki_status

runner = CliRunner()


def init(path, name):
    res = runner.invoke(app, ["init", str(path), "--name", name])
    assert res.exit_code == 0, res.output


def test_status_counts_on_fresh_wiki(tmp_path):
    init(tmp_path / "demo", "demo")
    ref = WikiRef("demo", tmp_path / "demo", "path")
    st = wiki_status(ref, today=date(2026, 10, 26))
    assert st.name == "demo"
    assert st.sources == 0 and st.max_sources == 100
    assert st.pages == 2 and st.max_pages == 500
    assert st.sections and all(v == 0 for v in st.sections.values())
    assert st.stale == [] and st.inbox == 0 and st.quarantined == 0
    assert st.pending == {"new": 0, "changed": 0, "deleted": 0}
    assert (
        len(st.last_log) == 1
        and st.last_log[0].startswith("## [")
        and "init" in st.last_log[0]
    )


def test_status_counts_pending_raw_and_inbox(tmp_path, make_wiki):
    root = make_wiki(tmp_path)
    (root / "raw/docs/new.md").write_text("new\n", encoding="utf-8")
    (root / "raw/docs/guide.md").write_text("changed\n", encoding="utf-8")
    (root / "inbox").mkdir(exist_ok=True)
    (root / "inbox/.gitkeep").write_text("")
    (root / "inbox/a.pdf").write_text("x")
    (root / "inbox/b.pdf").write_text("x")
    q = root / ".wiki/quarantine"
    q.mkdir(parents=True)
    (q / "bad.txt").write_text("x")
    (q / "bad.txt.reason.json").write_text("{}")
    (root / "wiki" / PAGE_REL).write_text(
        page_text(
            "conventions.git-flow",
            "Git flow",
            "# Git flow\n",
            review_after="2026-01-02",
        ),
        encoding="utf-8",
    )
    st = wiki_status(WikiRef("Acme", root, "path"), today=date(2026, 10, 26))
    assert st.sources == 1
    assert st.pending == {"new": 1, "changed": 1, "deleted": 0}
    assert st.inbox == 2 and st.quarantined == 1
    assert st.pages == 3
    assert st.sections["conventions"] == 1
    assert st.stale == ["conventions.git-flow"]


def test_status_all_lists_registry_and_env_wikis(tmp_path, monkeypatch):
    init(tmp_path / "one", "one")
    init(tmp_path / "two", "two")
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["status", "--all"])
    assert res.exit_code == 0, res.output
    assert "one" in res.output and "two" in res.output
    assert "sources 0/100" in res.output and "pages 2/500" in res.output
    # env-only wiki
    (tmp_path / "envw").mkdir()
    init(tmp_path / "envw", "envw")
    reg = tmp_path / "home" / "wikis.toml"
    reg.write_text(
        "".join(
            ln for ln in reg.read_text().splitlines(True) if not ln.startswith('"envw"')
        )
    )
    monkeypatch.setenv("LLM_WIKI_PATH", str(tmp_path / "envw"))
    res = runner.invoke(app, ["status", "--all", "--format", "json"])
    assert res.exit_code == 0, res.output
    names = {d["name"] for d in json.loads(res.output)}
    assert names == {"one", "two", "envw"}


def test_status_all_survives_broken_wiki(tmp_path, monkeypatch):
    init(tmp_path / "good", "good")
    init(tmp_path / "bad", "bad")
    (tmp_path / "bad" / "wiki.toml").write_text("not = [valid", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["status", "--all"])
    assert res.exit_code == 0, res.output
    assert "error:" in res.output
    assert "good" in res.output and "pages 2/500" in res.output


def test_no_wiki_resolves_exit_2(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 2
    assert res.stderr.startswith("error: no wiki found")


def test_status_non_utf8_log_does_not_crash(tmp_path, monkeypatch):
    init(tmp_path / "demo", "demo")
    log = tmp_path / "demo" / "wiki" / "log.md"
    log.write_bytes(log.read_bytes() + b"\xff\n")
    st = wiki_status(
        WikiRef("demo", tmp_path / "demo", "path"), today=date(2026, 10, 26)
    )
    assert st.last_log == ["error: log.md not valid UTF-8"]
    monkeypatch.chdir(tmp_path / "demo")
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "error: log.md not valid UTF-8" in res.output


def test_status_all_non_utf8_log_keeps_all_rows(tmp_path, monkeypatch):
    init(tmp_path / "a", "a")
    init(tmp_path / "b", "b")
    log = tmp_path / "a" / "wiki" / "log.md"
    log.write_bytes(log.read_bytes() + b"\xff\n")
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["status", "--all", "--format", "json"])
    assert res.exit_code == 0, res.output
    assert {d["name"] for d in json.loads(res.output)} == {"a", "b"}


def test_status_all_catches_any_exception_per_wiki(tmp_path, monkeypatch):
    import llm_wiki.status as status_mod

    init(tmp_path / "good", "good")
    init(tmp_path / "bad", "bad")
    real = status_mod.wiki_status

    def flaky(ref, today=None):
        if ref.name == "bad":
            raise RuntimeError("boom")
        return real(ref, today)

    monkeypatch.setattr(status_mod, "wiki_status", flaky)
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["status", "--all"])
    assert res.exit_code == 0, res.output
    assert "error: boom" in res.output
    assert "good" in res.output and "pages 2/500" in res.output
