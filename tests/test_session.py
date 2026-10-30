import json
from datetime import date
from importlib import resources

import pytest
from conftest import ok, page_text

from llm_wiki import gitops
from llm_wiki.config import LlmSettings, load_config
from llm_wiki.session import OPEN_QUESTIONS, EditSession, SessionFailed

TODAY = date(2026, 10, 28)
PAGE = "wiki/concepts/branching.md"
OQ_TEXT = (
    "# Open questions\n\n"
    "- [2026-01-01] Is q1 still valid?\n"
    "- [2026-01-02] Is q2 still valid?\n"
)
Q1 = "- [2026-01-01] Is q1 still valid?"


@pytest.fixture
def wiki(tmp_path, make_wiki):
    root = make_wiki(tmp_path)
    (root / OPEN_QUESTIONS).write_text(OQ_TEXT, encoding="utf-8")
    (root / "AGENTS.md").write_text("# Acme: wiki rules\n", encoding="utf-8")
    gitignore = resources.files("llm_wiki") / "templates" / "gitignore"
    (root / ".gitignore").write_text(gitignore.read_text("utf-8"), encoding="utf-8")
    gitops.init_repo(root)
    gitops.commit_all(root, "init")
    return root


def session(root, name="1-job"):
    return EditSession(
        root,
        load_config(root),
        LlmSettings(),
        TODAY,
        root / ".wiki" / "runs" / "r1",
        name,
    )


def bad_page():
    # a concept page without sources: a lint error
    return page_text(
        "concepts.branching",
        "Branching",
        "# Branching\n\n**TL;DR** Rebase first.\n",
        type_="concept",
    )


def edit(writes, cost=0.1):
    return {"stdout": ok(cost=cost), "write": writes}


def test_session_reverts_strays_and_lints_changed(wiki, fake_claude):
    s = session(wiki)
    s.allowed.add(PAGE)
    overview = (wiki / "wiki" / "overview.md").read_text(encoding="utf-8")
    fake_claude.queue(edit({PAGE: bad_page(), "wiki/overview.md": "stray\n"}))
    s.edit("do it", "", "edit")
    findings = s.check()
    assert s.reverted == ["wiki/overview.md"]
    assert (wiki / "wiki" / "overview.md").read_text(encoding="utf-8") == overview
    assert findings
    assert {f.path for f in findings} == {PAGE}
    assert all(f.severity == "error" for f in findings)
    assert s.changed() == [PAGE]
    assert (wiki / ".wiki" / "runs" / "r1" / "1-job.edit.json").is_file()
    assert s.cost_usd == pytest.approx(0.1)
    s.undo()
    assert not (wiki / PAGE).exists()
    assert s.changed() == []


def test_session_repair_then_fail(wiki, fake_claude):
    s = session(wiki, name="2-job")
    s.allowed.add(PAGE)
    fake_claude.queue(edit({PAGE: bad_page()}), edit({PAGE: bad_page()}, cost=0.2))
    prompts = []

    def prompt(found):
        prompts.append(found)
        return "fix" if found else "write"

    with pytest.raises(SessionFailed) as exc:
        s.apply(prompt, "-p1")
    assert str(exc.value).startswith("lint errors after the repair: ")
    assert PAGE in str(exc.value)
    assert prompts[0] is None
    assert prompts[1]
    assert {f.path for f in prompts[1]} == {PAGE}
    runs = wiki / ".wiki" / "runs" / "r1"
    assert (runs / "2-job-p1.edit.json").is_file()
    assert (runs / "2-job-p1.repair.json").is_file()
    found = json.loads((runs / "2-job-p1.repair.findings.json").read_text())
    assert found == [
        {"path": f.path, "code": f.code, "message": f.message} for f in prompts[1]
    ]
    assert s.cost_usd == pytest.approx(0.3)


