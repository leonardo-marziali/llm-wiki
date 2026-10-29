import re
from datetime import date

import pytest
from conftest import RAW_REL, page_text

from llm_wiki.config import dump_config, load_config
from llm_wiki.index import build_indexes
from llm_wiki.ingest import page_rel
from llm_wiki.lint import CONSOLIDATE_HINT, Finding, LintReport, lint
from llm_wiki.maint import (
    FIX_CODES,
    HAND_HINT,
    HINTS,
    MAX_JOBS,
    PLAN_PROMPTS,
    Job,
    JobKind,
    Mode,
    build_maint_plan_prompt,
    consolidate_jobs,
    fix_jobs,
    flagged,
    hint,
    live_linkers,
    validate_maint_plan,
)
from llm_wiki.manifest import Manifest
from llm_wiki.plan import system_prompt, validate_plan
from llm_wiki.schemas import MAINT_SCHEMA, MaintPage, MaintPlan, Plan

TODAY = date(2026, 10, 28)
GIT = "conventions.git-flow"
GIT_REL = "wiki/conventions/git-flow.md"
OTHER_RAW = "raw/docs/other.md"
SECRET = "\npassword = Sup3rS3cret!x\n"


@pytest.fixture
def root(tmp_path, make_wiki):
    return make_wiki(tmp_path)


def sha(root) -> str:
    return Manifest.load(root).entries[RAW_REL].sha256


def add(root, id_, title, body=None, *, cited=True, type_="concept", **kw):
    """Write page ``id_``; by default it cites RAW_REL in sources and a footnote."""
    if body is None:
        body = (
            f"# {title}\n\n**TL;DR** {title}.\n\nShort fact.[^1]\n\n[^1]: {RAW_REL}\n"
        )
    if cited:
        kw.setdefault("sources", [(RAW_REL, sha(root))])
    path = root / page_rel(id_)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page_text(id_, title, body, type_=type_, **kw), encoding="utf-8")
    return path


def edit(root, rel, old, new):
    p = root / rel
    text = p.read_text(encoding="utf-8")
    assert old in text
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


def reindex(root):
    build_indexes(root, load_config(root))


def report(root, cfg=None) -> LintReport:
    return lint(root, cfg or load_config(root), today=TODAY)


def set_caps(root, **caps):
    cfg = load_config(root).model_copy(update={"caps": caps})
    (root / "wiki.toml").write_text(dump_config(cfg), encoding="utf-8")
    return load_config(root)


def ids_in(jobs: list[Job]) -> set[str]:
    return {i for j in jobs for i in [*j.targets, *j.inputs]}


# --- fix jobs ---


def test_fix_jobs_group_by_page_and_route_codes(root):
    add(root, "concepts.a", "Alpha", "# Alpha\n\nSee [gone](gone.md).\n")
    edit(root, GIT_REL, "review_after: 2099-01-01", "review_after: 2026-06-01")
    edit(root, GIT_REL, f"sha256: {sha(root)}", "sha256: " + "ab" * 32)
    add(root, "concepts.s", "Secret", f"# S\n\n**TL;DR** S.\n{SECRET}")
    rep = report(root)
    assert any(f.code == "not-in-index" for f in rep.findings)  # unindexed pages

    jobs, left, _ = fix_jobs(root, load_config(root), rep)

    assert [(j.kind, j.targets) for j in jobs] == [("fix", ["concepts.a"])]
    assert jobs[0].inputs[0] == "concepts.a"
    assert {"broken-link", "missing-tldr"} <= {f.code for f in jobs[0].findings}
    left_codes = {(f.code, f.path) for f in left}
    assert ("stale", GIT_REL) in left_codes
    assert ("sha-mismatch", GIT_REL) in left_codes
    assert ("secret-in-page", "wiki/concepts/s.md") in left_codes
    assert HINTS["stale"] == (
        "review the page against its sources, then re-run `wiki ingest` or edit "
        "`last_verified` by hand"
    )
    assert HINTS["sha-mismatch"] == "run `wiki ingest`"
    assert "secret-in-page" in HINTS
    every = left + [f for j in jobs for f in j.findings]
    assert all(f.code != "not-in-index" for f in every)
    assert all(f.code not in FIX_CODES for f in left if f.path != "wiki/concepts/s.md")


