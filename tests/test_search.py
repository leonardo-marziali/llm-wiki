import json
from dataclasses import fields
from pathlib import Path

from typer.testing import CliRunner

from llm_wiki.cli import app
from llm_wiki.config import Section, WikiConfig, dump_config
from llm_wiki.registry import WikiRef
from llm_wiki.search import SearchHit, search, tokenize

CFG = WikiConfig(
    name="w", description="d", profile="general",
    sections=[Section(name="sys", kind="as-is", description="x")],
)


def make_wiki(root: Path, name: str = "w") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "wiki.toml").write_text(dump_config(CFG.model_copy(update={"name": name})), encoding="utf-8")
    return root


def put(root: Path, rel: str, id_: str, title: str, body: str = "body", summary: str = "S.",
        status: str = "as-is", tags: str = "[]") -> None:
    p = root / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nid: {id_}\ntitle: {title}\ntype: concept\nstatus: {status}\nsummary: {summary}\n"
        f"confidence: high\nreview_status: draft\nlast_verified: 2026-01-01\n"
        f"review_after: 2026-07-01\ntags: {tags}\n---\n{body}\n",
        encoding="utf-8",
    )


def ref(root: Path, name: str = "w") -> WikiRef:
    return WikiRef(name, root, "path")


def test_tokenize():
    assert tokenize("Hello, A b-c World_1 x") == ["hello", "world_1"]
    assert tokenize("Foo bar a") == ["foo", "bar"]


def test_title_match_outranks_body_match(tmp_path):
    w = make_wiki(tmp_path / "w")
    put(w, "sys/a.md", "body-hit", "Other", body="kafka kafka")
    put(w, "sys/b.md", "title-hit", "Kafka cluster", body="nothing")
    put(w, "sys/c.md", "none", "Unrelated")
    hits = search([ref(w)], "kafka")
    assert [h.id for h in hits] == ["title-hit", "body-hit"]
    assert hits[0].score > hits[1].score > 0
    assert hits[0].path == str(w / "wiki/sys/b.md")


def test_status_filter(tmp_path):
    w = make_wiki(tmp_path / "w")
    put(w, "sys/a.md", "a", "Kafka", status="as-is")
    put(w, "sys/b.md", "b", "Kafka", status="to-be")
    assert [h.id for h in search([ref(w)], "kafka", status="to-be")] == ["b"]


def test_section_filter(tmp_path):
    w = make_wiki(tmp_path / "w")
    put(w, "sys/a.md", "a", "Kafka")
    put(w, "other/b.md", "b", "Kafka")
    put(w, "overview.md", "overview", "Kafka")
    assert [h.id for h in search([ref(w)], "kafka", section="other")] == ["b"]
    assert sorted(h.id for h in search([ref(w)], "kafka")) == ["a", "b", "overview"]


def test_search_across_wikis_all(tmp_path):
    w1 = make_wiki(tmp_path / "w1", "one")
    w2 = make_wiki(tmp_path / "w2", "two")
    put(w1, "sys/a.md", "a", "Kafka")
    put(w2, "sys/b.md", "b", "Kafka")
    hits = search([ref(w1, "one"), ref(w2, "two")], "kafka")
    assert sorted((h.wiki, h.id) for h in hits) == [("one", "a"), ("two", "b")]


def test_search_skips_unparseable_pages(tmp_path):
    w = make_wiki(tmp_path / "w")
    put(w, "sys/a.md", "a", "Kafka")
    (w / "wiki/sys/bad.md").write_text("---\n: : [\n---\nkafka\n", encoding="utf-8")
    assert [h.id for h in search([ref(w)], "kafka")] == ["a"]


def test_top_limits_results(tmp_path):
    w = make_wiki(tmp_path / "w")
    for i in range(5):
        put(w, f"sys/p{i}.md", f"p{i}", "Kafka")
    assert len(search([ref(w)], "kafka", top=3)) == 3
    assert search([ref(w)], "zzzzz") == []


def test_cli_search_json(tmp_path):
    w = make_wiki(tmp_path / "w")
    put(w, "sys/a.md", "a", "Kafka")
    r = CliRunner().invoke(app, ["search", "kafka", "--wiki", str(w), "--format", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert isinstance(data, list) and len(data) == 1
    assert set(data[0]) == {f.name for f in fields(SearchHit)}
    assert data[0]["last_verified"] == "2026-01-01"


def test_cli_text_and_no_match(tmp_path):
    w = make_wiki(tmp_path / "w")
    put(w, "sys/a.md", "a", "Kafka", summary="Broker.")
    r = CliRunner().invoke(app, ["search", "kafka", "--wiki", str(w)])
    assert r.exit_code == 0
    assert r.stdout.strip().endswith("w:a  Kafka — Broker. [as-is, 2026-01-01]")
    r = CliRunner().invoke(app, ["search", "zzzzz", "--wiki", str(w)])
    assert r.exit_code == 0 and r.stdout.strip() == "no matches"


def test_cli_all_and_wiki_conflict(tmp_path):
    w = make_wiki(tmp_path / "w")
    r = CliRunner().invoke(app, ["search", "kafka", "--wiki", str(w), "--all"])
    assert r.exit_code == 2
    assert "use either --wiki or --all" in r.stderr
