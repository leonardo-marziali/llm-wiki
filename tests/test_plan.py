from typing import get_args

import pytest
from conftest import RAW_REL

from llm_wiki.config import dump_config, load_config
from llm_wiki.lint import routing_error
from llm_wiki.manifest import Manifest
from llm_wiki.pages import AdrStatus, Confidence, PageType, Status
from llm_wiki.plan import validate_plan
from llm_wiki.schemas import PLAN_SCHEMA, Plan

OTHER_RAW = "raw/docs/other.md"


@pytest.fixture
def root(tmp_path, make_wiki):
    return make_wiki(tmp_path)


def claim(source: str = RAW_REL) -> dict:
    return {
        "text": "Branch from main.",
        "source": source,
        "locator": "section 1",
        "confidence": "high",
    }


def page(id_: str = "concepts.branching", **kw) -> dict:
    base = {
        "id": id_,
        "action": "create",
        "type": "concept",
        "status": "as-is",
        "adr_status": None,
        "title": "Branching",
        "summary": "How we branch.",
        "claims": [claim()],
        "reason": "New topic.",
    }
    return {**base, **kw}


def plan(*pages: dict) -> Plan:
    return Plan.model_validate(
        {
            "source_title": "Guide",
            "source_summary": "A guide.",
            "pages": list(pages),
            "contradictions": [],
            "open_questions": [],
        }
    )


def check(root, *pages: dict, allowed_updates=None, cfg=None) -> list[str]:
    return validate_plan(
        plan(*pages),
        root=root,
        cfg=cfg or load_config(root),
        source=RAW_REL,
        manifest=Manifest.load(root),
        allowed_updates=allowed_updates,
    )


def test_valid_plan_passes(root):
    update = page("conventions.git-flow", action="update", type="convention")
    assert check(root, page(), update) == []


def test_too_many_pages(root):
    pages = [page(f"concepts.p{i}") for i in range(16)]
    errors = check(root, *pages)
    assert any("16 pages" in e and "15" in e for e in errors)


def test_duplicate_ids(root):
    errors = check(root, page(), page())
    assert any("concepts.branching" in e and "more than once" in e for e in errors)


@pytest.mark.parametrize("bad", ["Concepts.Branching", "concepts.-x", "concepts..x"])
def test_bad_id_grammar(root, bad):
    errors = check(root, page(bad))
    assert any(bad in e and "id" in e for e in errors)


def test_nested_id_rejected(root):
    errors = check(root, page("concepts.a.b"))
    assert any("concepts.a.b" in e for e in errors)


def test_unknown_or_sources_section(root):
    unknown = check(root, page("nope.thing"))
    assert any("nope.thing" in e and "section" in e for e in unknown)
    src = check(root, page("sources.src-x", type="source-summary"))
    assert any("sources.src-x" in e and "section" in e for e in src)


@pytest.mark.parametrize("name", ["index", "log", "open-questions", "_index"])
def test_system_page_rejected(root, name):
    errors = check(root, page(name, action="update"))
    assert any(name in e for e in errors)


def test_overview_update_only(root):
    ok = page("overview", action="update", type="overview")
    assert check(root, ok) == []
    errors = check(root, page("glossary", action="create", type="glossary"))
    assert any("glossary" in e and "update" in e for e in errors)


def test_create_existing_rejected(root):
    errors = check(root, page("conventions.git-flow"))
    assert any("conventions.git-flow" in e and "already exists" in e for e in errors)


def test_update_missing_rejected(root):
    errors = check(root, page("concepts.ghost", action="update"))
    assert any("concepts.ghost" in e and "does not exist" in e for e in errors)


def test_routing_to_be_in_as_is(root):
    errors = check(root, page(status="to-be"))
    assert any("concepts.branching" in e and "to-be" in e for e in errors)


def test_decision_without_adr_status(root):
    errors = check(root, page("decisions.use-git", type="decision"))
    assert any("decisions.use-git" in e and "adr_status" in e for e in errors)
    good = page("decisions.use-git", type="decision", adr_status="accepted")
    assert check(root, good) == []