def test_hints_cover_left_codes():
    never_sent = {
        "secret-in-page",
        "injection-in-page",
        "log-format",
        "index-too-long",
        "stale",
        "sha-mismatch",
        "duplicate-id",
        "cap-sources",
    }
    consolidate = {
        "page-too-long",
        "duplicate-title",
        "cap-pages",
        "cap-pages-warn",
        "cap-section",
        "cap-section-warn",
    }
    assert never_sent | consolidate <= set(HINTS)
    assert all(HINTS[c] == "run `wiki consolidate`" for c in consolidate)
    assert not FIX_CODES & set(HINTS)


def test_fix_jobs_route_consolidate_codes_to_left(root):
    long_body = "# Long\n\n**TL;DR** Long.\n\n" + "".join(
        f"- item {n}\n" for n in range(420)
    )
    add(root, "concepts.long", "Long page", long_body, cited=False, type_="hub")
    add(root, "concepts.flow", "Git flow")  # duplicate title of git-flow
    reindex(root)
    jobs, left, _ = fix_jobs(root, load_config(root), report(root))
    left_codes = {f.code for f in left}
    assert {"page-too-long", "duplicate-title"} <= left_codes
    assert all(
        f.code not in {"page-too-long", "duplicate-title"}
        for j in jobs
        for f in j.findings
    )


def test_deep_finding_joins_first_page_job(root):
    add(root, "concepts.a", "Alpha", "# Alpha\n\nNo summary line.\n")
    reindex(root)
    rep = report(root)
    rep.findings.append(
        Finding(
            "warn",
            "deep-missing-link",
            page_rel("concepts.a"),
            "Link them. (pages: concepts.a, conventions.git-flow; evidence: x)",
            pages=("concepts.a", GIT),
        )
    )
    jobs, _, _ = fix_jobs(root, load_config(root), rep)
    (job,) = jobs
    assert job.targets == ["concepts.a"]
    assert job.inputs[:2] == ["concepts.a", GIT]
    assert {"missing-tldr", "deep-missing-link"} <= {f.code for f in job.findings}


def test_orphan_job_adds_search_hits_and_overview(root):
    add(root, "concepts.deploy", "Deploy pipeline")
    for n, title in enumerate(["Deploy pipeline stages", "Deploy pipeline", "Other"]):
        add(root, f"components.c{n}", title, type_="component")
    # the components link each other so only concepts.deploy is an orphan
    for n in range(3):
        links = "".join(f"[c{m}](c{m}.md) " for m in range(3) if m != n)
        edit(root, page_rel(f"components.c{n}"), "Short fact.", f"{links}Short fact.")
    reindex(root)
    jobs, _, _ = fix_jobs(root, load_config(root), report(root))
    (job,) = [j for j in jobs if j.targets == ["concepts.deploy"]]
    assert job.inputs[0] == "concepts.deploy"
    assert "overview" in job.inputs
    hits = [i for i in job.inputs if i.startswith("components.")]
    assert len(hits) <= 3
    assert {"components.c0", "components.c1"} <= set(hits)


