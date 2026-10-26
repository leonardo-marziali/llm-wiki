import re
import subprocess
from datetime import date

import pytest
from typer.testing import CliRunner

from llm_wiki import gitops, registry
from llm_wiki.cli import app
from llm_wiki.config import load_config
from llm_wiki.errors import GitError, WikiError
from llm_wiki.init import InitResult, init_wiki
from llm_wiki.lint import lint
from llm_wiki.pages import read_page

TODAY = date(2026, 10, 26)

AGENTS_HEADINGS = [
    "Purpose and layers",
    "Hard rules",
    "Sections and routing",
    "Page types",
    "Frontmatter schema",
    "Page body template",
    "Size and naming rules",
    "Citations and linking",
    "Contradictions and supersession",
    "As-is / to-be policy",
    "Fact preservation",
    "Extraction by source format",
    "Updates, changed and deleted sources",
    "Security",
    "Log and index",
    "How coding agents use this wiki",
]

SPEC_PATHS = [
    "AGENTS.md",
    "CLAUDE.md",
    "wiki.toml",
    ".gitignore",
    "inbox/.gitkeep",
    "raw/.gitkeep",
    "wiki/index.md",
    "wiki/log.md",
    "wiki/overview.md",
    "wiki/glossary.md",
    "wiki/open-questions.md",
    ".wiki/manifest.json",
    ".wiki/converted/.gitkeep",
    ".wiki/reports/.gitkeep",
]


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def test_init_creates_tree_and_lint_clean(tmp_path):
    root = tmp_path / "My Wiki"
    res = init_wiki(root, today=TODAY)
    assert isinstance(res, InitResult) and res.created
    assert res.ref.name == "my-wiki"
    for rel in SPEC_PATHS:
        assert (root / rel).is_file(), rel
    cfg = load_config(root)
    assert cfg.name == "my-wiki" and cfg.profile == "software"
    for s in cfg.sections:
        assert (root / "wiki" / s.name / "_index.md").is_file(), s.name
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"
    assert (root / "wiki/log.md").read_text(encoding="utf-8") == "# Log\n\n## [2026-10-26] init | my-wiki\n"
    assert (root / "wiki/open-questions.md").read_text(encoding="utf-8") == "# Open questions\n"
    gi = (root / ".gitignore").read_text(encoding="utf-8").split()
    for entry in (".wiki/quarantine/", ".wiki/runs/", ".wiki/tmp/", ".env*"):
        assert entry in gi
    for name, type_ in (("overview", "overview"), ("glossary", "glossary")):
        page = read_page(root, root / "wiki" / f"{name}.md")
        assert page.error is None and page.fm.type == type_ and page.fm.id == name
        assert page.fm.review_after == date(2027, 1, 24)
        assert any(line.startswith("**TL;DR**") for line in page.body.split("\n"))
    report = lint(root, cfg, today=TODAY)
    assert report.errors == 0, report.findings
    assert report.warnings == 0, report.findings
    log = _git(root, "log", "--oneline").splitlines()
    assert len(log) == 1 and log[0].endswith("chore: init wiki my-wiki")
    assert _git(root, "status", "--porcelain") == ""
    assert _git(root, "branch", "--show-current").strip() == "main"
    assert registry.resolve("my-wiki", tmp_path).path == root.resolve()


def test_init_general_profile_sections(tmp_path):
    root = tmp_path / "notes"
    init_wiki(root, name="notes", profile="general", description="Personal notes.", today=TODAY)
    cfg = load_config(root)
    assert [s.name for s in cfg.sections] == ["concepts", "topics", "howto", "references", "proposals", "sources"]
    assert cfg.description == "Personal notes."
    for s in cfg.sections:
        assert (root / "wiki" / s.name / "_index.md").is_file()
    assert not (root / "wiki" / "conventions").exists()
    assert "proposals" in (root / "AGENTS.md").read_text(encoding="utf-8")
    assert lint(root, cfg, today=TODAY).errors == 0


def test_init_existing_wiki_only_registers(tmp_path):
    root = tmp_path / "w"
    init_wiki(root, name="acme", today=TODAY)
    registry.registry_file().unlink()
    head = _git(root, "rev-parse", "HEAD")
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}
    res = init_wiki(root, today=TODAY)
    assert res.created is False
    assert res.ref.name == "acme" and res.ref.path == root.resolve()
    assert registry.resolve("acme", tmp_path).path == root.resolve()
    assert _git(root, "rev-parse", "HEAD") == head
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}
    assert after == before


def test_init_non_empty_dir_refused(tmp_path):
    root = tmp_path / "stuff"
    root.mkdir()
    (root / "file.txt").write_text("x", encoding="utf-8")
    with pytest.raises(WikiError, match="not empty and not a wiki"):
        init_wiki(root, today=TODAY)
    assert sorted(p.name for p in root.iterdir()) == ["file.txt"]


def test_init_empty_existing_dir_ok(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    assert init_wiki(root, today=TODAY).created


def test_agents_md_has_all_headings_in_order(tmp_path):
    root = tmp_path / "w"
    init_wiki(root, today=TODAY)
    text = (root / "AGENTS.md").read_text(encoding="utf-8")
    prose = re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)  # body template has ## lines
    h2 = re.findall(r"^## (.+)$", prose, re.M)
    titles = [re.sub(r"^\d+\.\s*", "", h).strip() for h in h2]
    assert titles == AGENTS_HEADINGS
    assert "{{" not in text and "}}" not in text
    assert len(text) < 26_000  # "about 25k"


def test_agents_md_mentions_sections_and_caps(tmp_path):
    root = tmp_path / "w"
    init_wiki(root, name="acme", description="Acme platform knowledge.", today=TODAY)
    text = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "400" in text and "conventions" in text
    assert "acme" in text and "Acme platform knowledge." in text
    for s in load_config(root).sections:
        assert f"`{s.name}/`" in text
    for cap in ("100", "500", "15", "80,000"):
        assert cap in text


def test_init_without_git_identity_fails_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(gitops, "has_identity", lambda root: False)
    root = tmp_path / "w"
    root.mkdir()
    with pytest.raises(GitError, match="configure git user.name and user.email"):
        init_wiki(root, today=TODAY)
    assert list(root.iterdir()) == []
    assert not registry.registry_file().exists()


def test_gitops_helpers(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    gitops.init_repo(root)
    assert gitops.has_identity(root)
    (root / "a.txt").write_text("a", encoding="utf-8")
    sha = gitops.commit_all(root, "first")
    assert re.fullmatch(r"[0-9a-f]{40}", sha)
    assert gitops.git(root, "rev-parse", "HEAD").strip() == sha
    with pytest.raises(GitError, match="unknown-subcommand|not a git command"):
        gitops.git(root, "unknown-subcommand")


def test_cli_init_messages(tmp_path):
    root = tmp_path / "w"
    r = CliRunner().invoke(app, ["init", str(root), "--name", "acme", "--profile", "general",
                                 "--description", "Acme."])
    assert r.exit_code == 0, r.output
    assert r.stdout.strip() == f"created wiki acme at {root.resolve()}"
    assert load_config(root).profile == "general"
    r = CliRunner().invoke(app, ["init", str(root)])
    assert r.exit_code == 0, r.output
    assert r.stdout.strip() == f"registered existing wiki acme at {root.resolve()}"
    other = tmp_path / "full"
    other.mkdir()
    (other / "x").write_text("x", encoding="utf-8")
    r = CliRunner().invoke(app, ["init", str(other)])
    assert r.exit_code == 2 and "not empty and not a wiki" in r.stderr
