# M8: linked images belong to their page

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** a plain link to a local image, like
`[login.png](attachments/login.png)`, makes the image a member of its page. A
Confluence export of 77 pages and 203 images then counts 82 sources instead
of 191.

**Architecture:** `bundle.py` finds every link through `_IMAGE_TOKEN` and
`_targets`. Finding members, placing them, the split into parts and attaching
images to parts all read from those two, so this is one parser change.

**Tech Stack:** Python, pytest, the `fake_claude` fixture.

**Spec:** [the design](../specs/2026-10-26-llm-wiki-design.md), §5 "Page
bundles".

## Global Constraints

- No new dependencies. Offline tests only: this plan has no real `claude` run.
- Never copy the user's sample sources, their names or their content into the
  repo. Tests use made-up names.
- A plain link counts only when it is relative and names an image type: `.png`,
  `.jpg`, `.jpeg`, `.gif`, `.webp` (`IMAGE_SUFFIXES`).
- Plain links to other types and to remote URLs are normal text: never in
  `ignored`. A plain image link whose file is missing is listed as `missing:`.
- Embed behaviour does not change. `![](d.svg)` is still `not an image type`.

## Review Focus

- A page lists 25 images under one `## Attachments` heading: it splits into
  parts of at most 20, and each image is sent once.
- A page embeds and also lists one image: one member, sent once.
- A page's plain link climbs out of the tree (`../../secret.png`): not a member.
- A linked thumbnail, `[![t](thumb.png)](full.png)`: both are members.
- A plain image link inside a code span or fence: not a member.

---

### Task 1: Parse plain image links

**Files:**

- Modify: `src/llm_wiki/bundle.py` (`_IMAGE_TOKEN`, `_targets`)
- Test: `tests/test_bundle.py`

**Interfaces:**

- Produces: `image_links(text)`, `link_paths(text, defs)` and
  `local_links(text)` keep their signatures and now also return plain image
  links.

- [ ] **Step 1: Write the failing tests**

````python
def test_image_links_plain_links_to_images_count():
    text = (
        "- [Shot](a/one.png)\n- [t](two.JPG 'A title')\n- [r][ref]\n"
        "[![thumb](thumb.png)](full.png)\n\n[ref]: three.png\n"
    )
    assert image_links(text) == [
        "a/one.png", "two.JPG", "three.png", "thumb.png", "full.png",
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
    page = write(
        top / "p.md", "[a](a.png) [gone](gone.png) [out](../../secret.png)\n"
    )
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
````

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_bundle.py -q -k "plain"`

Expected: FAIL. `image_links` returns no plain links yet.

- [ ] **Step 3: Extend `_IMAGE_TOKEN` and `_targets` in `bundle.py`**

- Add two alternatives at the end of `_IMAGE_TOKEN`: `\]\(…\)` with a named
  `plain` target, using the same target pattern as `![alt](target)`, and
  `\]\[(?P<ref>[^\]\n]*)\]`. They match only the closing half of a link, so an
  embed still wins and a thumbnail's outer link is still seen.
- In `_targets`, resolve `plain` through `_clean_target` and `ref` through
  `defs`. Yield either only when `_local_path(target)` has a suffix in
  `IMAGE_SUFFIXES`. Everything else stays silent.
- Update the comment above `_IMAGE_TOKEN` and the module docstring if it no
  longer holds.

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/pytest -q`

Expected: all pass, including the existing
`test_image_links_in_order_outside_code`, where `[plain](doc.pdf)` stays out.

- [ ] **Step 5: Commit**

```bash
git add src/llm_wiki/bundle.py tests/test_bundle.py docs/superpowers/specs/2026-10-26-llm-wiki-design.md docs/superpowers/plans/2026-11-05-llm-wiki-m8-linked-images.md
git commit -m "feat: count images a page only links as part of that page"
```

### Task 2: Prove it through a whole ingest, and document it

**Files:**

- Test: `tests/test_bundle_ingest.py`
- Modify: `README.md` ("Pages with images")
- Modify: `docs/superpowers/specs/2026-10-26-llm-wiki-design.md` (§11)

**Interfaces:**

- Consumes: the parser from Task 1; the `wiki`, `shots` and `fake_claude`
  fixtures and the `ingest` and `plan` helpers in `test_bundle_ingest.py`.

- [ ] **Step 1: Write the test**

`test_ingest_folder_with_an_attachments_list_is_one_source_per_page`:

- A folder holds `manual.md` with `![a](img/a.png)`, then `## Attachments` with
  `- [a.png](img/a.png)` and `- [b.png](img/b.png)` and `- [other](other.md)`,
  and `other.md` with no images. `shots` writes `a` and `b` into `img/`.
- Ingest it with `copy` as in `test_ingest_folder_again_with_one_changed_image`,
  scripting one plan and edit per page.
- Assert `Manifest.load(root).live() == 2`.
- Assert `page_members(root, "raw/<folder>/manual.md")` names `a.png` and
  `b.png`, and both files are in `raw/<folder>/manual/`.
- Assert the `manual.md` plan call in the fake log's `stdin_message` holds 2
  image blocks.
- Assert the run report does not mention `other.md` as ignored.

- [ ] **Step 2: Run it**

Run: `.venv/bin/pytest tests/test_bundle_ingest.py -q -k attachments_list`

Expected: PASS, since Task 1 did the work. If it fails, that is a bug in Task 1:
fix it there.

- [ ] **Step 3: Update `README.md`**

In "Pages with images", say that a plain link like
`[login.png](attachments/login.png)` counts too, so an export's attachment list
stays with its page, and that plain links to pages and websites are not listed.

- [ ] **Step 4: Check the real export, offline**

In a scratch directory with a scratch `LLM_WIKI_HOME` and `GIT_CONFIG_GLOBAL`,
and `LLM_WIKI_CLAUDE=tests/fake_claude/claude`, run
`wiki ingest --yes --wiki <scratch> <the export folder>`.

Expected: no `refused:` line, and `82 sources` in the first report line. Every
plan call fails on the empty fake queue, at no cost; only the count matters.

- [ ] **Step 5: Add the "Done" note and run every check**

Add item 8 under spec §11 with the date, the test count and the offline result.
State that the real run is still open, if it is. Run the six commands under
"Setup and checks" in `AGENTS.md`.

Expected: all clean.

- [ ] **Step 6: Commit**

```bash
git add tests/test_bundle_ingest.py README.md docs/superpowers/specs/2026-10-26-llm-wiki-design.md
git commit -m "docs: explain linked images in the manual and close M8"
```
