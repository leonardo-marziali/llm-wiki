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
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def test_init_creates_tree_and_lint_clean(tmp_path):
    root = tmp_path / "My Wiki"
    res = init_wiki(root, today=TODAY)
    assert isinstance(res, InitResult)
    assert res.created
    assert res.ref.name == "my-wiki"
    for rel in SPEC_PATHS:
        assert (root / rel).is_file(), rel
    cfg = load_config(root)
    assert cfg.name == "my-wiki"
    assert cfg.profile == "software"
    for s in cfg.sections:
        assert (root / "wiki" / s.name / "_index.md").is_file(), s.name
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"
    assert (root / "wiki/log.md").read_text(
        encoding="utf-8"
    ) == "# Log\n\n## [2026-10-26] init | my-wiki\n"
    assert (root / "wiki/open-questions.md").read_text(
        encoding="utf-8"
    ) == "# Open questions\n"
    gi = (root / ".gitignore").read_text(encoding="utf-8").split()
    for entry in (
        ".wiki/quarantine/",
        ".wiki/runs/",
        ".wiki/tmp/",
        ".wiki/reports/",
        ".env*",
    ):
        assert entry in gi
    for name, type_ in (("overview", "overview"), ("glossary", "glossary")):
        page = read_page(root, root / "wiki" / f"{name}.md")
        assert page.error is None
        assert page.fm is not None
        assert page.fm.type == type_
        assert page.fm.id == name
        assert page.fm.review_after == date(2027, 1, 24)
        assert any(line.startswith("**TL;DR**") for line in page.body.split("\n"))
    report = lint(root, cfg, today=TODAY)
    assert report.errors == 0, report.findings
    assert report.warnings == 0, report.findings
    log = _git(root, "log", "--oneline").splitlines()
    assert len(log) == 1
    assert log[0].endswith("chore: init wiki my-wiki")
    assert _git(root, "status", "--porcelain") == ""
    assert _git(root, "branch", "--show-current").strip() == "main"
    assert registry.resolve("my-wiki", tmp_path).path == root.resolve()


def test_init_general_profile_sections(tmp_path):
    root = tmp_path / "notes"
    init_wiki(
        root,
        name="notes",
        profile="general",
        description="Personal notes.",
        today=TODAY,
    )
    cfg = load_config(root)
    assert [s.name for s in cfg.sections] == [
        "concepts",
        "topics",
        "howto",
        "references",
        "proposals",
        "sources",
    ]
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
    before = {
        p: p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }
    res = init_wiki(root, today=TODAY)
    assert res.created is False
    assert res.ref.name == "acme"
    assert res.ref.path == root.resolve()
    assert registry.resolve("acme", tmp_path).path == root.resolve()
    assert _git(root, "rev-parse", "HEAD") == head
    after = {
        p: p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }
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
    # the body template has ## lines
    prose = re.sub(r"^```.*?^```", "", text, flags=re.MULTILINE | re.DOTALL)
    h2 = re.findall(r"^## (.+)$", prose, re.MULTILINE)
    titles = [re.sub(r"^\d+\.\s*", "", h).strip() for h in h2]
    assert titles == AGENTS_HEADINGS
    assert "{{" not in text
    assert "}}" not in text
    # "about 25k" of content; Prettier's table-column padding is not content
    assert len(re.sub(r" {2,}", " ", text)) < 26_000


def test_agents_md_mentions_sections_and_caps(tmp_path):
    root = tmp_path / "w"
    init_wiki(root, name="acme", description="Acme platform knowledge.", today=TODAY)
    text = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "400" in text
    assert "conventions" in text
    assert "acme" in text
    assert "Acme platform knowledge." in text
    for s in load_config(root).sections:
        assert f"`{s.name}/`" in text
    for cap in ("100", "500", "15", "80,000"):
        assert cap in text


def test_init_without_git_identity_fails_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(gitops, "has_identity", lambda root: False)
    root = tmp_path / "w"
    root.mkdir()
    with pytest.raises(GitError, match=r"configure git user\.name and user\.email"):
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
    with pytest.raises(GitError, match=r"unknown-subcommand|not a git command"):
        gitops.git(root, "unknown-subcommand")


