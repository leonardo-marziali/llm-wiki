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
  `source_title` and `source_summary`, with type `source-summary`.

## Pages

- Use the frontmatter the plan and the message give: the planned `id`, `title`,
  `type`, `status`, `adr_status` (only on decision pages) and `summary`;
  `review_status: draft` on new pages; `last_verified` and `review_after`
  exactly as listed. On an updated page keep its `review_status` as it is.
- `sources` lists every raw file the page cites. Copy each `sha256` exactly as
  given in the message or the existing page; never compute or change one.
- Cite every claim with a footnote whose definition starts with the `raw/` path,
  then the locator: `[^n]: raw/<path> — <locator>`. Never cite a wiki page or
  `.wiki/converted/`.
- On an update, keep the content the plan does not change, with its footnotes.

## Contradictions

For each contradiction in the plan, keep both claims on the page, each with its
own footnote, lower the page's `confidence` to `low`, describe the conflict
under "Contradictions / open points" and add an entry to
`wiki/open-questions.md`. Add the plan's open questions to
`wiki/open-questions.md` too.

## Repair

When the message lists lint errors, this is a repair: fix exactly those errors
in the allowed files, and change nothing else.

## Reply

When the files are written, reply with the single word `done`.
