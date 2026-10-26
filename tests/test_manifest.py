import hashlib
from datetime import date
from pathlib import Path

from llm_wiki.manifest import MANIFEST, Manifest, SourceEntry, category_for, sha256_file


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _entry(path: str, sha: str, status="ingested", **kw) -> SourceEntry:
    return SourceEntry(
        path=path, sha256=sha, size=1, mime="text/plain", category="misc",
        first_seen=date(2026, 10, 26), status=status, **kw,
    )


def test_sha256_file_is_lowercase_hex(tmp_path):
    p = _write(tmp_path, "a.txt", "hello")
    assert sha256_file(p) == hashlib.sha256(b"hello").hexdigest()


def test_empty_manifest_when_missing(tmp_path):
    m = Manifest.load(tmp_path)
    assert m.entries == {}


def test_scan_classifies_new_changed_unchanged_deleted(tmp_path):
    keep = _write(tmp_path, "raw/docs/keep.md", "keep")
    chg = _write(tmp_path, "raw/docs/chg.md", "v1")
    gone = _write(tmp_path, "raw/gone.md", "gone")
    m = Manifest.load(tmp_path)
    for p, rel in ((keep, "raw/docs/keep.md"), (chg, "raw/docs/chg.md"), (gone, "raw/gone.md")):
        m.record(_entry(rel, sha256_file(p)))
    gone.unlink()
    chg.write_text("v2")
    _write(tmp_path, "raw/new.md", "new")
    _write(tmp_path, "raw/.gitkeep", "")
    _write(tmp_path, "raw/.hidden", "x")
    r = m.scan(tmp_path)
    assert r.new == ["raw/new.md"]
    assert r.changed == ["raw/docs/chg.md"]
    assert r.unchanged == ["raw/docs/keep.md"]
    assert r.deleted == ["raw/gone.md"]


def test_scan_is_idempotent_after_record(tmp_path):
    _write(tmp_path, "raw/a.md", "a")
    _write(tmp_path, "raw/sub/b.md", "b")
    m = Manifest.load(tmp_path)
    r = m.scan(tmp_path)
    assert r.new == ["raw/a.md", "raw/sub/b.md"]
    for rel in r.new:
        p = tmp_path / rel
        m.record(_entry(rel, sha256_file(p)))
    r2 = m.scan(tmp_path)
    assert (r2.new, r2.changed, r2.deleted) == ([], [], [])
    assert r2.unchanged == ["raw/a.md", "raw/sub/b.md"]


def test_category_for():
    assert category_for("raw/sub/b.md") == "sub"
    assert category_for("raw/a.md") == "misc"


def test_quarantined_and_failed_with_same_sha_are_unchanged(tmp_path):
    p = _write(tmp_path, "raw/f.md", "f")
    m = Manifest.load(tmp_path)
    m.record(_entry("raw/f.md", sha256_file(p), status="failed"))
    assert m.scan(tmp_path).unchanged == ["raw/f.md"]
    m.record(_entry("raw/f.md", sha256_file(p), status="quarantined"))
    assert m.scan(tmp_path).unchanged == ["raw/f.md"]


def test_quarantined_missing_file_is_not_deleted(tmp_path):
    (tmp_path / "raw").mkdir()
    m = Manifest.load(tmp_path)
    m.record(_entry("raw/q.md", "0" * 64, status="quarantined"))
    r = m.scan(tmp_path)
    assert r.deleted == []


def test_pages_for_reverse_lookup(tmp_path):
    m = Manifest.load(tmp_path)
    m.record(_entry("raw/a.md", "a" * 64, pages_touched=["p1.md", "p2.md"]))
    assert m.pages_for("raw/a.md") == ["p1.md", "p2.md"]
    assert m.pages_for("raw/unknown.md") == []


def test_save_is_deterministic(tmp_path):
    m = Manifest.load(tmp_path)
    m.record(_entry("raw/b.md", "b" * 64))
    m.record(_entry("raw/a.md", "a" * 64, redactions={"z": 1, "a": 2}))
    m.save(tmp_path)
    first = (tmp_path / MANIFEST).read_bytes()
    m.save(tmp_path)
    assert (tmp_path / MANIFEST).read_bytes() == first
    assert first.endswith(b"\n")
    assert b'"first_seen": "2026-10-26"' in first
    assert Manifest.load(tmp_path).entries == m.entries


def test_mark_deleted_keeps_entry(tmp_path):
    m = Manifest.load(tmp_path)
    m.record(_entry("raw/a.md", "a" * 64))
    m.mark_deleted("raw/a.md")
    assert m.entries["raw/a.md"].status == "deleted"
    assert m.scan(tmp_path).deleted == []
