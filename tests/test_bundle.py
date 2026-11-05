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


def test_local_links_clash_is_named_by_folder_not_by_order():
    links, _ = local_links("![](a/x.png)\n![](b/x.png)\n![](c/X.png)\n")
    assert [ln.name for ln in links] == ["a-x.png", "b-x.png", "c-x.png"]


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
    found, _ = find_members(page, tree, in_raw=True)
    assert [p for _, p in found] == [member.resolve()]


def test_find_members_in_a_source_tree_prefers_the_real_target(tmp_path):
    tree = tmp_path / "tree"
    page = write(tree / "guide.md", "![](shots/login.png)\n")
    real = touch(tree / "shots/login.png")
    touch(tree / "guide/login.png")  # an unrelated file in a same-named folder
    found, _ = find_members(page, tree)
    assert [p for _, p in found] == [real.resolve()]


def test_removing_a_clashing_link_keeps_the_other_image(tmp_path):
    root = tmp_path
    page = write(root / "raw/page.md", "![](a/login.png)\n![](b/login.png)\n")
    touch(root / "raw/page/a-login.png")
    b = touch(root / "raw/page/b-login.png")
    page.write_text("![](b/login.png)\n", encoding="utf-8")
    members, ignored = page_members(root, "raw/page.md")
    assert [m.path for m in members] == ["raw/page/b-login.png"]
    assert ignored == []
    assert b.is_file()


def test_odd_links_are_reported_and_never_raise(tmp_path):
    tree = tmp_path / "raw"
    long_name = "x" * 300 + ".png"
    page = write(tree / "page.md", f"![](a%00.png)\n![]({long_name})\n")
    found, ignored = find_members(page, tree)
    assert found == []
    assert ignored == ["missing: a%00.png", f"missing: {long_name}"]
    assert member_owners(tmp_path)  # what scan calls


def test_image_links_other_common_forms():
    text = (
        "![a [b] c](nested.png)\n"
        "![over\ntwo lines](two.png)\n"
        "![r][ref]\n![Short]\n![none]\n"
        "<img src=\"html.png\" alt=\"x\"> <IMG alt='y' src='html2.png'/>\n"
        "![[wiki.png]] ![[other.png|300]]\n\n"
        "[ref]: y.png\n[short]: <z w.png> 'title'\n"
    )
    assert image_links(text) == [
        "nested.png",
        "two.png",
        "y.png",
        "z w.png",
        "html.png",
        "html2.png",
        "wiki.png",
        "other.png",
    ]


def test_image_links_html_escapes_are_decoded():
    assert image_links('<img src="a.png?x=1&amp;y=2">') == ["a.png?x=1&y=2"]


def test_split_parts_counts_links_after_a_fence_with_a_hash_line():
    sections = [
        f"## S{i}\n\n```sh\n# run this\n```\n\n![](a{i}.png)\n\n![](b{i}.png)\n\n"
        for i in range(4)
    ]
    body = "".join(sections)
    sent = {f"{x}{i}.png" for x in "ab" for i in range(4)}
    parts = split_parts(body, sent, 2, 10_000)
    assert "".join(parts) == body
    assert [p.count("![") for p in parts] == [2, 2, 2, 2]
    assert all(p.startswith("## S") for p in parts)  # never inside the fence


def test_split_parts_counts_reference_links_defined_elsewhere():
    links = "## A\n\n![][x]\n\n![][y]\n\n## B\n\n![][z]\n\n"
    body = links + "[x]: x.png\n[y]: y.png\n[z]: z.png\n"
    parts = split_parts(body, {"x.png", "y.png", "z.png"}, 1, 10_000)
    assert [p.count("![") for p in parts] == [1, 1, 1]


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


def test_image_links_plain_links_to_images_count():
    text = (
        "- [Shot](a/one.png)\n- [t](two.JPG 'A title')\n- [r][ref]\n"
        "[![thumb](thumb.png)](full.png)\n\n[ref]: three.png\n"
    )
    assert image_links(text) == [
        "a/one.png",
        "two.JPG",
        "three.png",
        "thumb.png",
        "full.png",
    ]


def test_image_links_plain_links_to_anything_else_do_not_count():
    text = (
        "[page](other.md) [doc](d.pdf) [web](https://x.io/a.png) [abs](/a.png)\n"
        "[up][pdf]\n[note] and a[0] and [](#anchor)\n\n[pdf]: d.pdf\n"
    )
    assert image_links(text) == []
    assert local_links(text) == ([], [])


def test_image_links_plain_links_in_code_do_not_count():
    assert image_links("`[a](span.png)`\n```\n[b](fence.png)\n```\n") == []


def test_local_links_embed_and_plain_link_of_one_image_are_one_link():
    links, _ = local_links("![a](x%20y.png)\n\n- [x y.png](x%20y.png)\n")
    assert [ln.path for ln in links] == ["x y.png"]


def test_find_members_plain_links_missing_and_outside(tmp_path):
    top = tmp_path / "top"
    page = write(top / "p.md", "[a](a.png) [gone](gone.png) [out](../../secret.png)\n")
    touch(top / "a.png")
    touch(tmp_path / "secret.png")
    found, ignored = find_members(page, top)
    assert [link.path for link, _ in found] == ["a.png"]
    assert ignored == ["missing: gone.png", "missing: ../../secret.png"]


def test_split_parts_counts_plain_links():
    body = "## Attachments\n\n" + "".join(f"- [{i}.png]({i}.png)\n" for i in range(25))
    sent = {f"{i}.png" for i in range(25)}
    parts = split_parts(body, sent, 20, 80_000)
    assert len(parts) == 2
    assert all(len(image_links(p)) <= 20 for p in parts)
    assert sorted(lk for p in parts for lk in image_links(p)) == sorted(sent)
