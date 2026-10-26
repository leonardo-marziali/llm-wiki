import pytest

from llm_wiki import images
from llm_wiki.caps import HARD_CAPS
from llm_wiki.convert import SUPPORTED, convert
from llm_wiki.errors import RefusedSource, UnsupportedFormat

SHA = "a" * 64


@pytest.fixture
def root(tmp_path):
    (tmp_path / "raw").mkdir()
    return tmp_path


def out(root, conv, i=0):
    return (root / conv.outputs[i]).read_text(encoding="utf-8")


def test_txt_copied_with_header(root):
    p = root / "raw" / "a.txt"
    p.write_text("hello world", encoding="utf-8")
    c = convert(root, p, SHA, HARD_CAPS)
    assert c.converter == "text" and c.parts == 1 and c.flags == []
    assert c.outputs[0] == f".wiki/converted/{SHA}.md"
    text = out(root, c)
    assert text.startswith(f"---\nsource: raw/a.txt\nsha256: {SHA}\nconverter: text\n---\n")
    assert "hello world" in text


def test_latin1_txt_and_bom_csv(root, make_csv):
    t = root / "raw" / "l.txt"
    t.write_bytes("caf\xe9 \x81".encode("latin-1"))
    assert out(root, convert(root, t, SHA, HARD_CAPS)).startswith("---\nsource:")
    c = root / "raw" / "b.csv"
    make_csv(c, [["a", "b"], ["1", "2"]], encoding="utf-8-sig")
    text = out(root, convert(root, c, SHA, HARD_CAPS))
    assert text.startswith("---\nsource:") and "| a | b |" in text


def test_docx_headings_survive(root, make_docx):
    p = make_docx(root / "raw" / "d.docx", [("h1", "Title"), ("p", "intro"), ("h2", "Section A"), ("p", "body")])
    text = out(root, convert(root, p, SHA, HARD_CAPS))
    assert "# Title" in text and "## Section A" in text


def test_pptx_notes_extracted(root, make_pptx):
    p = make_pptx(root / "raw" / "s.pptx", [("One", ["x"], ""), ("Two", ["y"], "remember the demo")])
    c = convert(root, p, SHA, HARD_CAPS)
    assert c.converter == "markitdown+pptx-notes"
    notes = (root / ".wiki/converted" / f"{SHA}.notes.md").read_text()
    assert "Slide 2" in notes and "remember the demo" in notes and "Slide 1" not in notes
    assert f".wiki/converted/{SHA}.notes.md" in c.outputs


def test_pptx_without_notes_has_no_notes_file(root, make_pptx):
    p = make_pptx(root / "raw" / "s.pptx", [("One", ["x"], "")])
    c = convert(root, p, SHA, HARD_CAPS)
    assert not any(o.endswith(".notes.md") for o in c.outputs)


def test_xlsx_small_inline_large_csv_profile(root, make_xlsx):
    small = [["a", "b", "c"]] + [[str(i), str(i * 2), "x"] for i in range(4)]
    large = [["id", "kind", "n", "note"]] + [[str(i), "k" + str(i % 3), str(i), f"note {i}"] for i in range(299)]
    p = make_xlsx(root / "raw" / "w.xlsx", {"small": small, "large": large})
    c = convert(root, p, SHA, HARD_CAPS)
    text = out(root, c)
    assert c.converter == "sheets" and c.flags == ["large-sheet"]
    assert "## Sheet: small" in text and "| a | b | c |" in text
    assert "## Sheet: large" in text and "rows: 300" in text
    assert "kind: k0, k1, k2" in text
    assert f".wiki/converted/{SHA}.large.csv" in c.outputs
    assert (root / f".wiki/converted/{SHA}.large.csv").exists()


def test_text_pdf_no_flag(root, make_text_pdf):
    p = make_text_pdf(root / "raw" / "t.pdf", ["Quarterly report. " * 20])
    c = convert(root, p, SHA, HARD_CAPS)
    assert c.flags == [] and c.converter in {"markitdown", "pypdf"}
    assert "Quarterly report" in out(root, c)


def test_scanned_pdf_flagged_needs_vision(root, make_scanned_pdf):
    p = make_scanned_pdf(root / "raw" / "s.pdf")
    assert "needs-vision" in convert(root, p, SHA, HARD_CAPS).flags


def test_png_stub_and_flag(root, make_png):
    p = make_png(root / "raw" / "i.png", (64, 48))
    c = convert(root, p, SHA, HARD_CAPS)
    assert c.converter == "image" and c.flags == ["needs-vision"]
    assert "64x48" in out(root, c)


def test_tiny_image_refused(root, make_png):
    p = make_png(root / "raw" / "t.png", (16, 16), rects=())
    with pytest.raises(RefusedSource, match="image too small"):
        convert(root, p, SHA, HARD_CAPS)


def test_never_ingest_refused(root):
    p = root / "raw" / "id_rsa.pem"
    p.write_text("-----BEGIN PRIVATE KEY-----")
    with pytest.raises(RefusedSource):
        convert(root, p, SHA, HARD_CAPS)


def test_unsupported_format_raises(root):
    p = root / "raw" / "x.json"
    p.write_text("{}")
    with pytest.raises(UnsupportedFormat, match=r"\.json is not supported; supported: .*\.pdf"):
        convert(root, p, SHA, HARD_CAPS)
    assert ".pdf" in SUPPORTED


def test_uppercase_extension_routes(root, make_pptx):
    p = make_pptx(root / "raw" / "Arch Overview.PPTX", [("One", ["x"], "n")])
    assert convert(root, p, SHA, HARD_CAPS).converter == "markitdown+pptx-notes"


def test_large_text_split_into_parts(root):
    p = root / "raw" / "big.md"
    p.write_text("".join(f"## Sec {i}\n\n" + ("word " * 2000) + "\n\n" for i in range(20)))
    c = convert(root, p, SHA, HARD_CAPS)
    assert c.chars > 200_000 - 10_000 and c.parts >= 3
    for n in range(1, c.parts + 1):
        assert len(out_part(root, n)) <= 80_000


def out_part(root, n):
    return (root / f".wiki/converted/{SHA}.p{n}.md").read_text()


def test_single_huge_section_hard_split(root):
    p = root / "raw" / "one.md"
    p.write_text("# T\n\n" + "\n\n".join("para " * 3000 for _ in range(15)))
    c = convert(root, p, SHA, HARD_CAPS)
    assert c.parts >= 2
    assert all(len(out_part(root, n)) <= 80_000 for n in range(1, c.parts + 1))


def test_near_duplicate_phash(tmp_path, make_png):
    a = make_png(tmp_path / "a.png", (128, 128), rects=((10, 10, 90, 90),))
    b = make_png(tmp_path / "b.png", (128, 128), rects=((10, 10, 91, 90),))
    c = make_png(tmp_path / "c.png", (128, 128), rects=((0, 0, 127, 20), (0, 100, 20, 127), (60, 40, 120, 70)))
    ha, hb, hc = (images.triage(x).phash for x in (a, b, c))
    assert images.near_duplicate(hb, {"a": ha}) == "a"
    assert images.near_duplicate(hc, {"a": ha}, max_distance=0) is None
    assert images.triage(a).reason is None
