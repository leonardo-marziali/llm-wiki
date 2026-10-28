from importlib import resources
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from llm_wiki import __version__
from llm_wiki.cli import app
from llm_wiki.errors import WikiError
from llm_wiki.init import init_wiki
from llm_wiki.skill import (
    POINTER_BEGIN,
    POINTER_END,
    install_skill,
    render_pointer,
    render_skill,
    user_skills_dir,
    write_pointer,
)

SRC = Path(__file__).parent.parent / "src" / "llm_wiki"


def frontmatter(text: str) -> dict:
    _, fm, _ = text.split("---\n", 2)
    return yaml.safe_load(fm)


def test_description_within_budget():
    desc = frontmatter(render_skill())["description"]
    assert len(desc) <= 1024
    assert "wiki" in desc
    assert desc.startswith("Looks up")  # third person


def test_frontmatter_fields():
    fm = frontmatter(render_skill())
    assert fm["name"] == "wiki-lookup"
    assert fm["allowed-tools"] == (
        "Read Grep Glob Bash(wiki status *) Bash(wiki search *)"
    )
    assert fm["disallowed-tools"] == "Write Edit"
    assert fm["metadata"] == {"llm-wiki-version": __version__}


def test_skill_body_carries_the_answer_rules():
    body = render_skill()
    for needle in (
        "wiki status --all --format json",
        "index.md",
        "_index.md",
        "wiki search",
        "at most 5 pages",
        "[AS-IS]",
        "[TO-BE]",
        "STALE",
        "not in wiki",
        'wiki ingest --note "…"',
    ):
        assert needle in body, needle


def test_user_dir_honours_claude_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert user_skills_dir() == tmp_path / "cfg" / "skills"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert user_skills_dir() == tmp_path / ".claude" / "skills"


def test_install_idempotent(tmp_path):
    path, changed = install_skill(tmp_path)
    assert changed
    assert path == tmp_path / "wiki-lookup" / "SKILL.md"
    before = path.read_bytes()
    assert install_skill(tmp_path) == (path, False)
    assert path.read_bytes() == before


def test_install_upgrades_older_stamp(tmp_path):
    path = tmp_path / "wiki-lookup" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(render_skill().replace(__version__, "0.0.1"), encoding="utf-8")
    assert install_skill(tmp_path) == (path, True)
    assert path.read_text(encoding="utf-8") == render_skill()


def test_install_refuses_foreign_skill(tmp_path):
    # Review Focus 1
    path = tmp_path / "wiki-lookup" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: wiki-lookup\n---\nmine\n", encoding="utf-8")
    with pytest.raises(WikiError, match="--force"):
        install_skill(tmp_path)
    assert path.read_text(encoding="utf-8").endswith("mine\n")
    assert install_skill(tmp_path, force=True) == (path, True)


def test_pointer_at_most_15_lines():
    block = render_pointer("payments")
    assert len(block.splitlines()) <= 15
    assert block.startswith(POINTER_BEGIN)
    assert block.rstrip("\n").endswith(POINTER_END)
    assert "`payments`" in block


def test_pointer_replaces_only_marked_block(tmp_path):
    # Review Focus 3
    claude_md = tmp_path / "CLAUDE.md"
    path, changed = write_pointer(tmp_path, "old")
    assert changed
    assert path.read_text(encoding="utf-8") == render_pointer("old")
    head, tail = "# Repo\n\nKeep  this.\n\n", "\n\n## Mine\n\ntext\n"
    claude_md.write_text(
        head + render_pointer("old").rstrip("\n") + tail, encoding="utf-8"
    )
    assert write_pointer(tmp_path, "new") == (claude_md, True)
    text = claude_md.read_text(encoding="utf-8")
    assert text == head + render_pointer("new").rstrip("\n") + tail
    assert write_pointer(tmp_path, "new") == (claude_md, False)


def test_pointer_appended_to_existing_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Repo\n", encoding="utf-8")
    write_pointer(tmp_path, "w")
    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert text == "# Repo\n\n" + render_pointer("w")


def test_cli_skill_install_project(tmp_path):
    r = CliRunner().invoke(app, ["skill", "install", "--project", str(tmp_path)])
    assert r.exit_code == 0, r.output
    path = tmp_path / ".claude" / "skills" / "wiki-lookup" / "SKILL.md"
    assert f"wiki-lookup {__version__} installed at {path}" in r.output
    r = CliRunner().invoke(app, ["skill", "install", "--project", str(tmp_path)])
    assert f"wiki-lookup {__version__} already installed at {path}" in r.output


def test_cli_skill_install_user(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    r = CliRunner().invoke(app, ["skill", "install"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "skills" / "wiki-lookup" / "SKILL.md").is_file()


def test_pointer_requires_project(tmp_path, monkeypatch):
    # Review Focus 4
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    r = CliRunner().invoke(app, ["skill", "install", "--pointer"])
    assert r.exit_code == 2
    assert "--pointer needs --project DIR" in r.output
    assert not (tmp_path / "skills").exists()


def test_cli_pointer_names_the_resolved_wiki(tmp_path):
    root = tmp_path / "kb"
    init_wiki(root, name="kb")
    repo = tmp_path / "repo"
    repo.mkdir()
    r = CliRunner().invoke(
        app,
        ["skill", "install", "--project", str(repo), "--pointer", "--wiki", "kb"],
    )
    assert r.exit_code == 0, r.output
    assert "`kb`" in (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert f"pointer to wiki kb written to {repo / 'CLAUDE.md'}" in r.output


def test_package_data_complete():
    # Review Focus 5: every template and prompt is readable as package data
    pkg = resources.files("llm_wiki")
    files = [
        p.relative_to(SRC).as_posix()
        for d in ("templates", "prompts")
        for p in (SRC / d).rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    ]
    assert "templates/skill-wiki-lookup.md" in files
    assert "templates/claude-pointer.md" in files
    for rel in files:
        assert (pkg / rel).is_file(), rel
