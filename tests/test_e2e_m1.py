from datetime import date

from conftest import page_text
from typer.testing import CliRunner

from llm_wiki.cli import app
from llm_wiki.config import load_config
from llm_wiki.index import build_indexes
from llm_wiki.manifest import Manifest, SourceEntry, sha256_file

runner = CliRunner()


def test_e2e_m1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "demo"]).exit_code == 0
    root = tmp_path / "demo"
    monkeypatch.chdir(root)

    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "sources 0/100" in res.output and "pages 2/500" in res.output
    res = runner.invoke(app, ["lint"])
    assert res.exit_code == 0, res.output

    rel = "raw/docs/guide.md"
    raw = root / rel
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("# Guide\n\nAlways branch from main.\n", encoding="utf-8")
    sha = sha256_file(raw)
    m = Manifest.load(root)
    m.record(
        SourceEntry(
            path=rel,
            sha256=sha,
            size=raw.stat().st_size,
            mime="text/markdown",
            category="docs",
            first_seen=date(2026, 1, 1),
            status="ingested",
        )
    )
    m.save(root)
    page = root / "wiki/conventions/git-flow.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        page_text(
            "conventions.git-flow",
            "Git flow",
            f"# Git flow\n\n**TL;DR** Branch from main.\n\nWe branch from main.[^1]\n\n[^1]: {rel}\n",
            sources=[(rel, sha)],
            review_after="2099-01-01",
        ),
        encoding="utf-8",
    )
    build_indexes(root, load_config(root))

    res = runner.invoke(app, ["search", "branch"])
    assert res.exit_code == 0, res.output
    assert "conventions.git-flow" in res.output
    res = runner.invoke(app, ["lint"])
    assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "sources 1/100" in res.output and "pages 3/500" in res.output