def test_flagged_pages_never_in_jobs(root):
    # an orphan whose best search hit is the flagged page
    add(root, "concepts.a", "Deploy pipeline")
    # flagged (secret), same title (a merge candidate), deprecated and linked from
    # concepts.l (a relink candidate), and itself a linker of concepts.old
    add(
        root,
        "concepts.s",
        "Deploy pipeline",
        "# Deploy pipeline\n\n**TL;DR** Deploy pipeline deploy pipeline.\n\n"
        f"See [old](old.md) and [gone](gone.md).\n{SECRET}",
        status="deprecated",
    )
    add(root, "concepts.old", "Old thing", status="deprecated")
    add(
        root,
        "concepts.l",
        "Linker",
        "# Linker\n\n**TL;DR** Links.\n\n[s](s.md) [old](old.md) fact.[^1]\n\n"
        f"[^1]: {RAW_REL}\n",
    )
    edit(root, "wiki/overview.md", "- [Git flow]", "- [L](concepts/l.md)\n- [Git flow]")
    reindex(root)
    rep = report(root)
    assert "wiki/concepts/s.md" in flagged(rep)
    rep.findings += [
        Finding(
            "warn",
            "deep-missing-link",
            page_rel("concepts.a"),
            "m",
            pages=("concepts.a", "concepts.s"),
        ),
        Finding(
            "warn",
            "deep-contradiction",
            page_rel("concepts.s"),
            "m",
            pages=("concepts.s", GIT),
        ),
    ]
    cfg = load_config(root)

    fixes, left, _ = fix_jobs(root, cfg, rep)
    consolidates, _ = consolidate_jobs(root, cfg, rep)

    assert "concepts.s" not in ids_in(fixes) | ids_in(consolidates)
    assert any(j.targets == ["concepts.a"] for j in fixes)  # not vacuous
    (relink,) = [j for j in consolidates if j.kind == "relink"]
    assert relink.targets == ["concepts.old"]
    assert relink.inputs == ["concepts.old", "concepts.l"]
    assert not [j for j in consolidates if j.kind == "merge"]
    assert {f.code for f in left if f.path == "wiki/concepts/s.md"} >= {
        "secret-in-page",
        "broken-link",
        "deep-contradiction",
    }


def test_fix_jobs_capped_errors_first(root):
    for n in range(12):
        add(root, f"concepts.p{n:02}", f"Page {n:02}")  # orphan warnings only
    add(root, "concepts.z", "Zulu", "# Zulu\n\nNo summary line.\n")  # an error
    reindex(root)
    jobs, _, more = fix_jobs(root, load_config(root), report(root))
    assert len(jobs) == MAX_JOBS == 10
    assert more == 3
    assert [j.targets[0] for j in jobs] == [
        "concepts.z",
        *(f"concepts.p{n:02}" for n in range(9)),
    ]


# --- consolidate jobs ---


def seed_consolidate(root):
    # relink: a deprecated page an as-is page still links to
    add(root, "concepts.old", "Old thing", status="deprecated")
    add(root, "concepts.user", "User", "# User\n\n**TL;DR** U.\n\n[old](old.md)\n")
    # promote: a to-be page named on an open-questions line, with an as-is and
    # a to-be linker
    add(root, "requirements.sso", "SSO login", type_="requirement", status="to-be")
    add(
        root,
        "components.auth",
        "Auth service",
        "# Auth\n\n**TL;DR** A.\n\n[sso](../requirements/sso.md)\n",
        type_="component",
    )
    add(
        root,
        "requirements.mfa",
        "MFA",
        "# MFA\n\n**TL;DR** M.\n\n[sso](sso.md)\n",
        type_="requirement",
        status="to-be",
    )
    question = (
        "- [2026-10-01] SSO is live; promote? — [SSO login](requirements/sso.md) "
        f"— {RAW_REL}"
    )
    (root / "wiki" / "open-questions.md").write_text(
        f"# Open questions\n\n- [2026-10-01] Unrelated?\n{question}\n",
        encoding="utf-8",
    )
    # merge: two pages with the same title
    add(root, "components.cache", "Caching layer", type_="component")
    add(root, "concepts.cache", "Caching layer")
    # split: a page past max_page_lines
    long_body = "# Long\n\n**TL;DR** Long.\n\n" + "".join(
        f"- item {n}\n" for n in range(420)
    )
    add(root, "concepts.long", "Long page", long_body)
    reindex(root)
    return question


def test_consolidate_jobs_kinds_and_order(root):
    question = seed_consolidate(root)
    jobs, _ = consolidate_jobs(root, load_config(root), report(root))
    assert [(j.kind, j.targets) for j in jobs] == [
        ("relink", ["concepts.old"]),
        ("promote", ["requirements.sso"]),
        ("merge", ["components.cache", "concepts.cache"]),
        ("split", ["concepts.long"]),
    ]
    relink, promote, merge, split = jobs
    assert relink.inputs == ["concepts.old", "concepts.user"]
    assert promote.inputs == ["requirements.sso", "components.auth"]  # as-is only
    assert promote.questions == [question]
    assert merge.inputs[:2] == ["components.cache", "concepts.cache"]
    assert [f.code for f in merge.findings] == ["duplicate-title"]
    assert split.inputs == ["concepts.long"]
    assert [f.code for f in split.findings] == ["page-too-long"]


