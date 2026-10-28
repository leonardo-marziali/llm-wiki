"""Changed and deleted sources: ingested against the pages they already back."""

import hashlib
from datetime import date
from importlib import resources

import pytest
from conftest import RAW_REL, ok, page_text

from llm_wiki import gitops
from llm_wiki.config import LlmSettings, load_config
from llm_wiki.index import build_indexes
from llm_wiki.ingest import DELETE_CLAIM as PLAN_CLAIM
from llm_wiki.ingest import RunContext, deleted_plan
from llm_wiki.ingest_run import run_ingest
from llm_wiki.manifest import Manifest, SourceEntry
from llm_wiki.registry import WikiRef

TODAY = date(2026, 10, 28)
AGENTS = "# Acme: wiki rules\n\nFollow the rules.\n"
OLD_TEXT = "# Guide\n\nAlways branch from main.\n"  # make_wiki's raw source
NEW_TEXT = "# Guide\n\nAlways rebase before merging.\n"
GIT_FLOW = "wiki/conventions/git-flow.md"
SRC = "wiki/sources/src-docs-guide.md"
SRC_ID = "sources.src-docs-guide"
OPEN_QUESTION = f"{RAW_REL} was deleted; are its facts still valid?"
DELETE_CLAIM = "source deleted: mark its claims unverified per AGENTS.md"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def src_page(digest, status="as-is"):
    if status == "deprecated":
        body = (
            "# Guide\n\n**TL;DR** The branching guide (deleted).\n\n"
            "> [!unverified]\n> The guide says to branch from main.\n"
        )
        return page_text(
            SRC_ID, "Guide", body, type_="source-summary", status="deprecated"
        )
    body = (
        "# Guide\n\n**TL;DR** The branching guide.\n\n"
        f"The guide says how to branch.[^1]\n\n[^1]: {RAW_REL} — §1\n"
    )
    return page_text(
        SRC_ID, "Guide", body, type_="source-summary", sources=[(RAW_REL, digest)]
    )


def git_flow(digest, text="We rebase before merging every change."):
    body = f"# Git flow\n\n**TL;DR** Rebase first.\n\n{text}[^1]\n\n[^1]: {RAW_REL}\n"
    return page_text(
        "conventions.git-flow", "Git flow", body, sources=[(RAW_REL, digest)]
    )


def deprecated_git_flow():
    body = (
        "# Git flow\n\n**TL;DR** Branch from main (unverified).\n\n"
        "> [!unverified]\n> We branch from main for every change.\n"
    )
    return page_text("conventions.git-flow", "Git flow", body, status="deprecated")


@pytest.fixture
def wiki(tmp_path, make_wiki):
    """A committed wiki on main whose guide backs git-flow and its src page."""
    root = make_wiki(tmp_path)
    (root / "AGENTS.md").write_text(AGENTS, encoding="utf-8")
    gitignore = resources.files("llm_wiki") / "templates" / "gitignore"
    (root / ".gitignore").write_text(gitignore.read_text("utf-8"), encoding="utf-8")
    (root / "inbox").mkdir()
    (root / "inbox" / ".gitkeep").write_text("", encoding="utf-8")
    old = sha(OLD_TEXT)
    (root / SRC).write_text(src_page(old), encoding="utf-8")
    converted = root / ".wiki" / "converted" / f"{old}.md"
    converted.parent.mkdir(parents=True)
    converted.write_text(OLD_TEXT, encoding="utf-8")
    m = Manifest.load(root)
    m.record(
        m.entries[RAW_REL].model_copy(
            update={
                "last_ingested": date(2026, 1, 1),
                "converter": "markdown",
                "converted": [converted.relative_to(root).as_posix()],
                "pages_touched": ["conventions.git-flow", "glossary", SRC_ID],
            }
        )
    )
    m.save(root)
    build_indexes(root, load_config(root))
    gitops.init_repo(root)
    gitops.commit_all(root, "init")
    return root


def ingest(root, **kw):
    kw.setdefault("yes", True)
    kw.setdefault("interactive", False)
    return run_ingest(WikiRef("Acme", root, "path"), today=TODAY, **kw)


def claim(text="Always rebase before merging.", source=RAW_REL):
    return {"text": text, "source": source, "locator": "§1", "confidence": "high"}


def update(id_, type_, title):
    return {
        "id": id_,
        "action": "update",
        "type": type_,
        "status": "as-is",
        "adr_status": None,
        "title": title,
        "summary": f"{title} summary.",
        "claims": [claim()],
        "reason": "The guide changed.",
    }


