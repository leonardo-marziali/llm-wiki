from pathlib import Path

from llm_wiki.config import Section, WikiConfig
from llm_wiki.index import GENERATED_MARK, build_indexes

CFG = WikiConfig(
    name="Acme wiki",
    description="Acme knowledge.",
    profile="general",
    sections=[
        Section(name="conventions", kind="as-is", description="How we work."),
        Section(name="systems", kind="as-is", description="What we run."),
    ],
)


def page(
    title: str, type_: str = "convention", summary: str = "S.", status: str = "as-is"
) -> str:
    return (
        "---\n"
        f"id: x\ntitle: {title}\ntype: {type_}\nstatus: {status}\nsummary: {summary}\n"
        "confidence: high\nreview_status: draft\n"
        "last_verified: 2026-01-01\nreview_after: 2026-07-01\n---\nbody\n"
    )


def write(root: Path, rel: str, text: str) -> None:
    p = root / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_section_index_lines_and_order(tmp_path):
    write(tmp_path, "conventions/zeta.md", page("Zeta"))
    write(tmp_path, "conventions/alpha.md", page("alpha", summary="First."))
    write(tmp_path, "conventions/sub/hubby.md", page("Zzz hub", type_="hub"))
    changed = build_indexes(tmp_path, CFG)
    idx = tmp_path / "wiki/conventions/_index.md"
    assert idx in changed
    text = idx.read_text(encoding="utf-8")
    assert text.startswith("# Conventions\n\nHow we work.\n\n" + GENERATED_MARK + "\n")
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    lines = text.split(GENERATED_MARK + "\n")[1].splitlines()
    assert lines == [
        "- [Zzz hub](sub/hubby.md) — S. — as-is — 2026-01-01",
        "- [alpha](alpha.md) — First. — as-is — 2026-01-01",
        "- [Zeta](zeta.md) — S. — as-is — 2026-01-01",
    ]


def test_hand_written_header_preserved(tmp_path):
    write(tmp_path, "conventions/a.md", page("A"))
    idx = tmp_path / "wiki/conventions/_index.md"
    idx.write_text(
        "# Mine\n\nHand written.\n\n" + GENERATED_MARK + "\n- stale\n", encoding="utf-8"
    )
    build_indexes(tmp_path, CFG)
    text = idx.read_text(encoding="utf-8")
    assert text.startswith("# Mine\n\nHand written.\n\n" + GENERATED_MARK + "\n")
    assert "stale" not in text
    assert "- [A](a.md)" in text


def test_top_index_lists_sections_with_counts(tmp_path):
    write(tmp_path, "conventions/a.md", page("A"))
    write(tmp_path, "conventions/b.md", page("B"))
    write(tmp_path, "systems/h.md", page("Sys hub", type_="hub"))
    write(tmp_path, "overview.md", page("Overview", type_="overview"))
    build_indexes(tmp_path, CFG)
    text = (tmp_path / "wiki/index.md").read_text(encoding="utf-8")
    assert text.startswith("# Acme wiki\n\nAcme knowledge.\n\n" + GENERATED_MARK + "\n")
    assert (
        "- [Overview](overview.md)\n- [Glossary](glossary.md)\n"
        "- [Open questions](open-questions.md)" in text
    )
    assert "## Sections" in text
    assert "- [conventions](conventions/_index.md) — 2 pages — How we work." in text
    assert "- [systems](systems/_index.md) — 1 pages — What we run." in text
    assert text.index("conventions/_index.md") < text.index("systems/_index.md")
    assert "## Hubs\n" in text
    assert "- [Sys hub](systems/h.md)" in text
    assert "overview.md" not in (tmp_path / "wiki/conventions/_index.md").read_text(
        encoding="utf-8"
    )


def test_top_index_under_100_lines_with_many_hubs(tmp_path):
    for i in range(150):
        write(tmp_path, f"systems/h{i:03}.md", page(f"Hub {i:03}", type_="hub"))
    build_indexes(tmp_path, CFG)
    text = (tmp_path / "wiki/index.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) < 100
    assert lines[-1] == "- … see section indexes"
    assert "- [Hub 000](systems/h000.md)" in text


def test_rebuild_is_noop(tmp_path):
    write(tmp_path, "conventions/a.md", page("A"))
    assert build_indexes(tmp_path, CFG)
    assert build_indexes(tmp_path, CFG) == []


def test_invalid_pages_skipped(tmp_path):
    write(tmp_path, "conventions/a.md", page("A"))
    write(tmp_path, "conventions/bad.md", "no frontmatter\n")
    build_indexes(tmp_path, CFG)
    text = (tmp_path / "wiki/conventions/_index.md").read_text(encoding="utf-8")
    assert "bad.md" not in text
    top = (tmp_path / "wiki/index.md").read_text(encoding="utf-8")
    assert "— 1 pages —" in top


def test_multiline_summary_and_bracket_title_stay_one_line(tmp_path):
    text = page("A [b] — c", summary="'line one\n  line two'")
    write(tmp_path, "conventions/odd.md", text)
    build_indexes(tmp_path, CFG)
    out = (tmp_path / "wiki/conventions/_index.md").read_text(encoding="utf-8")
    lines = out.split(GENERATED_MARK + "\n")[1].splitlines()
    assert lines == [
        r"- [A \[b\] — c](odd.md) — line one line two — as-is — 2026-01-01"
    ]


def test_top_index_under_100_lines_when_budget_exhausted(tmp_path):
    cfg = CFG.model_copy(
        update={
            "sections": [
                Section(name=f"s{i:02}", kind="as-is", description="d")
                for i in range(85)
            ]
        }
    )
    write(tmp_path, "s00/h1.md", page("H1", type_="hub"))
    write(tmp_path, "s00/h2.md", page("H2", type_="hub"))
    build_indexes(tmp_path, cfg)
    lines = (tmp_path / "wiki/index.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) < 100


def test_existing_header_without_mark_kept_whole(tmp_path):
    write(tmp_path, "conventions/a.md", page("A"))
    idx = tmp_path / "wiki/conventions/_index.md"
    idx.write_text("# Old\n\nNotes.\n", encoding="utf-8")
    build_indexes(tmp_path, CFG)
    text = idx.read_text(encoding="utf-8")
    assert text.startswith("# Old\n\nNotes.\n\n" + GENERATED_MARK + "\n")
    assert "- [A](a.md)" in text


def test_page_in_unconfigured_dir_ignored(tmp_path):
    write(tmp_path, "conventions/a.md", page("A"))
    write(tmp_path, "stray/z.md", page("Stray", type_="hub"))
    build_indexes(tmp_path, CFG)
    assert not (tmp_path / "wiki/stray/_index.md").exists()
    top = (tmp_path / "wiki/index.md").read_text(encoding="utf-8")
    assert "Stray" not in top
    assert "stray" not in top