def test_cli_init_messages(tmp_path):
    root = tmp_path / "w"
    r = CliRunner().invoke(
        app,
        [
            "init",
            str(root),
            "--name",
            "acme",
            "--profile",
            "general",
            "--description",
            "Acme.",
        ],
    )
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
    assert r.exit_code == 2
    assert "not empty and not a wiki" in r.stderr


def _agents(tmp_path):
    root = tmp_path / "w"
    init_wiki(root, today=TODAY)
    # Prettier wraps the template's prose, so phrases may span lines
    return " ".join((root / "AGENTS.md").read_text(encoding="utf-8").split())


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

    monkeypatch.setattr(
        init_mod,
        "lint",
        lambda *a, **k: LintReport(
            [Finding("error", "broken-link", "wiki/x.md", "boom")]
        ),
    )
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


def _fail_lint(monkeypatch):
    from llm_wiki import init as init_mod
    from llm_wiki.lint import Finding, LintReport

    monkeypatch.setattr(
        init_mod,
        "lint",
        lambda *a, **k: LintReport([Finding("error", "broken-link", "x", "boom")]),
    )


def test_failed_init_leaves_no_intermediate_dirs(tmp_path, monkeypatch):
    _fail_lint(monkeypatch)
    with pytest.raises(WikiError, match="broken-link"):
        init_wiki(tmp_path / "a" / "b" / "c", today=TODAY)
    assert not (tmp_path / "a").exists()


def test_failed_init_keeps_preexisting_ancestors(tmp_path, monkeypatch):
    _fail_lint(monkeypatch)
    (tmp_path / "a").mkdir()
    with pytest.raises(WikiError, match="broken-link"):
        init_wiki(tmp_path / "a" / "b" / "c", today=TODAY)
    assert (tmp_path / "a").is_dir()
    assert not (tmp_path / "a" / "b").exists()