def test_routing_error_matches_lint_messages():
    assert routing_error("as-is", "concept", "as-is", None) is None
    assert routing_error("as-is", "concept", "to-be", None) == (
        "status 'to-be' in a as-is section"
    )
    assert routing_error("to-be", "requirement", "superseded", None) is None
    assert routing_error("decisions", "concept", "as-is", None) == (
        "type 'concept' in a decisions section (must be 'decision')"
    )


def set_caps(root, **caps):
    cfg = load_config(root).model_copy(update={"caps": caps})
    (root / "wiki.toml").write_text(dump_config(cfg), encoding="utf-8")
    return load_config(root)


def test_cap_blocks_creates_but_allows_updates(root):
    cfg = set_caps(root, max_pages=1)
    update = page("conventions.git-flow", action="update", type="convention")
    assert check(root, update, cfg=cfg) == []
    errors = check(root, page(), cfg=cfg)
    assert any("concepts.branching" in e and "wiki consolidate" in e for e in errors)


def test_section_cap_blocks_creates(root):
    cfg = set_caps(root, max_section_pages=1)
    errors = check(root, page("conventions.second", type="convention"), cfg=cfg)
    assert any("conventions.second" in e and "conventions" in e for e in errors)
    assert check(root, page(), cfg=cfg) == []  # concepts is empty


def test_foreign_citation_rejected(root):
    errors = check(root, page(claims=[claim(OTHER_RAW)]))
    assert any("concepts.branching" in e and OTHER_RAW in e for e in errors)


def test_other_known_raw_citation_allowed(root):
    other = root / OTHER_RAW
    other.write_text("other\n", encoding="utf-8")
    m = Manifest.load(root)
    m.entries[OTHER_RAW] = m.entries[RAW_REL].model_copy(update={"path": OTHER_RAW})
    m.save(root)
    assert check(root, page(claims=[claim(OTHER_RAW)])) == []
    other.unlink()  # in the manifest but gone from disk
    assert check(root, page(claims=[claim(OTHER_RAW)]))
    other.write_text("other\n", encoding="utf-8")
    m.entries[OTHER_RAW].status = "quarantined"
    m.save(root)
    assert check(root, page(claims=[claim(OTHER_RAW)]))


def test_changed_source_update_outside_pages_for_rejected(root):
    update = page("conventions.git-flow", action="update", type="convention")
    assert check(root, update, allowed_updates={"conventions.git-flow"}) == []
    errors = check(root, update, allowed_updates=set())
    assert any("conventions.git-flow" in e and "changed source" in e for e in errors)
    # creates are not restricted by allowed_updates
    assert check(root, page(), allowed_updates=set()) == []


def test_schema_matches_models():
    example = plan(page(adr_status="accepted")).model_dump()
    assert Plan.model_validate(example)
    assert set(PLAN_SCHEMA["required"]) == set(PLAN_SCHEMA["properties"])
    assert set(PLAN_SCHEMA["required"]) == set(Plan.model_fields)
    item = PLAN_SCHEMA["properties"]["pages"]["items"]
    assert set(item["required"]) == set(item["properties"])
    assert set(item["required"]) == set(example["pages"][0])
    claim_schema = item["properties"]["claims"]["items"]
    assert set(claim_schema["required"]) == set(example["pages"][0]["claims"][0])
    contra = PLAN_SCHEMA["properties"]["contradictions"]["items"]
    assert set(contra["required"]) == {"page_id", "existing", "new", "locator"}
    props = item["properties"]
    assert props["type"]["enum"] == list(get_args(PageType))
    assert props["status"]["enum"] == list(get_args(Status))
    assert props["adr_status"]["enum"] == [*get_args(AdrStatus), None]
    assert claim_schema["properties"]["confidence"]["enum"] == list(
        get_args(Confidence)
    )
    for node in (PLAN_SCHEMA, item, claim_schema, contra):
        assert node["additionalProperties"] is False