def test_consolidate_linkers_capped_at_8(root):
    add(root, "concepts.old", "Old thing", status="deprecated")
    for n in range(10):
        add(
            root,
            f"components.u{n}",
            f"User {n}",
            "# U\n\n**TL;DR** U.\n\n[old](../concepts/old.md)\n",
            type_="component",
        )
    reindex(root)
    (job,), _ = consolidate_jobs(root, load_config(root), report(root))
    assert job.inputs == ["concepts.old", *(f"components.u{n}" for n in range(7))]


def test_consolidate_page_only_in_first_job(root):
    # concepts.cache is deprecated and linked (relink) and a merge candidate
    add(root, "concepts.cache", "Caching layer", status="deprecated")
    add(root, "components.cache", "Caching layer", type_="component")
    add(root, "concepts.user", "User", "# User\n\n**TL;DR** U.\n\n[c](cache.md)\n")
    reindex(root)
    jobs, _ = consolidate_jobs(root, load_config(root), report(root))
    assert [(j.kind, j.targets) for j in jobs] == [("relink", ["concepts.cache"])]


def test_src_pages_never_merged(root):
    long_body = "# Src\n\n**TL;DR** S.\n\n" + "".join(
        f"- item {n}\n" for n in range(420)
    )
    for name, body in (("src-docs-a-md", None), ("src-docs-b-md", long_body)):
        add(root, f"sources.{name}", "Guide", body, type_="source-summary")
    add(root, "concepts.guide", "Guide")
    reindex(root)
    jobs, _ = consolidate_jobs(root, load_config(root), report(root))
    assert not any(i.startswith("sources.") for j in jobs for i in j.targets)
    assert not [j for j in jobs if j.kind in ("merge", "split")]


# --- the validator ---


def mpage(id_="concepts.branching", **kw) -> dict:
    base = {
        "id": id_,
        "action": "create",
        "type": "concept",
        "status": "as-is",
        "adr_status": None,
        "title": "Branching",
        "summary": "How we branch.",
        "superseded_by": None,
        "sources": [RAW_REL],
        "instructions": "Move the branching facts here.",
        "reason": "Split.",
    }
    return {**base, **kw}


def mplan(*pages: dict, resolved=()) -> MaintPlan:
    return MaintPlan.model_validate(
        {
            "summary": "Split git flow.",
            "pages": list(pages),
            "resolved_questions": list(resolved),
            "open_questions": [],
        }
    )


def job(kind: JobKind = "split", targets=(GIT,), inputs=None, questions=()) -> Job:
    return Job(
        kind=kind,
        targets=list(targets),
        inputs=list(inputs if inputs is not None else targets),
        findings=[],
        questions=list(questions),
    )


def check(root, plan, j=None, mode: Mode = "consolidate", cfg=None) -> list[str]:
    return validate_maint_plan(
        plan, j or job(), root=root, cfg=cfg or load_config(root), mode=mode
    )


def git_update(**kw) -> dict:
    return mpage(GIT, **{"action": "update", "type": "convention", **kw})


def test_valid_maint_plan_passes(root):
    assert check(root, mplan(mpage(), git_update(type="hub"))) == []


def test_maint_plan_size_and_unique_ids(root):
    pages = [mpage(f"concepts.p{i}") for i in range(16)]
    assert any("16 pages" in e and "15" in e for e in check(root, mplan(*pages)))
    errors = check(root, mplan(mpage(), mpage()))
    assert any("concepts.branching" in e and "more than once" in e for e in errors)


def test_update_outside_inputs_rejected(root):
    errors = check(root, mplan(mpage("overview", action="update", type="overview")))
    assert any("overview" in e and "input" in e for e in errors)
    j = job(inputs=[GIT, "overview"])
    assert (
        check(root, mplan(mpage("overview", action="update", type="overview")), j) == []
    )


