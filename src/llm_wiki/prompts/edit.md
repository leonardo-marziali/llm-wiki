# Edit instructions

You are applying one validated ingest plan to this wiki. Your working directory
is the wiki root. The message gives you the plan, the source to read, the exact
files you may change and the dates to write. Follow `AGENTS.md` above for
everything this section does not say.

## Scope

- Apply the plan exactly: write every planned page, with every claim of the plan
  on it, and nothing the plan does not hold.
- Change only the files in the allowed list. The tool reverts any other change
  and reports it. Never touch `raw/`, `AGENTS.md`, `CLAUDE.md`, `.claude/`,
  `wiki.toml`, `.wiki/`, the indexes or `log.md`.
- Read the converted source files the message names to word the claims. Read
  other wiki pages only to link them or to keep their existing content.
- Create the source page, or update it when it exists, from the plan's
  `source_title` and `source_summary`, with type `source-summary`. For a later
  part of a split source, keep the page's title and add this part to its summary
  and outline.
- A page the allowed list marks `verify` is not in the plan: the changed source
  backs it. Do only what its line says, and nothing else on that page.

## Pages

- Use the frontmatter the plan and the message give: the planned `id`, `title`,
  `type`, `status`, `adr_status` (only on decision pages) and `summary`;
  `review_status: draft` on new pages; `last_verified` and `review_after`
  exactly as listed. On an updated page keep its `review_status` as it is.
- `sources` lists every raw file the page cites. Copy each `sha256` exactly as
  given in the message or the existing page; never compute or change one.
- Cite every claim with a footnote whose definition starts with the `raw/` path,
  then the locator: `[^n]: raw/<path> — <locator>`. Never cite a wiki page or
  `.wiki/converted/`. Keep each claim's own locator from the plan; never merge
  claims with different locators under one footnote.
- Keep one blank line between the text and the first footnote definition.
- Cite every bullet under "Contradictions / open points" with the locator it is
  about. A gap with nothing in the source to cite goes only in
  `wiki/open-questions.md`, not on the page.
- Write identifiers exactly and in full as the plan gives them, and each
  `A -> B: label` flow claim as its own bullet.
- Fill `related`, and the "Related" list, with the existing pages on the same
  system, workflow or data; read the section indexes to find them.
- On an update, keep the content the plan does not change, with its footnotes.

## Contradictions

For each contradiction in the plan, keep both claims on the page, each with its
own footnote, lower the page's `confidence` to `low`, describe the conflict
under "Contradictions / open points" and add an entry to
`wiki/open-questions.md`. Add the plan's open questions to
`wiki/open-questions.md` too.

## Deleted sources

When the message says the source was deleted, there is nothing to read, and the
plan's claims say what to do rather than text to write. On each allowed page,
follow `AGENTS.md` "Updates, changed and deleted sources", rules 4 and 5: keep
every claim that cites the deleted path inside a `> [!unverified]` block without
footnotes, remove the path from `sources` with its footnote markers and
definitions, lower `confidence`, and set the status the message gives. Never
delete a page. Add the plan's open questions to `wiki/open-questions.md`.

## Repair

When the message lists lint errors, this is a repair: fix exactly those errors
in the allowed files, and change nothing else.

## Reply

When the files are written, reply with the single word `done`.
