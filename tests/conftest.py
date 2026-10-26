import pytest


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    monkeypatch.setenv("LLM_WIKI_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LLM_WIKI_PATH", raising=False)


@pytest.fixture
def make_docx():
    def make(path, headings_and_paras):
        """Items are ("h1"|"h2"|"p", text)."""
        import docx

        doc = docx.Document()
        for kind, text in headings_and_paras:
            if kind == "p":
                doc.add_paragraph(text)
            else:
                doc.add_heading(text, level=int(kind[1:]))
        doc.save(str(path))
        return path

    return make


@pytest.fixture
def make_pptx():
    def make(path, slides):
        """slides = [(title, bullets, notes)]; notes may be empty."""
        from pptx import Presentation

        prs = Presentation()
        for title, bullets, notes in slides:
            slide = prs.slides.add_slide(prs.slide_layouts[1])
            slide.shapes.title.text = title
            slide.placeholders[1].text_frame.text = "\n".join(bullets)
            if notes:
                slide.notes_slide.notes_text_frame.text = notes
        prs.save(str(path))
        return path

    return make


@pytest.fixture
def make_xlsx():
    def make(path, sheets):
        import openpyxl

        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for name, rows in sheets.items():
            ws = wb.create_sheet(name)
            for row in rows:
                ws.append(row)
        wb.save(str(path))
        return path

    return make


@pytest.fixture
def make_csv():
    def make(path, rows, encoding="utf-8"):
        import csv

        with open(path, "w", newline="", encoding=encoding) as fh:
            csv.writer(fh).writerows(rows)
        return path

    return make


@pytest.fixture
def make_text_pdf():
    def make(path, pages):
        """Minimal hand-written PDF: one Helvetica Tj text object per page."""
        n = len(pages)
        objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
        kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
        objs[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode()
        font = 3 + 2 * n
        for i, text in enumerate(pages):
            esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream = f"BT /F1 12 Tf 72 720 Td ({esc}) Tj ET".encode()
            objs[3 + 2 * i] = (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {4 + 2 * i} 0 R >>"
            ).encode()
            objs[4 + 2 * i] = f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        objs[font] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
        out = bytearray(b"%PDF-1.4\n")
        offsets = {}
        for num in sorted(objs):
            offsets[num] = len(out)
            out += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
        xref = len(out)
        size = max(objs) + 1
        out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
        for num in range(1, size):
            out += f"{offsets[num]:010d} 00000 n \n".encode()
        out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        path.write_bytes(bytes(out))
        return path

    return make


@pytest.fixture
def make_png():
    def make(path, size=(64, 64), rects=((8, 8, 40, 40),)):
        from PIL import Image, ImageDraw

        img = Image.new("RGB", size, "white")
        draw = ImageDraw.Draw(img)
        for rect in rects:
            draw.rectangle(rect, fill="black")
        img.save(str(path))
        return path

    return make


@pytest.fixture
def make_scanned_pdf():
    def make(path):
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (400, 400), "white")
        ImageDraw.Draw(img).rectangle((50, 50, 300, 300), fill="gray")
        img.save(str(path), "PDF")
        return path

    return make
