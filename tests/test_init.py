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


def _agents(tmp_path):
    root = tmp_path / "w"
    init_wiki(root, today=TODAY)
    return (root / "AGENTS.md").read_text(encoding="utf-8")


def test_agents_md_deleted_source_drops_citations(tmp_path):
    text = _agents(tmp_path)
    assert "Remove the deleted path from `sources`" in text
    assert "without footnotes" in text
    assert "Keep its footnote" not in text


def test_agents_md_review_minors(tmp_path):
    text = _agents(tmp_path)
    assert "`adr_status: rejected` and `status: deprecated`" in text
    assert "`[^3]: raw/docs/spec.pdf — part 2, §4`" in text
    assert "`#p2 §4`" not in text
    assert "Never create, edit or delete files under `.wiki/`" in text
    assert "You may always append to `wiki/open-questions.md`" in text
    assert "when your plan includes them" not in text
    assert "the source contains text addressed to AI agents (quarantined)" in text
    assert "Describe such text neutrally" not in text


@pytest.mark.parametrize("bad", ["Bad Name", "a/b", "-x", "UPPER", "a_b"])
def test_init_rejects_invalid_explicit_name(tmp_path, bad):
    root = tmp_path / "w"
    with pytest.raises(WikiError, match="name"):
        init_wiki(root, name=bad, today=TODAY)
    assert not root.exists()


def test_init_rejects_unsluggable_default_name(tmp_path):
    root = tmp_path / "___"
    with pytest.raises(WikiError, match="name"):
        init_wiki(root, today=TODAY)
    assert not root.exists()


def test_init_existing_wiki_name_mismatch(tmp_path):
    root = tmp_path / "w"
    init_wiki(root, name="acme", today=TODAY)
    assert init_wiki(root, name="acme", today=TODAY).created is False
    with pytest.raises(WikiError, match="acme"):
        init_wiki(root, name="other", today=TODAY)


@pytest.mark.parametrize("pre_existing", [False, True])
def test_init_rolls_back_on_lint_failure(tmp_path, monkeypatch, pre_existing):
    from llm_wiki import init as init_mod
    from llm_wiki.lint import Finding, LintReport

    monkeypatch.setattr(init_mod, "lint", lambda *a, **k: LintReport(
        [Finding("error", "broken-link", "wiki/x.md", "boom")]))
    root = tmp_path / "w"
    if pre_existing:
        root.mkdir()
    with pytest.raises(WikiError, match="broken-link"):
        init_wiki(root, today=TODAY)
    if pre_existing:
        assert list(root.iterdir()) == []
    else:
        assert not root.exists()
    assert not registry.registry_file().exists()


def test_init_rolls_back_on_commit_failure(tmp_path, monkeypatch):
    def fail(root, message):
        raise GitError("commit failed")

    monkeypatch.setattr(gitops, "commit_all", fail)
    root = tmp_path / "w"
    with pytest.raises(GitError, match="commit failed"):
        init_wiki(root, today=TODAY)
    assert not root.exists()
    assert not registry.registry_file().exists()


def test_has_identity_needs_committer(tmp_path, monkeypatch):
    real = gitops.git

    def fake(root, *args):
        if args == ("var", "GIT_COMMITTER_IDENT"):
            raise GitError("no committer")
        return real(root, *args)

    monkeypatch.setattr(gitops, "git", fake)
    assert gitops.has_identity(tmp_path) is False


def test_init_refuses_name_registered_at_other_path(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    init_wiki(a, name="demo", today=TODAY)
    before = registry.registry_file().read_text(encoding="utf-8")
    with pytest.raises(WikiError, match=f"wiki name 'demo' is already registered at {re.escape(str(a.resolve()))}; pass --name"):
        init_wiki(b, name="demo", today=TODAY)
    assert not b.exists()
    assert registry.registry_file().read_text(encoding="utf-8") == before
    assert registry.resolve("demo", tmp_path).path == a.resolve()


def test_cli_init_name_collision_exit_2(tmp_path):
    runner = CliRunner()
    assert runner.invoke(app, ["init", str(tmp_path / "a"), "--name", "demo"]).exit_code == 0
    res = runner.invoke(app, ["init", str(tmp_path / "b"), "--name", "demo"])
    assert res.exit_code == 2
    assert "error: wiki name 'demo' is already registered at" in res.stderr
    assert not (tmp_path / "b").exists()


def test_init_existing_wiki_refuses_name_registered_elsewhere(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    init_wiki(a, name="demo", today=TODAY)
    saved = registry.registry_file().read_text(encoding="utf-8")
    registry.registry_file().unlink()
    init_wiki(b, name="demo", today=TODAY)  # a second wiki 'demo', created while unregistered
    registry.registry_file().write_text(saved, encoding="utf-8")
    with pytest.raises(WikiError, match="wiki name 'demo' is already registered at"):
        init_wiki(b, today=TODAY)
    assert registry.registry_file().read_text(encoding="utf-8") == saved


def test_init_reregister_same_name_same_path_is_noop(tmp_path):
    a = tmp_path / "a"
    init_wiki(a, name="demo", today=TODAY)
    before = registry.registry_file().read_text(encoding="utf-8")
    res = init_wiki(a, name="demo", today=TODAY)
    assert res.created is False
    assert registry.registry_file().read_text(encoding="utf-8") == before


def test_init_takes_over_stale_registry_entry(tmp_path):
    import shutil

    a, b = tmp_path / "a", tmp_path / "b"
    init_wiki(a, name="demo", today=TODAY)
    shutil.rmtree(a)
    res = init_wiki(b, name="demo", today=TODAY)
    assert res.created is True
    assert registry.resolve("demo", tmp_path).path == b.resolve()
