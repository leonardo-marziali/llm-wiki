"""``wiki consolidate``: split, merge, promote and relink jobs, then the
deterministic archive of retired pages no live page links to (Ruling 5).
"""

import hashlib
import re
from datetime import date
from importlib import resources

import pytest
from conftest import RAW_REL, ok, page_text
from typer.testing import CliRunner

from llm_wiki import archive, claude, gitops, maint_run
from llm_wiki.archive import archive_unlinked
from llm_wiki.cli import app
from llm_wiki.config import load_config
from llm_wiki.index import build_indexes
from llm_wiki.ingest_run import run_ingest
from llm_wiki.lint import lint
from llm_wiki.maint_run import run_maintenance
from llm_wiki.manifest import Manifest
from llm_wiki.registry import WikiRef
from llm_wiki.search import search

TODAY = date(2026, 10, 28)
AGENTS = "# Acme: wiki rules\n\nFollow the rules.\n"
GIT = "conventions.git-flow"
LOG_LINE = re.compile(r"^## \[\d{4}-\d{2}-\d{2}\] [a-z-]+ \| .+$")


# --- the wiki ---


@pytest.fixture
def wiki(tmp_path, make_wiki):
    """A committed wiki that is its own repo, on main."""
    root = make_wiki(tmp_path)
    (root / "AGENTS.md").write_text(AGENTS, encoding="utf-8")
    gitignore = resources.files("llm_wiki") / "templates" / "gitignore"
    (root / ".gitignore").write_text(gitignore.read_text("utf-8"), encoding="utf-8")
    gitops.init_repo(root)
    gitops.commit_all(root, "init")
    return root


def rel(id_):
    section, slug = id_.split(".")
    return f"wiki/{section}/{slug}.md"


def archived(id_, n=""):
    section, slug = id_.split(".")
    return f"wiki/{section}/_archive/{slug}{n}.md"


def raw_sha(root):
    return Manifest.load(root).entries[RAW_REL].sha256


def body(title, links="", notes=("§1",), extra=""):
    """TL;DR, one cited fact (followed by ``links``), then the footnotes."""
    marks = "".join(f"[^{n}]" for n in range(1, len(notes) + 1))
    defs = "".join(f"[^{n}]: {RAW_REL} — {loc}\n" for n, loc in enumerate(notes, 1))
    fact = f"{title} holds.{marks}{links}"
    return f"# {title}\n\n**TL;DR** {title}.\n\n{fact}\n\n{extra}{defs}"


def text(root, id_, title, page_body, *, type_="concept", **kw):
    kw.setdefault("sources", [(RAW_REL, raw_sha(root))])
    return page_text(id_, title, page_body, type_=type_, **kw)


def page(root, id_, title, links="", **kw):
    return text(root, id_, title, body(title, links), **kw)


def deprecated(root, id_, title, links=""):
    return page(root, id_, title, links, status="deprecated")


def link(id_, title="x", here="concepts"):
    section, slug = id_.split(".")
    target = f"{slug}.md" if section == here else f"../{section}/{slug}.md"
    return f" See [{title}]({target})."


def commit_pages(root, pages, *, linked=(), touched=None, msg="add pages"):
    """Write {id: text}, link ``linked`` from the overview, index, commit."""
    for id_, page_ in pages.items():
        (root / rel(id_)).write_text(page_, encoding="utf-8")
    if linked:
        overview = root / "wiki" / "overview.md"
        with overview.open("a", encoding="utf-8") as fh:
            for id_ in linked:
                section, slug = id_.split(".")
                fh.write(f"- [{slug}]({section}/{slug}.md)\n")
    if touched is not None:
        m = Manifest.load(root)
        entry = m.entries[RAW_REL]
        m.record(entry.model_copy(update={"pages_touched": sorted(touched)}))
        m.save(root)
    build_indexes(root, load_config(root))
    gitops.commit_all(root, msg)


# --- the fake claude ---


def mpage(id_, title, **kw):
    base = {
        "id": id_,
        "action": "update",
        "type": "concept",
        "status": "as-is",
        "adr_status": None,
        "title": title,
        "summary": f"{title} summary.",
        "superseded_by": None,
        "sources": [RAW_REL],
        "instructions": "Do the job.",
        "reason": "consolidate",
    }
    return {**base, **kw}


