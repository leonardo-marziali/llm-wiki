from llm_wiki.bundle import (
    find_members,
    image_links,
    inbox_members,
    local_links,
    member_owners,
    member_rel,
    page_members,
    split_parts,
)


def write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def test_image_links_in_order_outside_code():
    text = (
        "![a](one.png)\n`![code](span.png)`\n```\n![fenced](fence.png)\n```\n"
        '![t](two.png "A title") text ![](<with space.png>)\n[plain](doc.pdf)\n'
    )
    assert image_links(text) == ["one.png", "two.png", "with space.png"]


def test_image_links_keep_parentheses_and_spaces():
    assert image_links("![a](image (1).png)") == ["image (1).png"]


def test_local_links_names_and_ignored():
    links, ignored = local_links(
        "![](https://x.io/a.png)\n![](//cdn/b.png)\n![](data:image/png;base64,AA)\n"
        "![](/abs/c.png)\n![](d.svg)\n![](Sub/My%20Shot.PNG?version=1&x=2#frag)\n"
    )
    assert [(ln.path, ln.name) for ln in links] == [("Sub/My Shot.PNG", "my-shot.png")]
    assert ignored == [
        "remote: https://x.io/a.png",
        "remote: //cdn/b.png",
        "remote: data:image/png;base64,AA",
        "remote: /abs/c.png",
        "not an image type: d.svg",
    ]


def test_local_links_confluence_name_and_dedupe():
    links, _ = local_links(
        "![](attachments/1/image2020-1-30%2012%3A5%3A7.png)\n"
        "![](./attachments/1/image2020-1-30%2012%3A5%3A7.png)\n"
    )
    assert [ln.name for ln in links] == ["image2020-1-30-12-5-7.png"]


def test_local_links_clash_gets_a_number():
    links, _ = local_links("![](a/x.png)\n![](b/x.png)\n![](c/X.png)\n")
    assert [ln.name for ln in links] == ["x.png", "x-2.png", "x-3.png"]


def test_member_rel_is_next_to_the_page():
    assert member_rel("raw/docs/guide.md", "x.png") == "raw/docs/guide/x.png"
    assert member_rel("raw/guide.md", "x.png") == "raw/guide/x.png"


def test_find_members_relative_nested_and_missing(tmp_path):
    tree = tmp_path / "tree"
    page = write(
        tree / "sub/page.md",
        "![](shot.png)\n![](../img/deep/two.png)\n![](gone.png)\n",
    )
    one = touch(tree / "sub/shot.png")
    two = touch(tree / "img/deep/two.png")
    found, ignored = find_members(page, tree)
    assert [(ln.name, p) for ln, p in found] == [
        ("shot.png", one.resolve()),
        ("two.png", two.resolve()),
    ]
    assert ignored == ["missing: gone.png"]


def test_find_members_refuses_to_leave_the_tree(tmp_path):
    tree = tmp_path / "tree"
    page = write(tree / "page.md", "![](../out.png)\n![](link.png)\n")
    outside = touch(tmp_path / "out.png")
    (tree / "link.png").symlink_to(outside)
    found, ignored = find_members(page, tree)
    assert found == []
    assert ignored == ["missing: ../out.png", "missing: link.png"]


def test_find_members_prefers_the_page_folder(tmp_path):
    tree = tmp_path / "raw"
    page = write(tree / "page.md", "![](x.png)\n")
    touch(tree / "x.png")
    member = touch(tree / "page/x.png")
    found, _ = find_members(page, tree)
    assert [p for _, p in found] == [member.resolve()]


def test_page_members_from_raw(tmp_path):
    root = tmp_path
    write(root / "raw/page.md", "![](attachments/x.png)\n![](y.png)\n![](z.png)\n")
    touch(root / "raw/page/x.png")
    touch(root / "raw/y.png")
    members, ignored = page_members(root, "raw/page.md")
    assert [(m.path, m.name) for m in members] == [
        ("raw/page/x.png", "x.png"),
        ("raw/y.png", "y.png"),
    ]
    assert ignored == ["missing: z.png"]
    assert page_members(root, "raw/other.pdf") == ([], [])
    assert page_members(root, "raw/gone.md") == ([], [])


def test_member_owners_image_linked_by_two_pages(tmp_path):
    root = tmp_path
    write(root / "raw/a.md", "![](shared.png)\n![](later.png)\n")
    write(root / "raw/b.md", "![](shared.png)\n")
    write(root / "raw/.hidden/c.md", "![](../shared.png)\n")
    touch(root / "raw/shared.png")
    owners = member_owners(root)
    assert owners["raw/shared.png"] == ["raw/a.md", "raw/b.md"]
    assert owners["raw/a/shared.png"] == ["raw/a.md"]
    assert owners["raw/a/later.png"] == ["raw/a.md"]  # may not exist yet
    assert owners["raw/b/shared.png"] == ["raw/b.md"]


def test_inbox_members_marks_files_and_targets(tmp_path):
    inbox = tmp_path / "inbox"
    page = write(inbox / "docs/page.md", "![](img/a.png)\n![](../shared.png)\n")
    other = write(inbox / "other.md", "![](shared.png)\n")
    alone = touch(inbox / "alone.png")
    a = touch(inbox / "docs/img/a.png")
    shared = touch(inbox / "shared.png")
    pairs = [
        (alone, "raw/alone.png"),
        (a, "raw/docs/img/a.png"),
        (page, "raw/docs/page.md"),
        (other, "raw/other.md"),
        (shared, "raw/shared.png"),
    ]
    by_page = inbox_members(inbox, pairs)
    assert by_page == {
        page: [
            (a.resolve(), "raw/docs/page/a.png"),
            (shared.resolve(), "raw/docs/page/shared.png"),
        ],
        other: [(shared.resolve(), "raw/other/shared.png")],
    }


def test_split_parts_runs_of_headings_within_the_image_limit():
    body = "".join(f"## H{i}\n\n![](i{i}.png)\n\ntext\n\n" for i in range(5))
    parts = split_parts(body, {f"i{i}.png" for i in range(5)}, 2, 1000)
    assert [p.count("![") for p in parts] == [2, 2, 1]
    assert "".join(parts) == body
    assert all(p.startswith("## ") for p in parts)


def test_split_parts_splits_inside_a_crowded_heading():
    body = "# One\n\n" + "".join(f"![](i{i}.png)\n\n" for i in range(5))
    parts = split_parts(body, {f"i{i}.png" for i in range(5)}, 2, 1000)
    assert [p.count("![") for p in parts] == [2, 2, 1]
    assert "".join(parts) == body


def test_split_parts_splits_inside_a_crowded_line():
    body = "".join(f"![](i{i}.png) " for i in range(5)) + "\n"
    parts = split_parts(body, {f"i{i}.png" for i in range(5)}, 2, 1000)
    assert [p.count("![") for p in parts] == [2, 2, 1]
    assert "".join(parts) == body


def test_split_parts_counts_only_new_images_to_send():
    body = "## A\n\n![](a.png) ![](b.png)\n\n## B\n\n![](a.png) ![](tiny.png)\n"
    assert split_parts(body, {"a.png", "b.png"}, 2, 1000) == [body]


def test_split_parts_honours_the_char_limit():
    body = "## A\n\n" + "x" * 50 + "\n\n## B\n\n" + "y" * 50 + "\n"
    parts = split_parts(body, set(), 2, 70)
    assert len(parts) == 2
    assert "".join(parts) == body
