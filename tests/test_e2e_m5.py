"""M5 end to end: a wiki at its page cap refuses an ingest, ``wiki consolidate``
frees room (a merge, then archives), and the next ingest retries the source,
which now fits.
"""

import hashlib
from importlib import resources

from conftest import RAW_REL, ok, page_text
from typer.testing import CliRunner

from llm_wiki import gitops
from llm_wiki.cli import app
from llm_wiki.config import load_config
from llm_wiki.index import build_indexes
from llm_wiki.lint import CONSOLIDATE_HINT
from llm_wiki.manifest import Manifest
from llm_wiki.pages import iter_pages

runner = CliRunner()

AGENTS = "# Acme: wiki rules\n\nFollow the rules.\n"
B1, B2, OLD = "concepts.branching", "concepts.branching-howto", "concepts.old-deploy"
NEW_TEXT = "# Releases\n\nReleases ship every Friday.\n"
NEW_REL = "raw/releases.md"
NEW_PAGE = "concepts.releases"
NEW_SRC = "sources.src-releases-md"


def rel(id_):
    section, slug = id_.split(".")
    return f"wiki/{section}/{slug}.md"


def archived(id_):
    section, slug = id_.split(".")
    return f"wiki/{section}/_archive/{slug}.md"


def read(root, rel_):
    return (root / rel_).read_text(encoding="utf-8")


def cited(root, id_, title, locs=("§1",), *, status="as-is", extra="", tldr=None):
    """A concept page citing the fixture's raw guide once per locator."""
    sha = Manifest.load(root).entries[RAW_REL].sha256
    marks = "".join(f"[^{n}]" for n in range(1, len(locs) + 1))
    notes = "".join(f"[^{n}]: {RAW_REL} — {loc}\n" for n, loc in enumerate(locs, 1))
    body = (
        f"# {title}\n\n**TL;DR** {tldr or title}.\n\n"
        f"Always branch from main.{marks}\n\n{notes}"
    )
    return page_text(
        id_,
        title,
        body,
        type_="concept",
        status=status,
        extra=extra,
        sources=[(RAW_REL, sha)],
    )


def seed(tmp_path, make_wiki):
    """A committed own-repo wiki at its cap of 6 pages: overview, glossary,
    git flow, two near-duplicate branching pages and a deprecated page that
    nothing links.
    """
    root = make_wiki(tmp_path)
    (root / "AGENTS.md").write_text(AGENTS, encoding="utf-8")
    gitignore = resources.files("llm_wiki") / "templates" / "gitignore"
    (root / ".gitignore").write_text(gitignore.read_text("utf-8"), encoding="utf-8")
    (root / "inbox").mkdir()
    (root / "inbox" / ".gitkeep").write_text("", encoding="utf-8")
    toml = root / "wiki.toml"
    toml.write_text(read(root, "wiki.toml") + "\n[caps]\nmax_pages = 6\n", "utf-8")
    pages = {
        B1: cited(root, B1, "Branching"),
        B2: cited(root, B2, "Branching", ("§2",), tldr="How to branch"),
        OLD: cited(root, OLD, "Old deploy", status="deprecated"),
    }
    for id_, text in pages.items():
        (root / rel(id_)).write_text(text, encoding="utf-8")
    with (root / "wiki" / "overview.md").open("a", encoding="utf-8") as fh:
        fh.write("- [Branching](concepts/branching.md)\n")
        fh.write("- [Branching how-to](concepts/branching-howto.md)\n")
    m = Manifest.load(root)
    entry = m.entries[RAW_REL]
    touched = sorted(["conventions.git-flow", B1, B2, OLD])
    m.record(entry.model_copy(update={"pages_touched": touched}))
    m.save(root)
    build_indexes(root, load_config(root))
    gitops.init_repo(root)
    gitops.commit_all(root, "init")
    return root


# --- the fake claude ---


def ingest_plan():
    claim = {
        "text": "Releases ship every Friday.",
        "source": NEW_REL,
        "locator": "§1",
        "confidence": "high",
    }
    page = {
        "id": NEW_PAGE,
        "action": "create",
        "type": "concept",
        "status": "as-is",
        "adr_status": None,
        "title": "Releases",
        "summary": "When releases ship.",
        "claims": [claim],
        "reason": "New topic.",
    }
    return {
        "source_title": "Releases",
        "source_summary": "The release day.",
        "pages": [page],
        "contradictions": [],
        "open_questions": [],
    }


def ingest_writes():
    digest = hashlib.sha256(NEW_TEXT.encode("utf-8")).hexdigest()
    src = [(NEW_REL, digest)]
    note = f"[^1]: {NEW_REL} — §1\n"
    page = page_text(
        NEW_PAGE,
        "Releases",
        "# Releases\n\n**TL;DR** Fridays.\n\n"
        f"Releases ship every Friday.[^1]\n\n{note}",
        type_="concept",
        sources=src,
    )
    src_page = page_text(
        NEW_SRC,
        "Releases",
        "# Releases\n\n**TL;DR** The release day.\n\n"
        f"The source names the release day.[^1]\n\n{note}",
        type_="source-summary",
        sources=src,
    )
    return {rel(NEW_PAGE): page, rel(NEW_SRC): src_page}


