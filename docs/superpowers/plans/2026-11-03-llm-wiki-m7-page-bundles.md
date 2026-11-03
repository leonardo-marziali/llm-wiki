# M7: page bundles and multi-wiki lookup

**Goal:** a Markdown page and the local images it links count as one source, so
a component of 363 files (77 pages, 203 images) fits in one wiki of about 80
sources. Agents look up 1–3 wikis and merge the results.

**Spec:** [the design](../specs/2026-10-26-llm-wiki-design.md), §1, §3, §5, §7.

## Architecture

- `bundle.py` parses image links and resolves them. It knows nothing of the
  manifest, so `manifest.py` can use it.
- A member's raw path is a pure function of the page's path and text:
  `raw/<page path without .md>/<slugged file name>`. Nothing has to remember
  where a page's images went: raw pages resolve their members from their own
  text, also after a stopped run or for a page dropped alone.
- `Manifest.scan` skips members, and reports a page as changed when a member
  changed. Members have no manifest entry of their own, so `live()` counts each
  bundle once.
- `prepare_source` triages the members, picks what each part sends, and records
  the member list. `plan_source` sends the part's images as image blocks.

## Binding rulings

- A bundle is one Markdown source plus the local images it links. Remote URLs
  and missing files are ignored and listed in the run report.
- Images are bundle members, not sources. The source cap counts bundles.
- The manifest entry lists members with `path` and `sha256`. A bundle is changed
  when the page or any member changes.
- A file both linked and dropped alone is a member only. An image linked by two
  pages is a member of both, with one copy per page.
- `images.triage` drops tiny images; `images.near_duplicate` sends
  near-duplicates once within a bundle.
- `max_images_per_part = 20`. The API allows 600 images and 128 MB per request,
  so its limit does not bind: 20 keeps a plan call inside its $1 budget, and it
  is the documented point above which stricter image dimensions apply.
- Citations name the page as the source and the image in the locator. Image
  facts stay `confidence: low`.
- Prompt changes go into `prompts/*.md`, not `templates/AGENTS.md`.

## Tasks

### Task 1: Spec and plan

- [x] Update spec §1, §3, §5 and "Area wikis"; write this plan; branch.

### Task 2: Link parsing and bundle discovery

- [ ] `bundle.py`: `image_links`, `local_links`, `find_members`, `member_rel`,
      `page_members`, `member_owners`, `inbox_members`.
- [ ] Tests: relative and nested paths, URL-encoded names, missing files, remote
      URLs, code blocks, tree escapes, an image linked by two pages, a member
      also dropped alone.

### Task 3: Manifest and caps

- [ ] `BundleMember`, `SourceEntry.members`, `Manifest.members_changed`, scan
      skips members, `max_images_per_part`.
- [ ] Tests: change detection on a member, bundles counted once, pre-M7 entries
      not reported as changed, status counts.

### Task 4: Plan call with images

- [ ] Intake places members; `Prepared.images` replaces `Prepared.image`;
      triage, de-duplication and part splitting; `plan_source` sends the part's
      images.
- [ ] Failure paths keep members consistent: quarantine, trust, a failed changed
      source, deletion, the cap, reject.

### Task 5: Prompt and lint

- [ ] Locator rule in `prompts/plan.md` and `prompts/edit.md`; `validate_plan`
      checks image locators; lint accepts them.

### Task 6: Multi-wiki lookup

- [ ] Check that repeated `--wiki` and the skill text cover the ruling.

### Task 7: End-to-end

- [ ] Extend `tests/test_e2e_m3.py`: a page with 3 images makes one plan call
      with 3 image blocks; a re-ingest after one image changes.

### Task 8: README and the "Done" note

- [ ] README, spec §11, the checks, `wiki doctor`, one paid run after asking.