def test_create_and_update_existence(root):
    assert any("already exists" in e for e in check(root, mplan(mpage(GIT))))
    errors = check(
        root,
        mplan(mpage("concepts.ghost", action="update")),
        job(inputs=[GIT, "concepts.ghost"]),
    )
    assert any("concepts.ghost" in e and "does not exist" in e for e in errors)


def test_new_evidence_rejected(root):
    errors = check(root, mplan(mpage(sources=[OTHER_RAW])))
    assert any("concepts.branching" in e and OTHER_RAW in e for e in errors)
    # RAW_REL is cited by git-flow, an input; the overview cites nothing
    assert check(root, mplan(mpage(sources=[RAW_REL]))) == []
    errors = check(root, mplan(mpage()), job(targets=["overview"]))
    assert any(RAW_REL in e for e in errors)


def test_new_evidence_counts_footnotes(root):
    # a raw path cited only in a footnote of an input page counts as cited
    edit(root, GIT_REL, f"[^1]: {RAW_REL}", f"[^1]: {RAW_REL}\n[^2]: {OTHER_RAW}")
    assert check(root, mplan(mpage(sources=[OTHER_RAW]))) == []


def test_superseded_needs_live_successor(root):
    add(root, "concepts.dup", "Dup")
    j = job("merge", targets=[GIT, "concepts.dup"])

    def stub(**kw):
        return mpage("concepts.dup", action="update", status="superseded", **kw)

    for bad in (None, "concepts.dup", "concepts.nowhere", "overview"):
        errors = check(root, mplan(stub(superseded_by=bad)), j)
        assert any("concepts.dup" in e and "superseded_by" in e for e in errors), bad
    assert check(root, mplan(stub(superseded_by=GIT)), j) == []
    assert (
        check(root, mplan(mpage(), stub(superseded_by="concepts.branching")), j) == []
    )
    # the successor is itself retired by this plan
    both = mplan(stub(superseded_by=GIT), git_update(status="deprecated"))
    assert any("superseded_by" in e for e in check(root, both, j))


def test_consolidate_cap_offsets_superseded(root):
    add(root, "concepts.dup", "Dup")  # 4 pages: git-flow, dup, overview, glossary
    j = job("merge", targets=[GIT, "concepts.dup"])
    stub = mpage(
        "concepts.dup",
        action="update",
        status="superseded",
        superseded_by="concepts.branching",
    )
    for cfg in (set_caps(root, max_pages=4), set_caps(root, max_section_pages=1)):
        assert check(root, mplan(mpage(), stub), j, cfg=cfg) == []
        errors = check(root, mplan(mpage()), j, cfg=cfg)
        assert errors
        assert all(e.endswith(CONSOLIDATE_HINT) for e in errors)
        assert any("concepts.branching" in e for e in errors)


def test_consolidate_cap_counts_only_newly_retired(root):
    add(root, "concepts.dup", "Dup", status="deprecated")
    j = job("merge", targets=[GIT, "concepts.dup"])
    again = mpage("concepts.dup", action="update", status="deprecated")
    errors = check(root, mplan(mpage(), again), j, cfg=set_caps(root, max_pages=4))
    assert any(e.endswith(CONSOLIDATE_HINT) for e in errors)


def test_fix_cap_like_ingest(root):
    # 3 pages exist; ingest reserves one more for the source page, fix does not
    cfg = set_caps(root, max_pages=4)
    assert check(root, mplan(mpage()), mode="fix", cfg=cfg) == []
    ingest_plan = Plan.model_validate(
        {
            "source_title": "G",
            "source_summary": "G.",
            "pages": [
                {
                    **{
                        k: v
                        for k, v in mpage().items()
                        if k not in ("superseded_by", "sources", "instructions")
                    },
                    "claims": [
                        {
                            "text": "x",
                            "source": RAW_REL,
                            "locator": "s",
                            "confidence": "high",
                        }
                    ],
                }
            ],
            "contradictions": [],
            "open_questions": [],
        }
    )
    assert validate_plan(
        ingest_plan,
        root=root,
        cfg=cfg,
        source=RAW_REL,
        manifest=Manifest.load(root),
        allowed_updates=None,
    )
    cfg = set_caps(root, max_pages=3)
    errors = check(root, mplan(mpage()), mode="fix", cfg=cfg)
    assert any(
        "concepts.branching" in e and e.endswith(CONSOLIDATE_HINT) for e in errors
    )
    # a supersede frees no slot in fix mode
    add(root, "concepts.dup", "Dup")
    stub = mpage(
        "concepts.dup",
        action="update",
        status="superseded",
        superseded_by="concepts.branching",
    )
    j = job("fix", targets=[GIT, "concepts.dup"])
    assert check(
        root, mplan(mpage(), stub), j, mode="fix", cfg=set_caps(root, max_pages=4)
    )