def plan(*pages):
    return {
        "source_title": "Guide",
        "source_summary": "The branching guide.",
        "pages": list(pages),
        "contradictions": [],
        "open_questions": [],
    }


def planned(structured, cost=0.05):
    return {"stdout": ok(structured=structured, cost=cost)}


def edit(writes, cost=0.1):
    return {"stdout": ok(cost=cost), "write": writes}


def failing(result="boom", cost=0.3):
    return {"stdout": {**ok(cost=cost), "is_error": True, "result": result}, "exit": 1}


def is_edit(call):
    return "--permission-mode" in call["argv"]


def changed_edit():
    new = sha(NEW_TEXT)
    return edit({GIT_FLOW: git_flow(new), SRC: src_page(new)})


def deleted_edit():
    return edit(
        {
            GIT_FLOW: deprecated_git_flow(),
            SRC: src_page("", status="deprecated"),
            "wiki/open-questions.md": f"# Open questions\n\n- {OPEN_QUESTION}\n",
        }
    )


def tracked(root, rel):
    return gitops.git(root, "ls-files", rel).strip() == rel


# --- changed sources ---


def test_changed_source_touches_only_pages_for(wiki, fake_claude):
    (wiki / RAW_REL).write_text(NEW_TEXT, encoding="utf-8")
    stray = planned(plan(update("overview", "overview", "Overview")))
    good = planned(plan(update("conventions.git-flow", "convention", "Git flow")))
    fake_claude.queue(stray, good, changed_edit())
    report = ingest(wiki)
    assert report.decision == "accept", report.summary
    assert [(o.rel, o.status) for o in report.outcomes] == [(RAW_REL, "ingested")]
    calls = fake_claude.calls()
    assert [is_edit(c) for c in calls] == [False, False, True]
    assert (
        "overview: a changed source may only update pages it already backs"
        in (calls[1]["stdin"])
    )
    (out,) = report.outcomes
    assert out.pages_updated == ["conventions.git-flow", SRC_ID]
    entry = Manifest.load(wiki).entries[RAW_REL]
    assert (entry.status, entry.sha256) == ("ingested", sha(NEW_TEXT))
    assert entry.supersedes_sha == sha(OLD_TEXT)
    # the old set plus this run's pages: glossary was not planned, yet it stays
    assert entry.pages_touched == ["conventions.git-flow", "glossary", SRC_ID]
    assert "rebase" in (wiki / GIT_FLOW).read_text(encoding="utf-8")


def test_changed_prompt_contains_old_new_diff(wiki, fake_claude):
    (wiki / RAW_REL).write_text(NEW_TEXT, encoding="utf-8")
    good = planned(plan(update("conventions.git-flow", "convention", "Git flow")))
    fake_claude.queue(good, changed_edit())
    report = ingest(wiki)
    assert report.decision == "accept", report.summary
    stdin = fake_claude.calls()[0]["stdin"]
    assert "- kind: changed" in stdin
    assert "## Changed source" in stdin
    for id_ in ("conventions.git-flow", "glossary", SRC_ID):
        assert f"\n- {id_}\n" in stdin
    assert "```diff" in stdin
    assert "-Always branch from main." in stdin
    assert "+Always rebase before merging." in stdin


def test_changed_prompt_without_old_conversion_omits_diff(wiki, fake_claude):
    (wiki / ".wiki" / "converted" / f"{sha(OLD_TEXT)}.md").unlink()
    gitops.commit_all(wiki, "drop the old conversion")
    (wiki / RAW_REL).write_text(NEW_TEXT, encoding="utf-8")
    good = planned(plan(update("conventions.git-flow", "convention", "Git flow")))
    fake_claude.queue(good, changed_edit())
    report = ingest(wiki)
    assert report.decision == "accept", report.summary
    stdin = fake_claude.calls()[0]["stdin"]
    assert "## Changed source" in stdin
    assert "```diff" not in stdin
    assert "-Always branch from main." not in stdin


# --- deleted sources ---