def mplan(*pages, summary="Consolidate", resolved=()):
    return {
        "summary": summary,
        "pages": list(pages),
        "resolved_questions": list(resolved),
        "open_questions": [],
    }


def planned(structured, cost=0.05):
    return {"stdout": ok(structured=structured, cost=cost)}


def edit(writes, cost=0.1):
    return {"stdout": ok(cost=cost), "write": writes}


def failing(result="boom", cost=0.3):
    return {
        "stdout": {**ok(cost=cost), "is_error": True, "result": result},
        "exit": 1,
    }


def run(root, **kw):
    kw.setdefault("yes", True)
    kw.setdefault("interactive", False)
    return run_maintenance(
        WikiRef("Acme", root, "path"), mode="consolidate", today=TODAY, **kw
    )


# --- git and the tree ---


def subjects(root):
    return gitops.git(root, "log", "--format=%s").splitlines()


def branches(root):
    return gitops.git(root, "branch", "--format=%(refname:short)").split()


def snapshot(root):
    """Every file of the wiki except .git and the (gitignored) run records."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and ".git" not in p.relative_to(root).parts[:1]
        and not p.relative_to(root).as_posix().startswith(".wiki/runs/")
    }


def archive_dirs(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("_archive"))


def read(root, rel_):
    return (root / rel_).read_text(encoding="utf-8")


def tracked(root, rel_):
    return gitops.git(root, "ls-files", "--", rel_).strip() == rel_


def log_tail(root, n):
    return read(root, "wiki/log.md").splitlines()[-n:]


def lint_codes(root):
    return {f.code for f in lint(root, load_config(root), today=TODAY).findings}


def touched(root):
    return Manifest.load(root).entries[RAW_REL].pages_touched


# --- archiving ---


def test_archive_only_unlinked(wiki, fake_claude, capsys):
    kept, gone, user = "concepts.kept", "concepts.gone", "concepts.user"
    commit_pages(
        wiki,
        {
            kept: deprecated(wiki, kept, "Kept thing"),
            gone: deprecated(wiki, gone, "Vanishing widget"),
            user: page(wiki, user, "User", link(kept)),
        },
        linked=[user],
        touched=[GIT, gone, kept, user],
    )
    ref = WikiRef("Acme", wiki, "path")
    assert [h.id for h in search([ref], "Vanishing widget")] == [gone]
    # the relink of kept fails, so a live page still links it: it stays
    fake_claude.queue(failing())
    report = run(wiki)
    out = capsys.readouterr().out
    assert report.decision == "accept", out
    assert [(o.job.kind, o.status) for o in report.outcomes] == [("relink", "failed")]
    assert report.archived == [gone]
    assert (wiki / rel(kept)).is_file()
    assert not (wiki / rel(gone)).exists()
    assert tracked(wiki, archived(gone))
    assert read(wiki, archived(gone)) == deprecated(wiki, gone, "Vanishing widget")
    assert gitops.current_branch(wiki) == "main"
    assert gitops.changed_paths(wiki) == []
    assert "archived: concepts.gone" in out
    # wiki lint: no broken link, search no longer finds it
    r = CliRunner().invoke(app, ["lint", "--wiki", str(wiki)])
    assert "broken-link" not in r.output
    assert gone not in {h.id for h in search([ref], "Vanishing widget")}
    assert "Vanishing" not in read(wiki, "wiki/concepts/_index.md")
    # the manifest and the log
    assert touched(wiki) == sorted([GIT, kept, user])
    tail = log_tail(wiki, 1)
    assert tail == ["## [2026-10-28] archive | concepts.gone"]
    assert LOG_LINE.match(tail[0])


def test_archive_only_run_costs_nothing(wiki, fake_claude, capsys):
    gone = "concepts.gone"
    commit_pages(wiki, {gone: deprecated(wiki, gone, "Gone")})
    report = run(wiki)
    out = capsys.readouterr().out
    assert report.decision == "accept", out
    assert fake_claude.calls() == []
    assert report.outcomes == []
    assert report.cost_usd == 0
    assert "archived: concepts.gone" in out
    assert "cost $0.00" in out
    assert tracked(wiki, archived(gone))
    assert subjects(wiki)[0] == "wiki consolidate: 0 jobs, 1 archived"
    assert branches(wiki) == ["main"]


def test_consolidate_noop(wiki, fake_claude, capsys):
    report = run(wiki)
    out = capsys.readouterr().out
    assert report.decision == "noop"
    assert report.branch is None
    assert "nothing to consolidate" in out
    assert fake_claude.calls() == []
    assert branches(wiki) == ["main"]


def test_archive_name_collision(wiki, fake_claude):
    gone = "concepts.gone"
    taken = wiki / "wiki" / "concepts" / "_archive"
    taken.mkdir()
    (taken / "gone.md").write_text("an older gone\n", encoding="utf-8")
    (taken / "gone-2.md").write_text("an even older gone\n", encoding="utf-8")
    commit_pages(wiki, {gone: deprecated(wiki, gone, "Gone")})
    report = run(wiki)
    assert report.archived == [gone]
    assert read(wiki, archived(gone)) == "an older gone\n"
    assert read(wiki, archived(gone, "-2")) == "an even older gone\n"
    assert read(wiki, archived(gone, "-3")) == deprecated(wiki, gone, "Gone")
    assert tracked(wiki, archived(gone, "-3"))


def test_archive_top_level_page(wiki):
    notes = wiki / "wiki" / "notes.md"
    notes.write_text(
        text(wiki, "notes", "Notes", body("Notes"), status="deprecated"),
        encoding="utf-8",
    )
    gitops.commit_all(wiki, "notes")
    manifest = Manifest.load(wiki)
    assert archive_unlinked(wiki, manifest) == ["notes"]
    assert tracked(wiki, "wiki/_archive/notes.md")
    assert not notes.exists()


def test_archive_keeps_overview_glossary_and_flagged_pages(wiki):
    # the rebuilt index.md always links the overview and the glossary; a flagged
    # page keeps its finding in sight
    glossary = wiki / "wiki" / "glossary.md"
    glossary.write_text(
        read(wiki, "wiki/glossary.md").replace("status: as-is", "status: deprecated"),
        encoding="utf-8",
    )
    secret = "password = Sup3rS3cret!x"  # a fake password value
    leaky = "concepts.leaky"
    commit_pages(
        wiki,
        {
            leaky: text(
                wiki,
                leaky,
                "Leaky",
                body("Leaky", extra=f"{secret}\n\n"),
                status="deprecated",
            )
        },
    )
    assert "secret-in-page" in lint_codes(wiki)
    assert archive.archive_candidates(wiki) == []


def test_archive_waits_for_a_file_that_stays(wiki):
    # R10: concepts.old has no live linker, but a retired page that stays (it is
    # still linked live) and an open question link it: archiving would break them
    old, stub, user = "concepts.old", "concepts.stub", "concepts.user"
    lone, lone_stub = "concepts.lone", "concepts.lone-stub"
    commit_pages(
        wiki,
        {
            old: deprecated(wiki, old, "Old thing"),
            stub: deprecated(wiki, stub, "Stub", link(old)),
            user: page(wiki, user, "User", link(stub)),
            # linked only by a retired page that is archived too: both go
            lone: deprecated(wiki, lone, "Lone"),
            lone_stub: deprecated(wiki, lone_stub, "Lone stub", link(lone)),
            "concepts.asked": deprecated(wiki, "concepts.asked", "Asked"),
        },
        linked=[user],
    )
    (wiki / "wiki/open-questions.md").write_text(
        "# Open questions\n\n- Is [Asked](concepts/asked.md) still true?\n",
        encoding="utf-8",
    )
    gitops.commit_all(wiki, "question")
    assert archive.archive_candidates(wiki) == [lone, lone_stub]


def test_relink_then_archive_same_run(wiki, fake_claude, capsys):
    old, user = "concepts.old", "concepts.user"
    commit_pages(
        wiki,
        {
            old: deprecated(wiki, old, "Old thing"),
            user: page(wiki, user, "User", link(old, "Old")),
        },
        linked=[user],
    )
    plan = mplan(
        mpage(user, "User", instructions="Remove the link to the deprecated page."),
        summary="Unlink the old page",
    )
    fake_claude.queue(planned(plan), edit({rel(user): page(wiki, user, "User")}))
    report = run(wiki)
    out = capsys.readouterr().out
    assert report.decision == "accept", out
    [o] = report.outcomes
    assert (o.job.kind, o.status) == ("relink", "done"), o.reason
    assert o.job.inputs == [old, user]
    assert report.archived == [old]
    assert tracked(wiki, archived(old))
    assert not (wiki / rel(old)).exists()
    assert "broken-link" not in lint_codes(wiki)
    assert log_tail(wiki, 2) == [
        "## [2026-10-28] consolidate | relink concepts.old",
        "## [2026-10-28] archive | concepts.old",
    ]
    assert subjects(wiki)[0] == ("wiki consolidate: 1 job, 1 archived (concepts.old)")
    assert len(fake_claude.calls()) == 2


# --- the jobs ---


def test_split_creates_hub_and_subpages(wiki, fake_claude):
    big = "concepts.big"
    filler = "".join(f"- item {n}\n" for n in range(400))
    big_text = text(
        wiki,
        big,
        "Big topic",
        body("Big topic", notes=("§1", "§2"), extra=filler + "\n"),
    )
    commit_pages(wiki, {big: big_text}, linked=[big])
    assert "page-too-long" in lint_codes(wiki)
    one, two = "concepts.alpha-rules", "concepts.beta-workflow"
    plan = mplan(
        mpage(big, "Big topic", type="hub", instructions="Make it a hub."),
        mpage(one, "Alpha rules", action="create", instructions="Move part one."),
        mpage(two, "Beta workflow", action="create", instructions="Move part two."),
        summary="Split the big page",
    )
    hub = page_text(
        big,
        "Big topic",
        "# Big topic\n\n**TL;DR** Big topic, in two parts.\n\n"
        "- [Alpha rules](alpha-rules.md)\n- [Beta workflow](beta-workflow.md)\n",
        type_="hub",
        sources=[(RAW_REL, raw_sha(wiki))],
    )
    sub_one = text(wiki, one, "Alpha rules", body("Alpha rules", link(two)))
    sub_two = text(
        wiki, two, "Beta workflow", body("Beta workflow", link(one), notes=("§2",))
    )
    fake_claude.queue(
        planned(plan), edit({rel(big): hub, rel(one): sub_one, rel(two): sub_two})
    )
    report = run(wiki)
    [o] = report.outcomes
    assert (o.job.kind, o.status) == ("split", "done"), o.reason
    assert report.decision == "accept", report.summary
    assert o.pages_created == [one, two]
    assert o.pages_updated == [big]
    cfg = load_config(wiki)
    found = lint(wiki, cfg, today=TODAY)
    assert "page-too-long" not in {f.code for f in found.findings}
    assert found.errors == 0, found.findings
    assert "type: hub" in read(wiki, rel(big))
    # every citation survives on a sub-page
    assert f"[^1]: {RAW_REL} — §1" in read(wiki, rel(one))
    assert f"[^1]: {RAW_REL} — §2" in read(wiki, rel(two))
    assert "- [Big topic](concepts/big.md)" in read(wiki, "wiki/index.md")
    assert report.archived == []


def test_merge_supersedes_and_archives(wiki, fake_claude):
    b1, b2, user = "concepts.b1", "concepts.b2", "concepts.user"
    commit_pages(
        wiki,
        {
            b1: page(wiki, b1, "Branching"),
            b2: text(wiki, b2, "Branching", body("Branching", notes=("§2",))),
            user: page(wiki, user, "User", link(b1) + link(b2)),
        },
        linked=[user],
        touched=[GIT, b1, b2, user],
    )
    plan = mplan(
        mpage(b1, "Branching", instructions="Keep both facts."),
        mpage(
            b2,
            "Branching",
            status="superseded",
            superseded_by=b1,
            instructions="Make it a stub.",
        ),
        mpage(user, "User", instructions="Retarget the b2 link to b1."),
        summary="Merge the branching pages",
    )
    survivor = text(wiki, b1, "Branching", body("Branching", notes=("§1", "§2")))
    stub = text(
        wiki,
        b2,
        "Branching",
        "# Branching\n\n**TL;DR** See [Branching](b1.md).\n",
        status="superseded",
        extra=f"superseded_by: {b1}\n",
    )
    retargeted = page(wiki, user, "User", link(b1))
    fake_claude.queue(
        planned(plan),
        edit({rel(b1): survivor, rel(b2): stub, rel(user): retargeted}),
    )
    report = run(wiki)
    [o] = report.outcomes
    assert (o.job.kind, o.status) == ("merge", "done"), o.reason
    assert report.decision == "accept", report.summary
    assert o.job.targets == [b1, b2]
    assert report.archived == [b2]
    assert read(wiki, archived(b2)) == stub
    assert "b2.md" not in read(wiki, rel(user))
    assert "broken-link" not in lint_codes(wiki)
    assert "duplicate-title" not in lint_codes(wiki)
    assert touched(wiki) == sorted([GIT, b1, user])
    assert log_tail(wiki, 2) == [
        "## [2026-10-28] consolidate | merge concepts.b1, concepts.b2",
        "## [2026-10-28] archive | concepts.b2",
    ]


def test_promote_resolves_question(wiki, fake_claude):
    sso, auth = "requirements.sso", "components.auth"
    question = "- [2026-10-01] SSO is live; promote? — [SSO login](requirements/sso.md)"
    before, after = "- Who owns [Git flow](conventions/git-flow.md)?", "- Last line."
    oq = f"# Open questions\n\n{before}\n{question}\n{after}\n"
    (wiki / "wiki/open-questions.md").write_text(oq, encoding="utf-8")
    to_be = text(
        wiki, sso, "SSO login", body("SSO login"), type_="requirement", status="to-be"
    )
    as_is = text(
        wiki,
        auth,
        "Auth",
        body("Auth", link(sso, "SSO", here="components")),
        type_="component",
    )
    commit_pages(wiki, {sso: to_be, auth: as_is}, linked=[auth])
    plan = mplan(
        mpage(
            sso,
            "SSO login",
            type="requirement",
            status="superseded",
            superseded_by=auth,
            instructions="Make it a stub pointing at the auth page.",
        ),
        mpage(auth, "Auth", type="component", instructions="State SSO as built."),
        summary="Promote SSO",
        resolved=[question],
    )
    stub = text(
        wiki,
        sso,
        "SSO login",
        "# SSO login\n\n**TL;DR** Built: see [Auth](../components/auth.md).\n\n"
        f"SSO login holds.[^1]\n\n[^1]: {RAW_REL} — §1\n",
        type_="requirement",
        status="superseded",
        extra=f"superseded_by: {auth}\n",
    )
    built = text(wiki, auth, "Auth", body("Auth"), type_="component")
    fake_claude.queue(
        planned(plan),
        edit(
            {
                rel(sso): stub,
                rel(auth): built,
                "wiki/open-questions.md": f"# Open questions\n\n{before}\n{after}\n",
            }
        ),
    )
    report = run(wiki)
    [o] = report.outcomes
    assert (o.job.kind, o.status) == ("promote", "done"), o.reason
    assert report.decision == "accept", report.summary
    assert read(wiki, "wiki/open-questions.md") == (
        f"# Open questions\n\n{before}\n{after}\n"
    )
    # superseded by the as-is page, then archived: no live page links it any more
    assert report.archived == [sso]
    assert f"superseded_by: {auth}" in read(wiki, archived(sso))
    assert "broken-link" not in lint_codes(wiki)


# --- rollback ---


def two_relinks(root):
    pages = {}
    for n in (1, 2):
        old, user = f"concepts.old{n}", f"concepts.user{n}"
        pages[old] = deprecated(root, old, f"Old {n}")
        pages[user] = page(root, user, f"User {n}", link(old))
    pages["concepts.gone"] = deprecated(root, "concepts.gone", "Gone")
    commit_pages(root, pages, linked=["concepts.user1", "concepts.user2"])


def test_consolidate_interrupt_rolls_back(wiki, fake_claude, monkeypatch):
    two_relinks(wiki)
    before = snapshot(wiki)
    head = gitops.git(wiki, "rev-parse", "HEAD")
    plan = mplan(mpage("concepts.user1", "User 1"))
    fake_claude.queue(
        planned(plan),
        edit({rel("concepts.user1"): page(wiki, "concepts.user1", "User 1")}),
    )
    real_run = claude.run

    def interrupt(call, **kw):
        if "- targets: concepts.old2" in call.prompt:
            raise KeyboardInterrupt
        return real_run(call, **kw)

    monkeypatch.setattr(claude, "run", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(wiki)
    assert len(fake_claude.calls()) == 2  # the first job ran and was committed
    assert branches(wiki) == ["main"]
    assert gitops.current_branch(wiki) == "main"
    assert gitops.git(wiki, "rev-parse", "HEAD") == head
    assert snapshot(wiki) == before
    assert archive_dirs(wiki) == []
    assert gitops.changed_paths(wiki) == []


def test_reject_after_archive_restores_tree(wiki, fake_claude):
    # Review Focus 4: the git mv into _archive/ is committed on the branch, so
    # switching back and deleting the branch undoes it
    two_relinks(wiki)
    before = snapshot(wiki)
    head = gitops.git(wiki, "rev-parse", "HEAD")
    seen = []

    def decide(summary):
        seen.append(summary)
        return "reject"

    fake_claude.queue(failing(), failing())
    report = run(wiki, yes=False, interactive=True, decide=decide)
    assert report.decision == "reject"
    assert report.archived == ["concepts.gone"]
    assert "archived: concepts.gone" in seen[0]
    assert branches(wiki) == ["main"]
    assert gitops.git(wiki, "rev-parse", "HEAD") == head
    assert snapshot(wiki) == before
    assert archive_dirs(wiki) == []
    assert gitops.changed_paths(wiki) == []


def test_interrupt_during_archive_move_rolls_back(wiki, fake_claude, monkeypatch):
    # a Ctrl-C between the git mv and its commit: the staged rename is undone
    commit_pages(wiki, {"concepts.gone": deprecated(wiki, "concepts.gone", "Gone")})
    before = snapshot(wiki)
    head = gitops.git(wiki, "rev-parse", "HEAD")

    def interrupted(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(archive.gitops, "stage_commit", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(wiki)
    assert branches(wiki) == ["main"]
    assert gitops.git(wiki, "rev-parse", "HEAD") == head
    assert snapshot(wiki) == before
    assert archive_dirs(wiki) == []
    assert gitops.changed_paths(wiki) == []


def test_archive_in_host_repo(tmp_path, make_wiki, fake_claude):
    # the wiki is a folder of a host repo: git mv runs on the host's paths
    root = make_wiki(tmp_path)
    (root / "AGENTS.md").write_text(AGENTS, encoding="utf-8")
    gitops.init_repo(tmp_path)
    gitops.switch(tmp_path, "dev", create=True)
    commit_pages(root, {"concepts.gone": deprecated(root, "concepts.gone", "Gone")})
    report = run(root)
    assert report.decision == "accept", report.summary
    assert report.archived == ["concepts.gone"]
    assert tracked(tmp_path, "w/" + archived("concepts.gone"))
    assert gitops.current_branch(tmp_path) == "dev"


# --- ingest after an archive ---

OLD_TEXT = "# Guide\n\nAlways branch from main.\n"  # make_wiki's raw source
NEW_TEXT = "# Guide\n\nAlways rebase before merging.\n"
SRC_ID = "sources.src-docs-guide-md"


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def src_page(digest):
    b = (
        "# Guide\n\n**TL;DR** The branching guide.\n\n"
        f"The guide says how to branch.[^1]\n\n[^1]: {RAW_REL} — §1\n"
    )
    return page_text(
        SRC_ID, "Guide", b, type_="source-summary", sources=[(RAW_REL, digest)]
    )


def git_flow(digest):
    b = (
        "# Git flow\n\n**TL;DR** Rebase first.\n\n"
        f"We rebase before merging every change.[^1]\n\n[^1]: {RAW_REL}\n"
    )
    return page_text(GIT, "Git flow", b, sources=[(RAW_REL, digest)])


def ingest_plan(*ids):
    claim = {
        "text": "Always rebase before merging.",
        "source": RAW_REL,
        "locator": "§1",
        "confidence": "high",
    }
    return {
        "source_title": "Guide",
        "source_summary": "The branching guide.",
        "pages": [
            {
                "id": i,
                "action": "update",
                "type": "convention" if i == GIT else "concept",
                "status": "as-is",
                "adr_status": None,
                "title": "Git flow" if i == GIT else "Gone",
                "summary": "Summary.",
                "claims": [claim],
                "reason": "The guide changed.",
            }
            for i in ids
        ],
        "contradictions": [],
        "open_questions": [],
    }


def test_changed_source_after_archive(wiki, fake_claude):
    gone = "concepts.gone"
    old = sha(OLD_TEXT)
    (wiki / "inbox").mkdir()
    (wiki / "inbox" / ".gitkeep").write_text("", encoding="utf-8")
    (wiki / "wiki/sources/src-docs-guide-md.md").write_text(
        src_page(old), encoding="utf-8"
    )
    converted = wiki / ".wiki" / "converted" / f"{old}.md"
    converted.parent.mkdir(parents=True)
    converted.write_text(OLD_TEXT, encoding="utf-8")
    m = Manifest.load(wiki)
    m.record(
        m.entries[RAW_REL].model_copy(
            update={
                "last_ingested": date(2026, 1, 1),
                "converter": "markdown",
                "converted": [converted.relative_to(wiki).as_posix()],
            }
        )
    )
    m.save(wiki)
    commit_pages(
        wiki,
        {gone: deprecated(wiki, gone, "Gone")},
        touched=[GIT, gone, "glossary", SRC_ID],
    )
    assert run(wiki).archived == [gone]
    assert touched(wiki) == sorted([GIT, "glossary", SRC_ID])
    # an older manifest may still list the archived id: ingest must cope
    m = Manifest.load(wiki)
    entry = m.entries[RAW_REL]
    m.record(entry.model_copy(update={"pages_touched": [*entry.pages_touched, gone]}))
    m.save(wiki)
    gitops.commit_all(wiki, "an old manifest")

    (wiki / RAW_REL).write_text(NEW_TEXT, encoding="utf-8")
    new = sha(NEW_TEXT)
    fake_claude.queue(
        planned(ingest_plan(GIT, gone)),  # the archived page: rejected, retried
        planned(ingest_plan(GIT)),
        edit(
            {
                rel(GIT): git_flow(new),
                "wiki/sources/src-docs-guide-md.md": src_page(new),
            }
        ),
    )
    report = run_ingest(
        WikiRef("Acme", wiki, "path"), yes=True, interactive=False, today=TODAY
    )
    assert report.decision == "accept", report.summary
    assert [o.status for o in report.outcomes] == ["ingested"]
    assert (
        "concepts.gone: action is update but the page does not exist"
        in (fake_claude.calls()[1]["stdin"])
    )
    assert not (wiki / rel(gone)).exists()
    assert tracked(wiki, archived(gone))
    assert "rebase" in read(wiki, rel(GIT))


# --- the CLI ---


def invoke(root, *args):
    return CliRunner().invoke(app, ["consolidate", "--wiki", str(root), *args])


def test_cli_consolidate_exit_codes(wiki, fake_claude, monkeypatch):
    # no-op: 0
    r = invoke(wiki, "--yes")
    assert r.exit_code == 0, r.output
    assert "nothing to consolidate" in r.output
    # no terminal and no --yes: a WikiError, 2
    old, user = "concepts.old", "concepts.user"
    commit_pages(
        wiki,
        {
            old: deprecated(wiki, old, "Old thing"),
            user: page(wiki, user, "User", link(old)),
        },
        linked=[user],
    )
    r = invoke(wiki)
    assert r.exit_code == 2
    assert "the gate needs a terminal; pass --yes" in r.output
    # --yes refused because lint fails: 1, and the tree is as before
    before = snapshot(wiki)
    fake_claude.queue(
        planned(mplan(mpage(user, "User"))),
        edit(
            {
                rel(user): page(wiki, user, "User"),
                "wiki/open-questions.md": "# Open questions\n\n- [x](nope.md)\n",
            }
        ),
    )
    r = invoke(wiki, "--yes")
    assert r.exit_code == 1, r.output
    assert "rejected: lint found 1 errors" in r.output
    assert snapshot(wiki) == before
    assert branches(wiki) == ["main"]
    # accept: 0
    fake_claude.queue(
        planned(mplan(mpage(user, "User"))),
        edit({rel(user): page(wiki, user, "User")}),
    )
    r = invoke(wiki, "--yes")
    assert r.exit_code == 0, r.output
    assert "accepted: wiki/consolidate-20" in r.output
    assert "archived: concepts.old" in r.output
    # not on main: 2
    gitops.switch(wiki, "feature", create=True)
    r = invoke(wiki, "--yes")
    assert r.exit_code == 2
    assert "consolidate runs on main" in r.output


def test_cli_consolidate_interrupt_exit_130(wiki, monkeypatch):
    def interrupted(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(maint_run, "run_maintenance", interrupted)
    r = invoke(wiki, "--yes")
    assert r.exit_code == 130
    assert "interrupted: the consolidate was rolled back" in r.output
