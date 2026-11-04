import hashlib
from datetime import date
from pathlib import Path

import pytest

from llm_wiki.errors import ConfigError
from llm_wiki.manifest import (
    MANIFEST,
    BundleMember,
    Manifest,
    SourceEntry,
    SourceStatus,
    category_for,
    sha256_file,
)


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _entry(path: str, sha: str, status: SourceStatus = "ingested", **kw) -> SourceEntry:
    return SourceEntry(
        path=path,
        sha256=sha,
        size=1,
        mime="text/plain",
        category="misc",
        first_seen=date(2026, 10, 26),
        status=status,
        **kw,
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
    for p, rel in (
        (keep, "raw/docs/keep.md"),
        (chg, "raw/docs/chg.md"),
        (gone, "raw/gone.md"),
    ):
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


def _raw_manifest(root: Path, text: str) -> None:
    (root / ".wiki").mkdir(exist_ok=True)
    (root / MANIFEST).write_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '{"version": 1, "sources": {"raw/a.md": {"path": "raw/a.md"}}}',
        '{"version": 2, "sources": {}}',
        '{"sources": {}}',
    ],
)
def test_load_bad_manifest_raises_config_error(tmp_path, text):
    _raw_manifest(tmp_path, text)
    with pytest.raises(ConfigError, match=r"manifest\.json"):
        Manifest.load(tmp_path)


def test_reappearing_deleted_path_is_new(tmp_path):
    p = _write(tmp_path, "raw/a.md", "a")
    m = Manifest.load(tmp_path)
    m.record(_entry("raw/a.md", sha256_file(p), status="deleted"))
    assert m.scan(tmp_path).new == ["raw/a.md"]


def test_mark_deleted_unknown_path_raises(tmp_path):
    manifest = Manifest.load(tmp_path)
    with pytest.raises(ConfigError, match=r"raw/x\.md"):
        manifest.mark_deleted("raw/x.md")


def test_refused_entry_not_counted_live(tmp_path):
    # parked in M3: a refused credential sent back to inbox held a cap slot
    m = Manifest.load(tmp_path)
    m.entries["raw/key.pem"] = SourceEntry(
        path="raw/key.pem",
        sha256="0" * 64,
        size=1,
        mime="application/x-pem-file",
        category="other",
        first_seen=date(2026, 10, 28),
        status="failed",
        flags=["refused"],
    )
    assert m.live() == 0
    assert not m.is_live("raw/key.pem")


def _bundle(root, make_png):
    """raw/page.md with two members, recorded as a bundle."""
    (root / "raw/page").mkdir(parents=True)
    page = root / "raw/page.md"
    page.write_text("![](a.png)\n![](b.png)\n", encoding="utf-8")
    make_png(root / "raw/page/a.png")
    make_png(root / "raw/page/b.png", rects=((0, 0, 9, 9),))
    m = Manifest()
    m.record(
        SourceEntry(
            path="raw/page.md",
            sha256=sha256_file(page),
            size=page.stat().st_size,
            mime="text/markdown",
            category="misc",
            first_seen=date(2026, 1, 1),
            status="ingested",
            members=[
                BundleMember(path=p, sha256=sha256_file(root / p))
                for p in ("raw/page/a.png", "raw/page/b.png")
            ],
        )
    )
    return m


def test_scan_skips_members_and_counts_a_bundle_once(tmp_path, make_png):
    m = _bundle(tmp_path, make_png)
    res = m.scan(tmp_path)
    assert res.unchanged == ["raw/page.md"]
    assert res.new == []
    assert m.live() == 1
    m.save(tmp_path)
    assert Manifest.load(tmp_path).entries["raw/page.md"].members == (
        m.entries["raw/page.md"].members
    )


def test_scan_new_page_claims_its_new_images(tmp_path, make_png):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw/page.md").write_text("![](a.png)\n", encoding="utf-8")
    make_png(tmp_path / "raw/a.png")
    make_png(tmp_path / "raw/alone.png")
    assert Manifest().scan(tmp_path).new == ["raw/alone.png", "raw/page.md"]


def test_scan_changed_member_changes_the_bundle(tmp_path, make_png):
    m = _bundle(tmp_path, make_png)
    make_png(tmp_path / "raw/page/a.png", rects=((1, 1, 60, 60),))
    res = m.scan(tmp_path)
    assert res.changed == ["raw/page.md"]
    assert m.members_changed(tmp_path, "raw/page.md")


def test_scan_missing_or_added_member_changes_the_bundle(tmp_path, make_png):
    m = _bundle(tmp_path, make_png)
    (tmp_path / "raw/page/a.png").unlink()
    assert m.scan(tmp_path).changed == ["raw/page.md"]
    make_png(tmp_path / "raw/page/a.png")
    assert m.scan(tmp_path).changed == []
    with (tmp_path / "raw/page.md").open("a", encoding="utf-8") as fh:
        fh.write("![](c.png)\n")
    m.record(
        m.entries["raw/page.md"].model_copy(
            update={"sha256": sha256_file(tmp_path / "raw/page.md")}
        )
    )
    assert m.scan(tmp_path).changed == []  # c.png is not there yet
    make_png(tmp_path / "raw/page/c.png")
    assert m.scan(tmp_path).changed == ["raw/page.md"]


def test_scan_entry_from_before_bundles_is_not_changed(tmp_path, make_png):
    m = _bundle(tmp_path, make_png)
    old = m.entries["raw/page.md"].model_copy(update={"members": None})
    m.record(old)
    assert m.scan(tmp_path).unchanged == ["raw/page.md"]
    assert not m.members_changed(tmp_path, "raw/page.md")


def test_scan_image_with_its_own_entry_stays_a_source(tmp_path, make_png):
    m = _bundle(tmp_path, make_png)
    png = tmp_path / "raw/page/a.png"
    m.record(
        SourceEntry(
            path="raw/page/a.png",
            sha256=sha256_file(png),
            size=png.stat().st_size,
            mime="image/png",
            category="page",
            first_seen=date(2026, 1, 1),
            status="ingested",
        )
    )
    assert m.scan(tmp_path).unchanged == ["raw/page.md", "raw/page/a.png"]
    assert m.live() == 2


def test_scan_recorded_member_of_a_gone_page_is_no_source(tmp_path, make_png):
    m = _bundle(tmp_path, make_png)
    (tmp_path / "raw/page.md").unlink()
    m.record(m.entries["raw/page.md"].model_copy(update={"status": "quarantined"}))
    assert m.scan(tmp_path).new == []