def test_deleted_source_marks_unverified_and_open_question(wiki, fake_claude):
    (wiki / RAW_REL).unlink()
    fake_claude.queue(deleted_edit())  # no plan call: only the edit
    report = ingest(wiki)
    assert report.decision == "accept", report.summary
    assert report.lint_errors == []
    (out,) = report.outcomes
    assert (out.rel, out.status) == (RAW_REL, "deleted")
    assert out.pages_updated == ["conventions.git-flow", SRC_ID]
    assert out.open_questions == 1
    (call,) = fake_claude.calls()
    assert is_edit(call)
    stdin = call["stdin"]
    assert OPEN_QUESTION in stdin
    assert DELETE_CLAIM in stdin
    for rel in (GIT_FLOW, SRC, "wiki/open-questions.md"):
        assert f"`{rel}`" in stdin
    assert "deprecated" in stdin
    entry = Manifest.load(wiki).entries[RAW_REL]
    assert entry.status == "deleted"
    assert not tracked(wiki, RAW_REL)
    assert gitops.changed_paths(wiki) == []
    assert "status: deprecated" in (wiki / GIT_FLOW).read_text(encoding="utf-8")
    assert "status: deprecated" in (wiki / SRC).read_text(encoding="utf-8")
    log = (wiki / "wiki" / "log.md").read_text(encoding="utf-8")
    assert f"## [2026-10-28] delete | {RAW_REL}\n" in log
    again = ingest(wiki)
    assert again.decision == "noop"


def test_deleted_source_edit_strays_reverted(wiki, fake_claude):
    (wiki / RAW_REL).unlink()
    agents = (wiki / "AGENTS.md").read_bytes()
    response = deleted_edit()
    response["write"]["AGENTS.md"] = "# Hijacked\n"
    response["write"][RAW_REL] = OLD_TEXT  # recreating the deleted file is a stray
    fake_claude.queue(response)
    report = ingest(wiki)
    assert report.decision == "accept", report.summary
    (out,) = report.outcomes
    assert out.status == "deleted"
    assert out.reverted == ["AGENTS.md", RAW_REL]
    assert (wiki / "AGENTS.md").read_bytes() == agents
    assert not (wiki / RAW_REL).exists()


@pytest.mark.parametrize("touched", [[], ["concepts.gone"]])
def test_deleted_source_without_pages_no_llm_call(wiki, fake_claude, touched):
    rel = "raw/docs/old.md"
    path = wiki / rel
    path.write_text("# Old\n", encoding="utf-8")
    m = Manifest.load(wiki)
    m.record(
        SourceEntry(
            path=rel,
            sha256=sha("# Old\n"),
            size=6,
            mime="text/markdown",
            category="docs",
            first_seen=date(2026, 1, 1),
            status="ingested",
            pages_touched=touched,  # none, or only pages whose files are gone
        )
    )
    m.save(wiki)
    gitops.commit_all(wiki, "add old")
    path.unlink()
    report = ingest(wiki)
    assert report.decision == "accept", report.summary
    assert [(o.rel, o.status) for o in report.outcomes] == [(rel, "deleted")]
    assert fake_claude.calls() == []
    assert Manifest.load(wiki).entries[rel].status == "deleted"
    assert not tracked(wiki, rel)
    log = (wiki / "wiki" / "log.md").read_text(encoding="utf-8")
    assert f"## [2026-10-28] delete | {rel}\n" in log


def test_deleted_source_edit_failure_stays_pending(wiki, fake_claude):
    (wiki / RAW_REL).unlink()
    before = (wiki / GIT_FLOW).read_bytes()
    fake_claude.queue(failing("boom", cost=0.2))
    first = ingest(wiki)
    assert first.decision == "accept", first.summary
    (out,) = first.outcomes
    assert (out.rel, out.status) == (RAW_REL, "failed")
    assert "boom" in out.reason
    assert out.cost_usd == pytest.approx(0.2)
    assert (wiki / GIT_FLOW).read_bytes() == before
    assert Manifest.load(wiki).entries[RAW_REL].status == "ingested"
    assert tracked(wiki, RAW_REL)  # the deletion is not committed
    assert gitops.changed_paths(wiki) == [RAW_REL]
    fake_claude.queue(deleted_edit())
    second = ingest(wiki)
    assert [(o.rel, o.status) for o in second.outcomes] == [(RAW_REL, "deleted")]
    assert Manifest.load(wiki).entries[RAW_REL].status == "deleted"
    assert not tracked(wiki, RAW_REL)


def test_deleted_source_lint_failure_after_repair_stays_pending(wiki, fake_claude):
    (wiki / RAW_REL).unlink()
    # the page keeps citing the deleted file: citation-missing, twice
    still_cites = edit({GIT_FLOW: git_flow(sha(OLD_TEXT))})
    fake_claude.queue(still_cites, still_cites)
    report = ingest(wiki)
    (out,) = report.outcomes
    assert out.status == "failed"
    assert "lint errors after the repair" in out.reason
    assert [is_edit(c) for c in fake_claude.calls()] == [True, True]
    assert "## Lint errors to fix" in fake_claude.calls()[1]["stdin"]
    assert Manifest.load(wiki).entries[RAW_REL].status == "ingested"
    assert tracked(wiki, RAW_REL)