def merge_plan():
    def mpage(id_, **kw):
        base = {
            "id": id_,
            "action": "update",
            "type": "concept",
            "status": "as-is",
            "adr_status": None,
            "title": "Branching",
            "summary": "Branching summary.",
            "superseded_by": None,
            "sources": [RAW_REL],
            "instructions": "Do the job.",
            "reason": "duplicate title",
        }
        return {**base, **kw}

    return {
        "summary": "Merge the two branching pages",
        "pages": [
            mpage(B1, instructions="Keep both facts."),
            mpage(B2, status="superseded", superseded_by=B1, instructions="Stub it."),
            {
                **mpage("overview", type="overview", title="Overview"),
                "summary": "Overview summary.",
                "instructions": "Retarget the how-to link to the branching page.",
            },
        ],
        "resolved_questions": [],
        "open_questions": [],
    }


def merge_writes(root):
    survivor = cited(root, B1, "Branching", ("§1", "§2"))
    stub = page_text(
        B2,
        "Branching",
        "# Branching\n\n**TL;DR** See [Branching](branching.md).\n",
        type_="concept",
        status="superseded",
        extra=f"superseded_by: {B1}\n",
        sources=[(RAW_REL, Manifest.load(root).entries[RAW_REL].sha256)],
    )
    overview = read(root, "wiki/overview.md").replace(
        "- [Branching how-to](concepts/branching-howto.md)\n", ""
    )
    return {rel(B1): survivor, rel(B2): stub, "wiki/overview.md": overview}


def test_e2e_m5_cap_reached(tmp_path, monkeypatch, make_wiki, fake_claude):
    root = seed(tmp_path, make_wiki)
    monkeypatch.chdir(root)
    assert len(list(iter_pages(root))) == 6
    inbox_file = root / "inbox" / "releases.md"
    inbox_file.write_text(NEW_TEXT, encoding="utf-8")

    # 1. at the cap: the plan (and its one retry) is refused and nothing is
    # edited; the source is kept in raw/ as failed, for the next run to retry
    fake_claude.queue(
        {"stdout": ok(structured=ingest_plan(), cost=0.05)},
        {"stdout": ok(structured=ingest_plan(), cost=0.05)},
    )
    r = runner.invoke(app, ["ingest", "--yes"])
    assert r.exit_code == 0, r.output  # a failed source does not fail the run
    assert f"failed      {NEW_REL}: plan rejected" in r.output
    assert "6 pages + 1 creates + 1 source page exceeds the cap of 6" in r.output
    assert CONSOLIDATE_HINT in r.output
    assert "created: none" in r.output
    assert len(fake_claude.calls()) == 2  # two plan calls, no edit
    assert not inbox_file.exists()
    assert read(root, NEW_REL) == NEW_TEXT
    assert Manifest.load(root).entries[NEW_REL].status == "failed"
    assert not (root / rel(NEW_PAGE)).exists()
    assert len(list(iter_pages(root))) == 6
    assert gitops.current_branch(root) == "main"
    assert gitops.changed_paths(root) == []

    # 2. status warns
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert "near cap: pages 6/6: run `wiki consolidate`" in r.output

    # 3. consolidate: the merge supersedes the how-to, which is then archived
    # with the unlinked deprecated page; the failed source is left alone
    fake_claude.queue(
        {"stdout": ok(structured=merge_plan(), cost=0.2)},
        {"stdout": ok(cost=0.3), "write": merge_writes(root)},
    )
    r = runner.invoke(app, ["consolidate", "--yes"])
    assert r.exit_code == 0, r.output
    assert "accepted: wiki/consolidate-" in r.output
    assert f"archived: {B2}, {OLD}" in r.output
    assert len(fake_claude.calls()) == 4
    assert gitops.current_branch(root) == "main"
    assert gitops.git(root, "log", "-1", "--format=%s").startswith(
        "wiki consolidate: 1 job, 2 archived"
    )
    assert not (root / rel(B2)).exists()
    assert not (root / rel(OLD)).exists()
    assert f"superseded_by: {B1}" in read(root, archived(B2))
    assert "status: deprecated" in read(root, archived(OLD))
    assert f"[^2]: {RAW_REL} — §2" in read(root, rel(B1))  # the citation moved
    live = sorted(p.rel for p in iter_pages(root))
    assert live == [
        "concepts/branching.md",
        "conventions/git-flow.md",
        "glossary.md",
        "overview.md",
    ]
    assert Manifest.load(root).entries[NEW_REL].status == "failed"

    # 4. the same ingest retries the failed source, which now fits:
    # 4 pages + 1 create + its source page = 6
    fake_claude.queue(
        {"stdout": ok(structured=ingest_plan(), cost=0.05)},
        {"stdout": ok(cost=0.1), "write": ingest_writes()},
    )
    r = runner.invoke(app, ["ingest", "--yes"])
    assert r.exit_code == 0, r.output
    assert "exceeds the cap" not in r.output
    assert len(fake_claude.calls()) == 6
    assert f"ingested    {NEW_REL}" in r.output
    assert (root / rel(NEW_PAGE)).is_file()
    assert Manifest.load(root).entries[NEW_REL].status == "ingested"
    assert len(list(iter_pages(root))) == 6
    assert gitops.current_branch(root) == "main"

    # 5. the wiki lints clean (the cap warning is not an error)
    r = runner.invoke(app, ["lint"])
    assert r.exit_code == 0, r.output
