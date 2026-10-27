import json
from datetime import date
from pathlib import Path

import pytest

from llm_wiki.pages import (
    Frontmatter,
    id_for,
    iter_pages,
    read_page,
    render_frontmatter,
    split_frontmatter,
)

SHA = "a" * 64


def fm_text(**over: str) -> str:
    fields = {
        "id": "conventions.git-flow",
        "title": "Git flow",
        "type": "convention",
        "status": "as-is",
        "summary": "How we branch.",
        "confidence": "high",
        "review_status": "draft",
        "last_verified": "2026-01-01",
        "review_after": "2026-07-01",
    }
    fields.update(over)
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items()) + "---\n"


def write(root: Path, rel: str, text: str) -> Path:
    p = root / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_valid_page_parses(tmp_path):
    p = write(tmp_path, "conventions/git-flow.md", fm_text() + "# Body\n")
    page = read_page(tmp_path, p)
    assert page.error is None
    assert page.fm is not None
    assert page.fm.title == "Git flow"
    assert page.fm.last_verified == date(2026, 1, 1)
    assert page.rel == "conventions/git-flow.md"
    assert page.body == "# Body\n"
    assert page.line_count == 12


def test_bad_vocab_sets_error(tmp_path):
    p = write(tmp_path, "a/x.md", fm_text(status="done"))
    page = read_page(tmp_path, p)
    assert page.fm is None
    assert page.error
    assert "status" in page.error


def test_missing_frontmatter_sets_error(tmp_path):
    p = write(tmp_path, "a/x.md", "# Just a body\n")
    page = read_page(tmp_path, p)
    assert page.fm is None
    assert page.error


def test_malformed_yaml_sets_error_not_raise(tmp_path):
    p = write(tmp_path, "a/x.md", "---\nid: [unclosed\n---\nbody\n")
    page = read_page(tmp_path, p)
    assert page.fm is None
    assert page.error
    with pytest.raises(ValueError):
        split_frontmatter("---\nid: [unclosed\n---\nbody\n")


def test_id_for_paths():
    assert id_for("conventions/git-flow.md") == "conventions.git-flow"
    assert id_for("overview.md") == "overview"
    assert id_for("a/b/c.md") == "a.b.c"


def test_source_path_must_be_raw(tmp_path):
    src = f"sources:\n  - path: wiki/x.md\n    sha256: {SHA}\n    locator: p1\n"
    text = fm_text().rstrip("-\n") + "\n" + src + "---\n"
    page = read_page(tmp_path, write(tmp_path, "a/x.md", text))
    assert page.fm is None
    assert page.error
    assert "raw/" in page.error
    ok = text.replace("wiki/x.md", "raw/x.md")
    assert read_page(tmp_path, write(tmp_path, "a/y.md", ok)).error is None


def test_review_after_must_follow_last_verified(tmp_path):
    p = write(tmp_path, "a/x.md", fm_text(review_after="2026-01-01"))
    page = read_page(tmp_path, p)
    assert page.fm is None
    assert page.error
    assert "review_after" in page.error


def test_iter_pages_skips_system_files_and_archive(tmp_path):
    for rel in (
        "overview.md",
        "b/two.md",
        "a/one.md",
        "a/_index.md",
        "index.md",
        "log.md",
        "open-questions.md",
        "_archive/old.md",
    ):
        write(tmp_path, rel, "x\n")
    assert [p.rel for p in iter_pages(tmp_path)] == [
        "a/one.md",
        "b/two.md",
        "overview.md",
    ]


def test_render_frontmatter_roundtrip(tmp_path):
    p = write(tmp_path, "a/x.md", fm_text())
    fm = read_page(tmp_path, p).fm
    assert isinstance(fm, Frontmatter)
    out = render_frontmatter(fm)
    assert out == render_frontmatter(fm)
    assert "2026-01-01" in out
    data, _ = split_frontmatter(out + "body\n")
    assert Frontmatter.model_validate(data) == fm


def test_non_utf8_file_sets_error_not_raise(tmp_path):
    p = tmp_path / "wiki" / "a" / "x.md"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\xff\xfe")
    page = read_page(tmp_path, p)
    assert page.fm is None
    assert page.error
    assert "UTF-8" in page.error
    assert page.body == ""
    assert page.line_count == 0


def test_iter_pages_skips_directories_named_md(tmp_path):
    (tmp_path / "wiki" / "x.md").mkdir(parents=True)
    write(tmp_path, "a/y.md", fm_text())
    assert [p.rel for p in iter_pages(tmp_path)] == ["a/y.md"]


@pytest.mark.parametrize("sha", ["a" * 64 + "\n", "A" * 64])
def test_sha256_rejects_newline_and_uppercase(tmp_path, sha):
    src = (
        "sources:\n  - path: raw/a.md\n"
        f"    sha256: {json.dumps(sha)}\n    locator: L1\n"
    )
    p = write(tmp_path, "a/x.md", fm_text().removesuffix("---\n") + src + "---\n")
    page = read_page(tmp_path, p)
    assert page.fm is None
    assert page.error
    assert "sha256" in page.error


def test_source_path_rejects_dotdot(tmp_path):
    src = f"sources:\n  - path: raw/../wiki/x.md\n    sha256: {SHA}\n    locator: L1\n"
    p = write(tmp_path, "a/x.md", fm_text().removesuffix("---\n") + src + "---\n")
    page = read_page(tmp_path, p)
    assert page.fm is None
    assert page.error
    assert "path" in page.error