def test_resolved_question_must_exist(root):
    named = (
        f"- [2026-10-01] Is it right? — [Git flow](conventions/git-flow.md) — {RAW_REL}"
    )
    other = "- [2026-10-01] Unrelated? — [Overview](overview.md)"
    (root / "wiki" / "open-questions.md").write_text(
        f"# Open questions\n\n{named}\n{other}\n", encoding="utf-8"
    )
    j = job("fix")
    assert check(root, mplan(git_update(), resolved=[named]), j, mode="fix") == []
    for bad in ("- not a line", named[:-3], other):
        errors = check(root, mplan(git_update(), resolved=[bad]), j, mode="fix")
        assert any("resolved" in e for e in errors), bad
    # a promote job may resolve the lines it was built from
    promote = job("promote", questions=[other])
    assert check(root, mplan(git_update(), resolved=[other]), promote) == []


SHARED = [
    ("Concepts.X", {}),  # grammar
    ("log", {"action": "update"}),  # a system page
    ("sources.src-x", {"type": "source-summary"}),  # a sources section
    ("nope.thing", {}),  # an unknown section
    ("glossary", {"type": "glossary"}),  # top level: update only
    ("concepts.branching", {"status": "to-be"}),  # routing
    ("decisions.use-git", {"type": "decision"}),  # decision without adr_status
    ("requirements.old", {"type": "requirement", "status": "deprecated"}),
]


@pytest.mark.parametrize(("id_", "kw"), SHARED)
def test_routing_and_grammar_shared_with_ingest(root, id_, kw):
    page = mpage(id_, **kw)
    ingest_page = {
        **{
            k: v
            for k, v in page.items()
            if k not in ("superseded_by", "sources", "instructions")
        },
        "claims": [
            {"text": "x", "source": RAW_REL, "locator": "s", "confidence": "high"}
        ],
    }
    ingest_errors = validate_plan(
        Plan.model_validate(
            {
                "source_title": "G",
                "source_summary": "G.",
                "pages": [ingest_page],
                "contradictions": [],
                "open_questions": [],
            }
        ),
        root=root,
        cfg=load_config(root),
        source=RAW_REL,
        manifest=Manifest.load(root),
        allowed_updates=None,
    )
    expected = [e for e in ingest_errors if e.startswith(id_ + ":")]
    assert expected
    maint_errors = check(root, mplan(page), job(inputs=[GIT, id_]))
    assert set(expected) <= set(maint_errors)


# --- the schema and the prompt ---


def test_maint_schema_matches_models():
    assert set(MAINT_SCHEMA["required"]) == set(MaintPlan.model_fields)
    item = MAINT_SCHEMA["properties"]["pages"]["items"]
    assert set(item["required"]) == set(MaintPage.model_fields)
    assert set(item["properties"]) == set(MaintPage.model_fields)
    assert item["properties"]["superseded_by"]["type"] == ["string", "null"]