def test_init_empty_clone(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    gitops.init_repo(root)
    result = init_wiki(root, today=TODAY)
    assert result.created
    assert result.host_repo is None
    assert not (root / ".git" / ".git").exists()
    assert len(gitops.git(root, "rev-list", "--all").split()) == 1
    assert gitops.git(root, "status", "--porcelain").strip() == ""


def test_failed_init_on_empty_clone_keeps_git(tmp_path, monkeypatch):
    _fail_lint(monkeypatch)
    root = tmp_path / "w"
    root.mkdir()
    gitops.init_repo(root)
    with pytest.raises(WikiError, match="broken-link"):
        init_wiki(root, today=TODAY)
    assert [p.name for p in root.iterdir()] == [".git"]


def test_init_dir_with_git_and_other_files_refused(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    gitops.init_repo(root)
    (root / "x.txt").write_text("x", encoding="utf-8")
    with pytest.raises(WikiError, match="not empty"):
        init_wiki(root, today=TODAY)


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
    with pytest.raises(
        WikiError,
        match=(
            "wiki name 'demo' is already registered at "
            f"{re.escape(str(a.resolve()))}; pass --name"
        ),
    ):
        init_wiki(b, name="demo", today=TODAY)
    assert not b.exists()
    assert registry.registry_file().read_text(encoding="utf-8") == before
    assert registry.resolve("demo", tmp_path).path == a.resolve()


def test_cli_init_name_collision_exit_2(tmp_path):
    runner = CliRunner()
    assert (
        runner.invoke(app, ["init", str(tmp_path / "a"), "--name", "demo"]).exit_code
        == 0
    )
    res = runner.invoke(app, ["init", str(tmp_path / "b"), "--name", "demo"])
    assert res.exit_code == 2
    assert "error: wiki name 'demo' is already registered at" in res.stderr
    assert not (tmp_path / "b").exists()


def test_init_existing_wiki_refuses_name_registered_elsewhere(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    init_wiki(a, name="demo", today=TODAY)
    saved = registry.registry_file().read_text(encoding="utf-8")
    registry.registry_file().unlink()
    init_wiki(
        b, name="demo", today=TODAY
    )  # a second wiki 'demo', created while unregistered
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


def _host_repo(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    gitops.init_repo(host)
    (host / "host.txt").write_text("h", encoding="utf-8")
    gitops.git(host, "add", "host.txt")
    return host


def test_init_inside_existing_repo_skips_git(tmp_path):
    host = _host_repo(tmp_path)
    wiki = host / "docs" / "wiki"
    result = init_wiki(wiki, today=TODAY)
    assert result.created
    assert result.host_repo == host.resolve()
    assert not (wiki / ".git").exists()
    assert gitops.git(host, "rev-list", "--all").split() == []
    assert gitops.git(host, "diff", "--cached", "--name-only").split() == ["host.txt"]
    assert lint(wiki, load_config(wiki), today=TODAY).errors == 0


def test_standalone_init_has_no_host_repo_and_one_commit(tmp_path):
    root = tmp_path / "w"
    result = init_wiki(root, today=TODAY)
    assert result.host_repo is None
    assert len(gitops.git(root, "rev-list", "--all").split()) == 1


def test_init_inside_repo_without_identity_succeeds(tmp_path, monkeypatch):
    host = _host_repo(tmp_path)
    monkeypatch.setattr(gitops, "has_identity", lambda root: False)
    result = init_wiki(host / "wiki", today=TODAY)
    assert result.created
    assert result.host_repo == host.resolve()


def test_enclosing_repo_detection(tmp_path):
    assert gitops.enclosing_repo(tmp_path / "plain" / "x") is None
    host = _host_repo(tmp_path)
    assert gitops.enclosing_repo(host / "a" / "b" / "c") == host.resolve()


def test_cli_init_host_repo_message(tmp_path):
    host = _host_repo(tmp_path)
    wiki = host / "wiki"
    r = CliRunner().invoke(app, ["init", str(wiki), "--name", "acme"])
    assert r.exit_code == 0, r.output
    assert r.stdout.strip() == (
        f"created wiki acme at {wiki.resolve()} "
        f"inside existing git repository {host.resolve()} "
        "(not committed — review and commit it there)"
    )


def test_init_inside_git_dir_is_refused(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    subprocess.run(["git", "init", "-q", str(host)], check=True)
    with pytest.raises(WikiError, match=r"inside a \.git directory"):
        init_wiki(host / ".git" / "w")
    assert not (host / ".git" / "w").exists()


def test_agents_md_annotated_screenshot_row(tmp_path):
    text = _agents(tmp_path)
    assert (
        "| Annotated screenshot (numbered markers) | The numbered steps in marker "
        "order, each with the UI element and action |"
    ) in text


def test_init_empty_clone_on_master_commits_to_main(tmp_path):
    # m3: an empty clone's unborn branch (here master) becomes main, which
    # ingest requires
    root = tmp_path / "w"
    root.mkdir()
    gitops.git(root, "init", "-q", "-b", "master")
    init_wiki(root, today=TODAY)
    assert gitops.current_branch(root) == "main"
    assert gitops.git(root, "branch", "--format=%(refname:short)").split() == ["main"]


def test_gitignore_template_ignores_reports():
    from importlib import resources

    text = (resources.files("llm_wiki") / "templates" / "gitignore").read_text(
        encoding="utf-8"
    )
    assert ".wiki/reports/" in text.split()


def test_new_wiki_ignores_os_metadata_files(tmp_path):
    # Finder and Explorer drop these into any folder they open; a new wiki must
    # still pass the ingest preflight
    from llm_wiki.gate import preflight_git

    root = tmp_path / "w"
    init_wiki(root, today=TODAY)
    for rel in (".DS_Store", "wiki/.DS_Store", "raw/Thumbs.db", "wiki/desktop.ini"):
        (root / rel).write_bytes(b"\0")
    assert preflight_git(root) == "main"
