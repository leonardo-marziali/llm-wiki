# llm-wiki — design (v1)

## Context

The user wants a personal, general-purpose **LLM wiki** (Karpathy pattern) with
a software-development lean, implemented in Python in `/workspaces/llm-wiki`
(empty repo). It is grounded in the deep-research report _LLM Wikis for
Coding-Agent Context (Oct 2026)_. A previous project, `kbpipe`
(`/tmp/edp-wiki-bulk-ingestion-pipeline`), was a Confluence-specific headless
bulk pipeline: 8+ stages, 1,780 pages, about $310. That is far above the
research's quality ceiling. Parts of it are reused (see §9), but not its
architecture.

**Intent:** curated knowledge that coding agents (Claude Code in the user's app
repos) read during sessions. Agents must make decisions consistent with
conventions, decisions, architecture and requirements they cannot discover from
code alone. Writing stays cheap to operate: one command, one review gate.

**Decisions made in conversation (the user's):**

- The **CLI drives the LLM**: `wiki <verb>` calls headless `claude -p`
  (subscription login, no API key).
- **Many small wikis**, each capped independently, plus a registry so the CLI
  and agents can pick one or search across all.
- **Ingest = one gate at the end**: one command converts, screens, plans, writes
  on a branch, then shows the diff summary for accept or reject.
- **Consumers = coding agents.** They read files directly through a pointer and
  a read-only lookup skill plus `wiki search`. Reads never cost LLM calls.
- **Write approach C: plan → edit.** A tool-less JSON plan is validated by
  Python, then an edit session restricted to the planned files.
- **No third-party vendor integrations.** No Atlassian, MCP or REST exporters.
  Sources are local files only.
- **Location-agnostic wikis.** Each wiki is either its own git repo or a folder
  inside an existing repository (for example an app or project repo); it can
  live at any path and is versioned wherever the user keeps it. `wiki init`
  never initialises or commits inside a host repo. The CLI never assumes a
  location.
- **The lookup skill is installed globally** (user-level skills). It finds wikis
  through the registry or an environment variable, not through repo-specific
  wiring.
- **The CLI is a normal, publishable Python package:** a wheel with its
  templates and prompts as package data, a versioned release, and an installed
  `wiki` entry point. Nothing depends on this source checkout at runtime.

**Calls I made after the builder review:**

- Each claim's as-is/to-be label is set by the plan call when it routes the
  claim to a section, and Python validates it. `decisions/` pages carry an
  `adr_status` field. The `general` profile gets a `proposals/` section for
  to-be content.
- A "changed source" is a file with the same name dropped into `inbox/` again.
  It replaces `raw/<path>` and the manifest records `supersedes_sha`; git keeps
  the old version. "Immutable" means the LLM never edits `raw/`.
- Screening **redacts** secrets and continues, keeping the key name and
  replacing the value with `‹REDACTED:type›`. Only **injection** hits quarantine
  a file; ordinary shell commands in runbooks are not hits, only text addressed
  to an AI or agent. `wiki ingest --trust <file>` releases a false positive.

## Goals / non-goals / anti-goals

- **Goals:** a cited, governed, git-versioned markdown wiki; very few CLI verbs,
  and the daily loop is one command; quality enforced by hard caps taken from
  the research; agents get small, high-signal context on demand.
- **Non-goals (v1):** vendor integrations, ingesting whole code repos (the
  research says do not restate code), embeddings/vector store, a server, a web
  UI, an `ask` command for humans, multi-user concurrency.
- **Anti-goals (failure even if it "works"):**
  - the wiki cites itself (raw sources must be the only evidence);
  - as-is and to-be facts are mixed;
  - secrets or injected instructions reach pages or agent context;
  - always-loaded context grows (the ETH AGENTS.md study);
  - a kbpipe-style multi-stage pipeline the user has to babysit;
  - silent overwrites of contradicting facts.

## 1. Scale caps (hard limits, from the research)

| Limit                         | Value                                                                         | Research basis                                                                                                |
| ----------------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Sources per wiki              | **100** hard (warn at 80)                                                     | Karpathy: index-first "works … at ~100 sources"                                                               |
| Pages per wiki                | **500** hard (warn at 400)                                                    | Top of the "100–500 pages" heuristic, which requires sharded indexes plus search (both built in from day one) |
| Pages per section `_index.md` | 100 (warn at 80); over that, split the section                                | v2: flat index holds up to about 100–200                                                                      |
| Top-level `index.md`          | < 100 lines; links to section indexes and hubs only                           | Research §Navigation                                                                                          |
| Page size                     | ≤ 400 lines; split into a hub plus sub-pages                                  | Research §Page contract                                                                                       |
| Pages touched per ingest      | ≤ 15 (typical 3–15)                                                           | Karpathy "10–15 pages"                                                                                        |
| Agent lookup budget           | ≤ 5 pages, about 8k tokens                                                    | Research kb-context                                                                                           |
| Always-loaded context         | skill description only (≤ 1,024 chars); optional CLAUDE.md pointer ≤ 15 lines | ETH study / research guardrails                                                                               |

- `wiki.toml` may **lower** these caps but never raise them; the hard maxima are
  constants in `caps.py`.
- **At cap:** ingest still allows updates to existing pages, but the plan
  validator rejects new pages. The CLI then suggests `wiki consolidate` or
  starting a new wiki.
- **Large single sources:** a converted text over about 80k chars is split into
  heading-based parts (`#p1`, `#p2`, …), each planned in turn. _(My call; the
  threshold is tunable.)_

## 2. CLI surface (Typer, entry point `wiki`)

The daily loop is `wiki ingest`; everything else is occasional.

| Command                                                        | LLM?                  | What it does                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| -------------------------------------------------------------- | --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `wiki init <path> [--name N] [--profile software\|general]`    | no                    | When `path` is not a wiki yet: creates the folder (plus `git init` and an initial commit, unless `path` is inside an existing git repo, where the files are left for the user to commit), writes `AGENTS.md`, `CLAUDE.md`, `wiki.toml`, the section skeleton and empty indexes, and runs lint (must pass). In every case it registers the wiki in the registry. Idempotent, so running it on an existing or cloned wiki just registers it.                                                                                                                                                                                                                                                                                              |
| `wiki ingest [PATH…] [--note TEXT] [--wiki W] [--yes]`         | yes                   | With no paths, processes `inbox/` plus anything new, changed or deleted under `raw/`. With paths, copies them into `raw/` first. `--note` saves the text as `raw/notes/<date>-<slug>.md` and ingests it (used for capturing decisions from coding sessions). One gate at the end; `--yes` auto-accepts when lint passes.                                                                                                                                                                                                                                                                                                                                                                                                                |
| `wiki search <query> [--wiki W\|--all] [--status …] [--top N]` | no                    | BM25-lite ranked results (path, id, title, summary, status, last_verified). Used by agents.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `wiki status [--wiki W]`                                       | no                    | Usage against caps, stale pages, pending inbox items, quarantine, last 5 log entries.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `wiki lint [--deep] [--fix] [--yes] [--strict] [--wiki W]`     | `--deep`/`--fix` only | Deterministic checks always run. `--deep` adds a sampled LLM semantic pass whose findings are warnings, and writes `.wiki/reports/lint-<date>.md`. `--fix` runs plan → edit on the page-contract findings, one job per page, on a `wiki/fix-<date>` branch with the same single gate; `--yes` accepts only a run whose lint passes. Findings no model may touch (secrets, injections, stale pages, sha mismatches, caps on sources, the log) are listed with a hint.                                                                                                                                                                                                                                                                    |
| `wiki consolidate [--wiki W] [--yes]`                          | yes                   | Relink pages that link to retired ones, merge duplicates (two live pages of one status outside to-be sections; one becomes a stub with `superseded_by`), split oversized pages (when the cap leaves room for one new page), and promote implemented to-be facts into as-is pages, in that order: Opus plan → edit per job, at most 10 jobs (3 promotes) a run, on a `wiki/consolidate-<date>` branch with the same single gate. Then retired (deprecated or superseded) pages that nothing kept links to are archived deterministically, by `git mv` to `<section>/_archive/`; the summary names each page that waits and what links it. Findings that got no job are listed with a hint. `--yes` accepts only a run whose lint passes. |
| `wiki skill install [--user\|--project DIR] [--pointer]`       | no                    | Installs the read-only `wiki-lookup` skill, by default to the user-level skills dir. `--pointer` also writes a marked block of ≤ 15 lines into `DIR/CLAUDE.md`. Idempotent; prints the installed version.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `wiki doctor`                                                  | tiny probe            | Checks the `claude` version and flags, login, model ids, settings isolation (no user memory leaking in), and the optional tools (Docling, gitleaks, ripgrep).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |

- **Wiki resolution order:** `--wiki` (a name or a path) → the current directory
  is inside a wiki → the registry default.
- **Registry:**
  - The registry is `$LLM_WIKI_HOME/wikis.toml`, defaulting to the platform
    user-config dir. It maps name → path and holds the default wiki.
  - `LLM_WIKI_PATH` (an os.pathsep-separated list of wiki dirs) adds wikis with
    no registration step. This suits setups where wikis are provisioned rather
    than created.
  - `wiki status --all` lists every known wiki with its path and cap usage.

## 3. Wiki layout (its own git repo, or a folder inside an existing repo)

```text
<wiki>/
├── AGENTS.md          schema + rules (rendered from template at init; tool-agnostic)
├── CLAUDE.md          "@AGENTS.md"
├── wiki.toml          name, description (one line: what belongs here), profile, sections, cap overrides (≤ hard max), models
├── inbox/             drop zone (gitignored contents are NOT ignored: inbox is empty after ingest)
├── raw/               IMMUTABLE evidence; ingest moves inbox files to raw/<section-hint>/<slug>.<ext>; raw/notes/ for --note
├── wiki/
│   ├── index.md  log.md  overview.md  glossary.md  open-questions.md
│   ├── <section>/_index.md + pages
│   └── sources/src-<slug>.md      one summary page per raw source
└── .wiki/
    ├── manifest.json            committed; one entry per raw source (path, sha256, size, mime, converter, converted[], pages_touched[], status, flags, first_seen, last_ingested)
    ├── converted/<sha>.md (+ .csv/.profile.md/.notes.md)   committed cache
    ├── quarantine/              flagged sources (gitignored), with a reason file
    ├── reports/                 lint reports
    └── runs/<run-id>/           plan.json, transcripts, validation results (gitignored)
```

**Profiles** set the default sections. _(My call: two profiles.)_

- `software`: `architecture/`, `components/`, `conventions/` (coding, git, PR,
  review, DoD), `decisions/` (ADR-NNNN), `requirements/` (to-be only),
  `operations/` (environments, runbooks), `concepts/`, `sources/`.
- `general`: `concepts/`, `topics/`, `howto/`, `references/`, `proposals/`
  (to-be only), `sources/`.

## 4. Page contract (enforced by lint)

Frontmatter (closed vocabularies):

```yaml
id: conventions.git-flow        # stable dot-path, never reused; filename = last segment
title: …
type: concept|component|system|integration|environment|process|convention|decision|requirement|howto|reference|glossary|source-summary|overview|hub
status: as-is|to-be|deprecated|superseded     # to-be pages live only in the profile's to-be sections (requirements/, proposals/) or decisions/ with adr_status: proposed
adr_status: proposed|accepted|rejected|superseded   # decisions/ only
summary: "1–2 sentences; this IS the index entry"
sources: [{path: raw/…, sha256: …, locator: "§2 / slide 4 / Sheet1!A1:D20"}]
confidence: stated|high|medium|low
review_status: draft|reviewed
last_verified: YYYY-MM-DD
review_after: YYYY-MM-DD        # +90d default, +60d for conventions/operations
supersedes: []  superseded_by: null  related: []  tags: []
```

- **Body:** H1 → **TL;DR** (2–4 lines) → "Rules for agents" (convention/process
  pages only: imperative, testable) → sections → "Contradictions / open points"
  → "Related" → footnotes `[^n]: raw/… — locator`.
- Every claim carries a footnote that points to `raw/` only. Uncited claims sit
  inside `> [!unverified]`.
- **Contradictions:** never overwrite. Keep both claims with citations, lower
  the confidence and add an entry to `open-questions.md`. When the contradiction
  is resolved, strike the losing claim through with "superseded by … on date".
- Ingest may create `review_status: draft` pages. Pages marked `reviewed` change
  only through the gate, and the diff summary highlights such changes.

## 5. `wiki ingest` flow (approach C)

1. **Preflight:** clean git tree on `main`, wiki resolved, `claude` available.
2. **Scan** (`manifest.py`): classify every source as new, changed, unchanged or
   deleted. A no-op ingest exits 0 with "nothing to do".
3. **Convert** (`convert.py`):
   - `.md`/`.txt`: copied as-is.
   - Supported formats are exactly: md/txt (including notes), images, pptx,
     xlsx, csv, docx and PDF.
   - docx, pptx, xlsx, csv: MarkItDown; for pptx, python-pptx also extracts the
     speaker notes.
   - xlsx sheets over 200×30: CSV plus a profile instead of inline tables.
     Every inline or preview table leads with a `(row)` column (the sheet row
     number) under a `range:` line, so a locator can name a cell range; cells
     are clipped at 1,000 characters.
   - PDF: MarkItDown first. If the text layer is thin (< 200 chars/page),
     escalate to Docling when installed; otherwise flag it "needs-vision".
   - Images: no conversion; they are sent to the plan call as image blocks
     (stream-json stdin; the plan call stays tool-less), and the resulting
     facts are `confidence: low`.
   - Unknown types fail loudly.
4. **Screen** (`screen.py`), on the converted text:
   - **Secrets:** the regex set (from kbpipe `secrets.py`, extended) plus
     gitleaks if it is on PATH. Values are redacted in the converted text (key
     name kept, value replaced with `‹REDACTED:type›`). The raw original is kept
     in `raw/` but is the only place a secret can remain, and an optional strict
     mode in `wiki.toml` quarantines instead. Redaction counts appear in the
     gate summary.
   - **Injection heuristics:** text addressing an AI, assistant or agent;
     "ignore previous"-style overrides; requests to run tools or change agent
     config; direction-mark or Unicode-tag characters, or a run of four or
     more zero-width ones (single zero-width spaces, common in exports, are
     stripped before the rules run instead); large base64 blobs of at least
     four distinct characters. A hit
     moves the source to quarantine with a reason file, and the rest of the
     batch continues. `wiki ingest --trust <file>` releases it and records
     `flags: [trusted]` in the manifest.
5. **Branch:** `wiki/ingest-<yyyymmdd>-<slug>`.
6. **For each source, sequentially:**
   - a. **Plan call** (`claude -p`, `--tools ""`, `--json-schema plan.v1`). It
     receives the system prompt (`AGENTS.md` plus the plan instructions), the
     section indexes, the candidate pages (top hits from `search.py` on the
     source's terms, ≤ 8 pages including their TL;DR and full text), and the
     converted source wrapped as untrusted data. The plan lists the pages to
     create or update: id, section, type, status, the claims with locators,
     contradictions found, open questions, and the summary update for
     `src-<slug>`.
   - b. **Validate the plan** (Python): ≤ 15 pages; caps (no new pages at cap);
     ids follow the grammar; as-is/to-be routing matches section and status;
     every locator cites this source or an existing raw path; no edits planned
     to `AGENTS.md`, `CLAUDE.md`, `.claude/` or `raw/`; a whole new source
     plans at least one page, or says why not in its open questions. One retry
     with the errors appended, then the source fails open (reported,
     skipped). A rejected plan's errors are kept in the run dir
     (`<n>-<slug>.plan.errors.json`).
   - c. **Edit session** (`claude -p`, `--tools Read,Edit,Write,Glob,Grep`, cwd
     = wiki, no Bash, no Web, `--permission-mode acceptEdits` or the equivalent
     verified by `doctor`). It receives the validated plan plus the rules, and
     edits pages in place.
   - d. **Post-check:** changed files must be a subset of the planned files plus
     `src-` and `open-questions.md`; anything else is reverted and reported.
     Then frontmatter and citations are validated; any failure gets one repair
     retry, and the lint errors it was given are kept in the run dir
     (`<slug>.repair.findings.json`).
7. **Finalize (deterministic):** `index.py` rebuilds every `_index.md` and
   `index.md`; `lint.py --changed` must pass; the manifest is recorded
   (pages_touched, sha); one parseable entry `## [YYYY-MM-DD] ingest | <title>`
   per source is appended to `log.md`; inbox files are moved into `raw/`;
   commit.
8. **Gate:** prints a diff summary (pages created, updated or reviewed-touched;
   contradictions; unverified and low-confidence claims; quarantined and failed
   sources; cost from the result JSON).
   - `accept` fast-forwards `main` and deletes the branch.
   - `reject` deletes the branch and puts inbox files back.
   - `diff` opens `git diff main…branch` and then re-asks.
   - Nothing is ever pushed.
9. **Changed or deleted sources** use the same flow. Sources are keyed by `raw/`
   path. An inbox file whose name matches an existing raw source (or an explicit
   `wiki ingest --replace raw/<path> <newfile>`) replaces it, with
   `supersedes_sha` recorded. A file removed from `raw/` counts as deleted. The
   plan input also includes `manifest.pages_for(path)` and an old-vs-new
   conversion diff, and the plan may only modify claims that cite that source.
   For a deleted source, its claims become `[!unverified]` and an open-questions
   entry is added; nothing is deleted silently.

## 6. Claude runner (`claude.py`, the only place CLI flags exist)

Based on kbpipe `llm/runner.py` (`build_argv`, `child_env`, `classify`, retries,
usage parsing):

```sh
claude -p --output-format json --model M --system-prompt-file <compiled> --tools <list> --no-session-persistence --setting-sources "" --strict-mcp-config --permission-prompts none --max-budget-usd <cap> [--json-schema S] [--permission-mode acceptEdits --restricted]
```

(the last group only for edit sessions; `--permission-prompts none` denies
instead of hanging; `--restricted` confines edits to the cwd).

- The prompt goes in on stdin. The plan call's cwd is an empty directory; the
  edit session's cwd is the wiki.
- A call with images uses the stream-json variant. `--output-format json`
  becomes `--output-format stream-json --verbose`, `--input-format stream-json`
  follows it, and everything else is unchanged. stdin is then one JSON line,
  `{"type":"user","message":{"role":"user","content":[{"type":"text",...},
  {"type":"image","source":{"type":"base64","media_type":...,"data":...}}]}}`.
  The runner reduces the stdout stream to its last `type:"result"` line (none
  counts as a transient failure) and parses it as usual. Each image is first
  downscaled to a long side of at most 1568 px and re-encoded as PNG, or as JPEG
  (quality 85) when the PNG would exceed 3.75 MB. The copies live in a per-call
  temp dir, outside the call's cwd, and are deleted afterwards. `wiki doctor`'s
  `vision` probe checks that a random word drawn in a PNG comes back from a plan
  call.
- The child env strips API-key variables and disables auto memory
  (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`). `--bare` is never passed.
- Error handling: transient errors get 3 jittered retries; usage limits pause
  with a clear message; auth failures print "run `claude /login`"; schema
  failures get one retry with the error. Edit sessions are never retried
  automatically; ingest's post-check and rollback handle a failed edit. The
  reported cost is the sum over all attempts.
- `--system-prompt-file` is accepted but missing from `claude --help`, so
  `wiki doctor` does not look for it there. Instead, its model probe proves the
  flag works: the model must echo a token that appears only in the system prompt
  file.
- The system prompt is byte-stable (`AGENTS.md` first) so prompt caching works.
- Default models (`~/.config/llm-wiki/config.toml`, overridable per wiki):
  `claude-sonnet-5-5` for plan and edit, `claude-opus-5-5` for `consolidate` and
  `lint --deep`. _(My call.)_
- Per-call budget caps are configurable; the default is $1 for a plan call, $2
  for an edit call and $3 for a `deep` call.

## 7. Agent consumption (read side, zero LLM cost from the CLI)

- `wiki skill install` writes `wiki-lookup/SKILL.md`, rendered from a package
  template and stamped with the CLI version, to the user-level skills dir by
  default. Being global, it works in every repo with no per-repo wiring.
  - It is model-invoked, with a third-person what-and-when description (≤ 1,024
    chars). The description is the only always-loaded cost.
  - `allowed-tools: Read, Grep, Glob, Bash(wiki status *), Bash(wiki search *)`;
    `disallowed-tools: Write, Edit`.
  - Steps: `wiki status --all --format json` (lists the known wikis and their
    one-line descriptions, which decides which wiki applies) → read that wiki's
    `index.md` → the relevant `_index.md` → `wiki search` → read ≤ 5 pages,
    TL;DR first.
- The optional `--pointer` CLAUDE.md block (≤ 15 lines) is only for repos that
  should name a preferred wiki: "for this repo prefer wiki `<name>`; use
  wiki-lookup before planning changes, creating branches/commits/PRs or touching
  build/deploy config."
  - Answers label each fact [AS-IS] or [TO-BE] and give the page link, the raw
    footnote, the confidence and `last_verified`. Pages past `review_after` are
    flagged STALE. If the wiki lacks the answer, the skill says "not in wiki"
    and suggests `wiki ingest --note "…"` for the user to run.
- Agents never write. Capturing knowledge stays a user command.

## 8. Lint (`lint.py`)

Deterministic checks:

- frontmatter schema and closed vocabularies; id/filename agreement; duplicate
  ids;
- titles that look like duplicates (rapidfuzz ≥ 90);
- broken relative links; orphans (excluding indexes); pages missing from
  `_index`;
- footnotes pointing outside `raw/`, or to missing raw files; sha mismatch with
  the manifest; a footnote definition glued to the text above it
  (`footnote-spacing`, warning);
- uncited paragraphs (heuristic warning);
- pages over 400 lines; caps (sources, pages, section size);
- `review_after` in the past (stale);
- as-is pages citing only to-be sources or under to-be sections, and the
  reverse;
- `superseded` pages without `superseded_by`;
- screen patterns on pages; `log.md` format; `index.md` over 100 lines.

`--deep` uses an LLM to sample pages for contradictions, stale claims, concepts
that are mentioned three or more times but have no page, and missing
cross-links.

- **Sampling:** one tool-less call. The overview and glossary always go in, then
  the other pages, least recently verified first, until about 120k chars. Pages
  with a secret or injection finding are never sent. The report's `deep:` line
  says how many pages were sampled out of how many.
- **Severity:** deep findings (`deep-contradiction`, `deep-stale-claim`,
  `deep-missing-page`, `deep-missing-link`) are always `warn`, so `--deep` keeps
  lint's exit codes.
- **Dropped findings:** a finding that names pages outside the sample, has the
  wrong number of pages for its kind, or carries injection text is dropped, and
  the report counts the drops. Messages are redacted before they are shown.
- **Flagged pages in maintenance:** a page with a secret or injection finding
  (error or warn) gets no `--fix` or `consolidate` job, and its text never
  enters a plan or deep prompt. A flagged `open-questions.md` is no allowed
  file of an edit. Edit sessions run in the wiki root with file tools; each
  edit call passes `--disallowed-tools` with a `Read(./<path>)` rule per
  flagged file and `Read(./.wiki/quarantine/**)`. The rules are relative to
  the wiki root, so its absolute path never enters one, and a `Read` deny also
  keeps the file out of Grep. Ingest edit sessions get the same rules for
  the pages flagged when the run starts; a file an edit may change is never
  denied, since it must be read to be edited. `wiki doctor` probes the
  denial (`read-denial`) with a canary file.

Exit codes: 0 ok, 1 findings, 2 error.

## 9. Reuse from kbpipe (`/tmp/edp-wiki-bulk-ingestion-pipeline/src/kbpipe/`)

Port and adapt these; do not import them, because kbpipe is Confluence-coupled.

- `llm/runner.py`: argv building, child env, error classification, retries,
  usage parsing. Drop the thread pool; ingest is sequential.
- `normalise/attachments.py` + `normalise/images.py`: the extractors and caps
  for the supported source formats. MarkItDown is the primary converter; these
  are the fallbacks and the condensers.
  - **images:** triage before any LLM sees them (drop tiny images and icons,
    sha256/pHash dedupe, per-source cap), then a vision description at
    `confidence: low`;
  - **pptx:** slide outline plus speaker notes (python-pptx);
  - **xlsx:** per-sheet tables, with CSV plus a profile for large sheets;
  - **csv:** the same small-vs-large handling as xlsx;
  - **docx:** text with its heading structure;
  - **PDFs:** the pypdf text layer as the first fallback, Docling when
    installed, and "needs-vision" pages rendered for the LLM when the text layer
    is thin.
- `normalise/secrets.py`: the redaction regexes and `never_ingest`.
- `stages/index.py`: index generation from frontmatter (re-shaped to the
  `_index.md` per section format).
- `stages/lint.py`: the check structure and the report writer.
- `kb-template/kb/AGENTS.md` §5–§8 and §11: the fact-preservation rules, the
  attachment-extraction table, the clean-up decision table and the
  update/coherence rules. This is strong content for the new `AGENTS.md`
  template.
- `tests/fake_claude/claude`: the scripted fake CLI for offline tests.

## 10. Repo layout (`/workspaces/llm-wiki`)

```text
pyproject.toml            py ≥3.12; package `llm-wiki`, script `wiki`; typer, pydantic v2, pyyaml, rapidfuzz, platformdirs, markitdown[all], python-pptx, openpyxl, pypdf, pillow, imagehash;
                          extras: docling; dev: pytest; version single-sourced; templates/prompts as package data (importlib.resources)
src/llm_wiki/
  cli.py config.py caps.py registry.py
  manifest.py convert.py screen.py
  claude.py schemas.py prompts/{plan,edit,lint_deep,consolidate}.md
  ingest.py gitops.py index.py lint.py search.py consolidate.py skill.py doctor.py
  templates/{AGENTS.md, CLAUDE.md, wiki.toml, profiles/*.toml, pages/*.md, skill-wiki-lookup.md, claude-pointer.md}
tests/ fake_claude/ fixtures/ (docx, pptx+notes, 2-sheet xlsx, thin pdf, png, fake-secret, injection, contradiction pair) test_*.py
docs/superpowers/specs/2026-10-26-llm-wiki-design.md   (this design, committed)
```

## 11. Milestones (each ends with tests green and a demo command)

1. **Deterministic core:** scaffold, config/registry, caps, `init`, manifest,
   convert, screen, index, lint, search, status. Demo:
   `wiki init demo && wiki status && wiki lint`.
2. **Runner + doctor:** fake claude; `wiki doctor` verifies the flags,
   `--permission-mode` and isolation against the real CLI (2.1.310 is installed
   here).
3. **Ingest:** plan → validate → edit → post-check → finalize → gate; changed
   and deleted sources; `--note`.
   _Done 2026-10-28: 621 offline tests; a real `wiki ingest --yes` of an AWS
   diagram, an annotated Jira screenshot and a docx produced an architecture
   page, a three-step howto in marker order and a conventions page, accepted
   with 0 lint errors for $0.39._
4. **Lookup skill + packaging:** `wiki skill install`; build the wheel and
   install it in a clean venv, where `wiki doctor` passes; run 5 evaluation
   questions from an unrelated repo.
   _Done 2026-10-28: the wheel installs in a fresh venv and `wiki doctor`
   passes there. From an unrelated repo, Claude Code with the skill answered 4
   questions with [AS-IS] labels and raw citations, reading 1 page each, and
   said "not in wiki" for the fifth, for about $0.35 in total. A headless run
   needs `Skill(wiki-lookup)` allowed; an interactive one asks once._
5. **Maintenance:** `lint --deep/--fix`, `consolidate`, cap-reached behaviour.
   _Done 2026-10-29: 761 offline tests, among them a cap-reached end to end
   test (ingest refused at the cap, `consolidate` merges and archives, the
   retried ingest fits). On a copy of the M3 demo wiki seeded with a duplicate
   howto, an unlinked deprecated page, a built to-be requirement and a broken
   link, a real `lint --deep` sampled 11 of 11 pages and reported the to-be
   contradiction and the duplicate ($0.18). `consolidate` merged the duplicate
   (a superseded stub, its overview link retargeted, the stub archived),
   promoted the requirement (its open question removed), and archived the
   deprecated page, with 0 lint errors ($0.35). That run had no TTY, so its
   "interactive" gate ran through the `decide()` callback of `gate.decide`.
   `lint --fix --yes` removed the broken link ($0.10), and `wiki lint` then
   exited 0._
6. **Real use:** ingest 5–10 real sources one at a time; tune the prompts.
   _Done 2026-10-30: 823 offline tests. Edit sessions now get deny-read rules
   (`read-denial` probe ok on the real CLI), a timed-out `claude` loses its
   whole process group, and lint warns on `footnote-spacing`. Ten real
   sources went in one per run: md, csv ×2 (one 15 MB), xlsx ×2, docx,
   pptx, png diagram, a 66-page text PDF, and a drawio file that intake
   refused as unsupported. The baseline cost $3.90 plus $0.60 for
   `lint --deep`: 0 lint errors, 1 plan retry, 2 repairs, 0 strays, 11
   `uncited-paragraph` warns. Its 14 defects, and 3 more found while tuning,
   led to code fixes (zero-width spaces and repeated-character runs no
   longer quarantine a source;
   `basic_auth` redacts only a value that decodes to `user:pass`; sheet
   tables carry row numbers and keep cells up to 1,000 characters; an
   empty plan for a new source is retried; plan errors and repair findings
   are kept in the run dir) and to two prompt rounds re-run from snapshots
   ($4.22): uncited open points, abbreviated identifiers, folded diagram
   flows, merged image locators and part-named source titles are gone.
   Total real spend $8.81 including both doctor runs. Left: images inside
   documents are not read, docx merged cells shift columns, and a new page
   links related existing pages in only some runs (older pages never gain
   the back-link; `lint --deep` reports it)._

## 12. Verification

- **Offline `pytest` with the fake claude:**
  - argv and env (no `--bare`, no API key);
  - plan validation rejects over-cap, wrong routing, foreign citations and
    forbidden paths;
  - the post-check reverts unplanned files;
  - re-running an ingest with nothing changed is a no-op;
  - a changed source touches only `pages_for` pages; a deleted source marks
    claims unverified;
  - secret and injection fixtures are quarantined and never appear in `wiki/`,
    `.wiki/converted/` or git history;
  - the contradiction fixture records both claims plus an open question;
  - lint detects each seeded issue (broken link, orphan, stale, oversize,
    uncited, sha mismatch, duplicate title, superseded without successor, cap
    exceeded).
- **Real checks:**
  - `wiki doctor` passes;
  - ingest 3 fixtures into a demo wiki and accept;
  - `wiki lint` exits 0;
  - the wheel builds and installs in a fresh venv; `wiki init` on a cloned wiki
    only registers it; `LLM_WIKI_PATH` discovery works;
  - with the skill installed globally, Claude Code in any repo answers the
    evaluation questions with [AS-IS]/[TO-BE] labels and raw citations while
    reading ≤ 5 pages, and says "not in wiki" for an unanswerable question.

## After approval

1. Commit this design as `docs/superpowers/specs/2026-10-26-llm-wiki-design.md`
   on a feature branch.
2. Produce the detailed task-by-task implementation plan (writing-plans) for
   milestone 1, then implement test-first.

## Left to the builder

Exact BM25 parameters; prompt wording; the order of the injection-heuristic
regexes; the converter thresholds within the stated rules; the Typer UX details
(colors, progress); the `wiki.toml` key names.