def test_plan_prompts_load(root):
    (root / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
    for mode, name in PLAN_PROMPTS.items():
        text = system_prompt(root, name)
        assert text.startswith("# Rules\n\n\n")
        assert "never" in text.lower(), mode


def test_maint_prompt_wraps_pages_with_nonce(root):
    add(root, "concepts.s", "Hidden secret page", f"# S\n\n**TL;DR** S.\n{SECRET}")
    reindex(root)
    rep = report(root)
    finding = Finding(
        "error", "broken-link", GIT_REL, "link target 'x.md' does not exist"
    )
    j = Job("fix", [GIT], [GIT, "overview"], [finding], ["- [2026-10-01] Why? — x"])
    prompt = build_maint_plan_prompt(
        root,
        load_config(root),
        j,
        "fix",
        exclude=flagged(rep),
        today=TODAY,
        errors=["concepts.x: bad id"],
    )
    m = re.search(r"<untrusted-page-([0-9a-f]{16})>", prompt)
    assert m is not None
    nonce = m.group(1)
    opens = re.findall(r"<untrusted-page-([0-9a-f]+)>", prompt)
    assert opens == [nonce, nonce]
    assert prompt.count(f"</untrusted-page-{nonce}>") == 2
    pages = re.findall(
        rf"<untrusted-page-{nonce}>\n(.*?)</untrusted-page-{nonce}>", prompt, re.DOTALL
    )
    assert pages[0].startswith(f"id: {GIT}\n")
    assert pages[1].startswith("id: overview\n")
    assert prompt.count("We branch from main for every change.") == 1
    assert "We branch from main for every change." in pages[0]
    assert "2026-10-28" in prompt
    assert "fix" in prompt and GIT in prompt
    assert f"broken-link {GIT_REL}: link target 'x.md' does not exist" in prompt
    assert f"<untrusted-indexes-{nonce}>" in prompt
    assert "Hidden secret page" not in prompt  # its index line is dropped
    assert "Sup3rS3cret" not in prompt
    assert "- [2026-10-01] Why? — x" in prompt
    assert "concepts.x: bad id" in prompt
    retry_free = build_maint_plan_prompt(
        root, load_config(root), j, "fix", exclude=set(), today=TODAY
    )
    assert "concepts.x: bad id" not in retry_free


def test_flagged_open_questions_give_no_promote_job(root):
    seed_consolidate(root)
    oq = root / "wiki" / "open-questions.md"
    oq.write_text(oq.read_text(encoding="utf-8") + SECRET, encoding="utf-8")
    rep = report(root)
    assert "wiki/open-questions.md" in flagged(rep)
    jobs, _ = consolidate_jobs(root, load_config(root), rep)
    assert [j.kind for j in jobs] == ["relink", "merge", "split"]


# --- fix round 1 ---


def stub_page(root, successor=GIT):
    """A superseded stub without a TL;DR that the overview links (no orphan)."""
    add(
        root,
        "concepts.stub",
        "Stub",
        f"# Stub\n\nReplaced by [{successor}](../conventions/git-flow.md).\n",
        status="superseded",
        extra=f"superseded_by: {successor}\n",
    )
    edit(
        root,
        "wiki/overview.md",
        "- [Git flow]",
        "- [S](concepts/stub.md)\n- [Git flow]",
    )
    reindex(root)


def keep_stub(**kw) -> MaintPlan:
    return mplan(mpage("concepts.stub", action="update", status="superseded", **kw))


def test_fix_job_on_stub_keeps_current_successor(root):
    # R9: the stub's job has only itself as input; keeping the successor it
    # already names is valid while that successor is live
    stub_page(root)
    (job,), _, _ = fix_jobs(root, load_config(root), report(root))
    assert job.inputs == ["concepts.stub"]
    assert [f.code for f in job.findings] == ["missing-tldr"]
    assert check(root, keep_stub(superseded_by=GIT), job, mode="fix") == []
    # another page that is neither an input nor the current successor
    errors = check(root, keep_stub(superseded_by="overview"), job, mode="fix")
    assert any("superseded_by" in e for e in errors)


@pytest.mark.parametrize(
    "spoil",
    [
        lambda root: edit(root, GIT_REL, "status: as-is", "status: deprecated"),
        lambda root: edit(root, GIT_REL, "status: as-is", "status: superseded"),
        lambda root: (root / GIT_REL).unlink(),
        lambda root: edit(
            root, GIT_REL, "for every change.", "for every change." + SECRET
        ),
    ],
    ids=["deprecated", "superseded", "archived-or-gone", "flagged"],
)
def test_kept_successor_must_be_live(root, spoil):
    stub_page(root)
    job_ = job("fix", targets=["concepts.stub"])
    spoil(root)
    errors = check(root, keep_stub(superseded_by=GIT), job_, mode="fix")
    assert any("concepts.stub" in e and "superseded_by" in e for e in errors)


def test_fix_job_prompt_carries_target_question(root):
    line = (
        f"- [2026-10-01] Still right? — [Git flow](conventions/git-flow.md) — {RAW_REL}"
    )
    (root / "wiki" / "open-questions.md").write_text(
        f"# Open questions\n\n- [2026-10-01] Unrelated?\n{line}\n", encoding="utf-8"
    )
    edit(root, GIT_REL, "for every change.", "for every change. See [x](x.md).")
    rep = report(root)
    (job,), _, _ = fix_jobs(root, load_config(root), rep)
    assert job.targets == [GIT]
    assert job.questions == [line]
    prompt = build_maint_plan_prompt(
        root, load_config(root), job, "fix", exclude=flagged(rep), today=TODAY
    )
    m = re.search(
        r"<untrusted-questions-([0-9a-f]{16})>\n(.*?)\n</untrusted-questions-\1>",
        prompt,
        re.DOTALL,
    )
    assert m is not None
    assert m.group(2) == line
    # the validator then accepts it as resolved
    assert check(root, mplan(git_update(), resolved=[line]), job, mode="fix") == []


def test_consolidate_jobs_carry_target_questions(root):
    seed_consolidate(root)
    line = "- [2026-10-02] Too long? — [Long](concepts/long.md)"
    oq = root / "wiki" / "open-questions.md"
    oq.write_text(oq.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")
    jobs, _ = consolidate_jobs(root, load_config(root), report(root))
    by_kind = {j.kind: j for j in jobs}
    assert by_kind["split"].questions == [line]
    assert by_kind["relink"].questions == []


def test_source_page_findings_left_for_ingest(root):
    src = "sources.src-docs-guide-md"
    add(
        root,
        src,
        "Guide source",
        "# Guide\n\nNo summary line.\n",
        type_="source-summary",
    )
    reindex(root)
    cfg = load_config(root)
    jobs, left, _ = fix_jobs(root, cfg, report(root))
    (job,) = [j for j in jobs if j.targets == [src]]
    assert [f.code for f in job.findings] == ["orphan"]  # its hosts are updated
    (tldr,) = [f for f in left if f.code == "missing-tldr"]
    assert tldr.path == page_rel(src)
    assert hint(tldr, cfg) == "re-run `wiki ingest`"
    assert hint(Finding("error", "broken-link", "wiki/log.md", "m"), cfg) == HAND_HINT
    assert hint(Finding("warn", "stale", GIT_REL, "m"), cfg) == HINTS["stale"]


def test_orphan_hosts_skip_retired_pages(root):
    add(root, "concepts.deploy", "Deploy pipeline")
    add(
        root,
        "components.old",
        "Deploy pipeline",
        type_="component",
        status="deprecated",
    )
    reindex(root)
    jobs, _, _ = fix_jobs(root, load_config(root), report(root))
    (job,) = [j for j in jobs if j.targets == ["concepts.deploy"]]
    assert "components.old" not in job.inputs


def test_page_linked_only_by_stub_gets_no_relink(root):
    add(root, "concepts.old", "Old thing", status="deprecated")
    add(
        root,
        "concepts.stub",
        "Stub",
        "# Stub\n\n**TL;DR** Gone.\n\n[old](old.md)\n",
        status="superseded",
        extra=f"superseded_by: {GIT}\n",
    )
    reindex(root)
    assert live_linkers(root, "concepts.old") == []
    assert live_linkers(root, GIT) == ["overview"]
    jobs, more = consolidate_jobs(root, load_config(root), report(root))
    assert jobs == []
    assert more == 0


def test_consolidate_jobs_report_overflow(root):
    for n in range(12):
        add(root, f"concepts.old{n:02}", f"Old {n:02} thing", status="deprecated")
        add(
            root,
            f"components.u{n:02}",
            f"User {n:02} page",
            f"# U\n\n**TL;DR** U.\n\n[o](../concepts/old{n:02}.md)\n",
            type_="component",
        )
    reindex(root)
    jobs, more = consolidate_jobs(root, load_config(root), report(root))
    assert len(jobs) == MAX_JOBS
    assert more == 2