def test_session_missing_page_and_lint_hooks(wiki, fake_claude):
    s = session(wiki)
    s.allowed.add(PAGE)
    fake_claude.queue(edit({}))
    s.edit("do it", "", "edit")
    seen = []

    def pages(changed):
        seen.append(changed)
        return ["wiki/conventions/git-flow.md"]

    findings = s.check(pages_to_lint=pages, extra=list)
    assert seen == [[]]
    assert [(f.code, f.path) for f in findings] == [("missing-page", PAGE)]


def test_session_open_questions_resolved_lines_removable(wiki, fake_claude):
    s = session(wiki)
    s.oq_removable = {Q1}
    resolved = (
        "# Open questions\n\n"
        "- [2026-01-02] Is q2 still valid?\n"
        "- [2026-10-28] Is q3 still valid?\n"
    )
    fake_claude.queue(edit({OPEN_QUESTIONS: resolved}))
    s.edit("resolve q1", "", "edit")
    assert s.check() == []
    assert (wiki / OPEN_QUESTIONS).read_text(encoding="utf-8") == resolved
    # q2 is not listed: removing it puts the earlier text back
    fake_claude.queue(
        edit(
            {OPEN_QUESTIONS: "# Open questions\n\n- [2026-10-28] Is q3 still valid?\n"}
        )
    )
    s.edit("drop q2", "", "repair")
    findings = s.check()
    assert [(f.code, f.path) for f in findings] == [
        ("open-questions-append-only", OPEN_QUESTIONS)
    ]
    assert (wiki / OPEN_QUESTIONS).read_text(encoding="utf-8") == resolved


def test_session_open_questions_append_only_by_default(wiki, fake_claude):
    s = session(wiki)
    fake_claude.queue(edit({OPEN_QUESTIONS: OQ_TEXT.replace(Q1 + "\n", "")}))
    s.edit("resolve q1", "", "edit")
    findings = s.check()
    assert [f.code for f in findings] == ["open-questions-append-only"]
    assert findings[0].message == (
        "may only grow: append entries, never edit or remove existing ones; "
        "the earlier text was put back"
    )
    assert (wiki / OPEN_QUESTIONS).read_text(encoding="utf-8") == OQ_TEXT


def test_deny_rules_relative_with_spaced_root(tmp_path, make_wiki, fake_claude):
    root = make_wiki(tmp_path / "my wiki (x)")
    (root / "AGENTS.md").write_text("# Acme: wiki rules\n", encoding="utf-8")
    gitops.init_repo(root)
    gitops.commit_all(root, "init")
    s = session(root)
    s.deny.add("wiki/concepts/flagged.md")
    fake_claude.queue(edit({}))
    s.edit("do it", "", "edit")
    argv = fake_claude.calls()[0]["argv"]
    i = argv.index("--disallowed-tools")
    assert argv[i + 1 :][:2] == [
        "Read(./.wiki/quarantine/**)",
        "Read(./wiki/concepts/flagged.md)",
    ]
    assert not any(str(tmp_path) in a for a in argv[i + 1 :])


def test_edit_session_denies_quarantine(wiki, fake_claude):
    s = session(wiki)
    assert s.deny == {".wiki/quarantine/"}
    fake_claude.queue(edit({}))
    s.edit("do it", "", "edit")
    argv = fake_claude.calls()[0]["argv"]
    assert "Read(./.wiki/quarantine/**)" in argv


def test_allowed_file_never_denied(wiki, fake_claude):
    s = session(wiki)
    s.allowed.add(PAGE)
    s.deny |= {PAGE, "wiki/concepts/other.md"}
    fake_claude.queue(edit({PAGE: bad_page()}))
    s.edit("do it", "", "edit")
    argv = fake_claude.calls()[0]["argv"]
    assert f"Read(./{PAGE})" not in argv
    assert "Read(./wiki/concepts/other.md)" in argv
    assert "Read(./.wiki/quarantine/**)" in argv
