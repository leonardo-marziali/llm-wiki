import pytest

from llm_wiki import gitops
from llm_wiki.errors import WikiError
from llm_wiki.gate import DIRT_OK, OP_PREFIXES, preflight_git


@pytest.fixture
def wiki(tmp_path, make_wiki):
    """A wiki made before M5: its .gitignore lacks `.wiki/reports/`."""
    root = make_wiki(tmp_path)
    gitops.init_repo(root)
    gitops.commit_all(root, "init")
    return root


def test_op_constants():
    assert OP_PREFIXES == ("wiki/ingest-", "wiki/fix-", "wiki/consolidate-")
    assert DIRT_OK == ("inbox/", "raw/", ".wiki/reports/")


@pytest.mark.parametrize(
    ("prefix", "op"),
    [
        ("wiki/ingest-", "ingest"),
        ("wiki/fix-", "fix"),
        ("wiki/consolidate-", "consolidate"),
    ],
)
def test_preflight_refuses_any_op_branch(wiki, prefix, op):
    branch = f"{prefix}20261027-old"
    gitops.switch(wiki, branch, create=True)
    with pytest.raises(WikiError) as exc:
        preflight_git(wiki)
    assert str(exc.value) == (
        f"a previous {op} left {branch} checked out; run "
        f"`git switch main && git branch -D {branch}` first"
    )


def test_preflight_names_the_op_off_main(wiki):
    gitops.switch(wiki, "feature", create=True)
    with pytest.raises(WikiError) as exc:
        preflight_git(wiki)
    assert str(exc.value) == "ingest runs on main; the wiki is on feature"
    with pytest.raises(WikiError) as exc:
        preflight_git(wiki, op="fix")
    assert str(exc.value) == "fix runs on main; the wiki is on feature"


def test_preflight_tolerates_reports_dirt(wiki):
    reports = wiki / ".wiki" / "reports"
    reports.mkdir(parents=True)
    (reports / "lint-20261028.md").write_text("# Report\n", encoding="utf-8")
    assert preflight_git(wiki) == "main"
    (wiki / ".wiki" / "other.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(WikiError, match=r"\.wiki/other\.txt"):
        preflight_git(wiki)