# --- failed sources ---


def test_failed_source_retried_next_run(wiki, fake_claude):
    rel, text = "raw/a.md", "# A\n\nAlways squash.\n"
    (wiki / "inbox" / "a.md").write_text(text, encoding="utf-8")
    fake_claude.queue(failing("plan boom", cost=0.1))
    first = ingest(wiki)
    assert first.decision == "accept", first.summary
    assert [(o.rel, o.status) for o in first.outcomes] == [(rel, "failed")]
    assert Manifest.load(wiki).entries[rel].status == "failed"
    assert tracked(wiki, rel)
    digest = sha(text)
    body = (
        "# Squashing\n\n**TL;DR** Squash.\n\n"
        f"Always squash before merging.[^1]\n\n[^1]: {rel} — §1\n"
    )
    new_page = page_text(
        "concepts.squashing",
        "Squashing",
        body,
        type_="concept",
        sources=[(rel, digest)],
    )
    src_body = (
        f"# A\n\n**TL;DR** A doc.\n\nThe doc says to squash.[^1]\n\n[^1]: {rel} — §1\n"
    )
    new_src = page_text(
        "sources.src-a",
        "A",
        src_body,
        type_="source-summary",
        sources=[(rel, digest)],
    )
    create = {
        **update("concepts.squashing", "concept", "Squashing"),
        "action": "create",
        "claims": [claim("Always squash.", rel)],
    }
    fake_claude.queue(
        planned(plan(create)),
        edit(
            {"wiki/concepts/squashing.md": new_page, "wiki/sources/src-a.md": new_src}
        ),
    )
    second = ingest(wiki)
    assert second.decision == "accept", second.summary
    assert [(o.rel, o.status) for o in second.outcomes] == [(rel, "ingested")]
    assert "Always squash." in fake_claude.calls()[1]["stdin"]  # planned again
    entry = Manifest.load(wiki).entries[rel]
    assert entry.status == "ingested"
    assert entry.pages_touched == ["concepts.squashing", "sources.src-a"]


def test_deleted_source_usage_limit_stops_run_and_stays_pending(wiki, fake_claude):
    (wiki / RAW_REL).unlink()
    limit = {
        "stdout": {**ok(cost=0.0), "is_error": True, "result": "usage limit reached"},
        "exit": 1,
        "write": {GIT_FLOW: deprecated_git_flow()},
    }
    before = (wiki / GIT_FLOW).read_bytes()
    fake_claude.queue(limit)
    report = ingest(wiki)
    (out,) = report.outcomes
    assert out.status == "failed"
    assert "usage limit" in report.stopped
    assert (wiki / GIT_FLOW).read_bytes() == before
    assert Manifest.load(wiki).entries[RAW_REL].status == "ingested"
    assert tracked(wiki, RAW_REL)


def test_deleted_plan_deprecates_only_pages_left_without_sources(wiki):
    other = "raw/docs/other.md"
    (wiki / other).write_text("# Other\n", encoding="utf-8")
    shared = page_text(
        "concepts.shared",
        "Shared",
        "# Shared\n\n**TL;DR** Two sources.\n",
        type_="concept",
        sources=[(RAW_REL, sha(OLD_TEXT)), (other, sha("# Other\n"))],
    )
    (wiki / "wiki" / "concepts" / "shared.md").write_text(shared, encoding="utf-8")
    m = Manifest.load(wiki)
    touched = [*m.pages_for(RAW_REL), "concepts.shared", "concepts.gone"]
    m.record(m.entries[RAW_REL].model_copy(update={"pages_touched": touched}))
    ctx = RunContext(
        root=wiki,
        cfg=load_config(wiki),
        settings=LlmSettings(),
        manifest=m,
        today=TODAY,
        run_dir=wiki / ".wiki" / "runs" / "r1",
        n=1,
    )
    p = deleted_plan(ctx, RAW_REL)
    assert p is not None
    statuses = {pg.id: (pg.action, pg.status) for pg in p.pages}
    assert statuses == {
        "conventions.git-flow": ("update", "deprecated"),  # its only source
        "glossary": ("update", "as-is"),  # never cited it
        SRC_ID: ("update", "deprecated"),
        "concepts.shared": ("update", "as-is"),  # another source remains
    }  # concepts.gone has no file: skipped
    assert {c.text for pg in p.pages for c in pg.claims} == {DELETE_CLAIM}
    assert PLAN_CLAIM == DELETE_CLAIM  # the brief's exact wording
    assert p.open_questions == [OPEN_QUESTION]
