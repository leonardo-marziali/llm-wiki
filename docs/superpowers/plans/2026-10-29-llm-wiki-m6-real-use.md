# llm-wiki M6: real use and prompt tuning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the residuals parked after M5, harden edit sessions with
deny-read permission rules, then ingest 5–10 of the user's real sources one at a
time and tune the package prompts against what those runs show.

**Architecture:**

- **Tasks 1–5 are code** and run offline against the fake `claude`: two M5
  residual fixes in maintenance, deny-read rules carried on `claude.Call` and
  probed by `wiki doctor`, process-group kill on timeout, and one new lint warn.
- **Tasks 6–7 are real use.** A fresh wiki ingests the user's sources one per
  run, each scored on a fixed scorecard. Defects found there become changes to
  the package prompts (`src/llm_wiki/prompts/*.md`) or, where a rule can be
  checked deterministically, to a validator with an offline test. Each tuning is
  re-run on a snapshot of the wiki taken before the affected source.
- **Task 8** documents the results and cleans up memory.

**Tech Stack:** Python ≥ 3.12, Typer, Pydantic v2, pytest with the fake
`claude`, the real `claude` CLI (2.1.312 installed) for Tasks 3, 6 and 7.

**Spec:** `docs/superpowers/specs/2026-10-26-llm-wiki-design.md` — §8 (flagged
pages in maintenance, the deny-read rule "left to M6"), §11 item 6, §12. The
carry-forward items come from memory `llm-wiki-carry-forward.md`.

## Global Constraints

- No new dependencies (pyproject and devcontainer stay as they are).
- Supported source formats stay exactly md/txt, images, pptx, xlsx, csv, docx
  and PDF; no vendor integrations.
- Every flag `build_argv` emits is listed in `claude.REQUIRED_FLAGS`, so
  `wiki doctor` checks it against `claude --help`.
- Prompt tuning changes the package prompts (`src/llm_wiki/prompts/`), never
  `templates/AGENTS.md`: an existing wiki's `AGENTS.md` is not upgraded
  automatically (still parked), so rules placed there would not reach it.
- Caps are unchanged: 100 sources and 500 pages per wiki.
- Lint exit codes stay 0 ok, 1 findings, 2 error; a new check is `warn`.
- The real sources live at `$M6_SOURCES` (a folder the user provides). They are
  never committed to this repo, and no file name or content from them goes into
  the repo's docs, tests or commit messages; the docs record counts, formats,
  costs and defect classes only.
- Real-run spend for Tasks 6–7 stops at **$10 total**; past that, stop and ask
  the user.
- Lint and analysis work loads the listed skills first (`python:ruff-*`,
  `sonarqube:*`, `markdown:markdownlint-*`); after a scripted edit run `pyright`
  on the touched files.

## Review Focus

1. **A wiki root whose absolute path holds spaces or parentheses.**
   `--disallowed-tools` splits on spaces, and a rule is `Tool(spec)`. Rules are
   therefore cwd-relative (`./…`) and each rule is its own argv item. Test
   `test_deny_rules_relative_with_spaced_root` in Task 3.
2. **The child already exited when the timeout fires, or its group is gone.**
   `killpg` must swallow `ProcessLookupError`, and the timeout still classifies
   as before. Test `test_timeout_kill_tolerates_gone_group` in Task 4.
3. **A `frontmatter-invalid` page whose YAML does not parse at all, or whose
   `sources` entries are malformed.** `page_sources` returns only the entries
   that validate, never raises. Test `test_page_sources_lenient_cases` in
   Task 1.
4. **Footnote definitions inside fenced code, consecutive definitions, or a
   definition at the top of the body.** None of these is flagged. Test
   `test_footnote_spacing_ignores_code_and_runs` in Task 5.
5. **A flagged `open-questions.md` while the plan still proposes questions.**
   The plan is rejected and retried with the error, never applied. Test
   `test_flagged_oq_plan_with_questions_rejected` in Task 2.

---

### Task 1: `lint --fix` of a `frontmatter-invalid` page keeps its sha pairs

M5 residual: `_JobEdit.shas` and `cited_paths` read `page.fm.sources`, which is
`None` when the frontmatter does not validate. A fix job on such a page that
copies its own `sources` back always fails on `maint-sha-changed`.

**Files:**

- Modify: `src/llm_wiki/pages.py` (new `page_sources`)
- Modify: `src/llm_wiki/lint.py:335-338` (`cited_paths`)
- Modify: `src/llm_wiki/maint_job.py` (`_JobEdit.__init__`, the `self.shas` set)
- Test: `tests/test_pages.py`, `tests/test_maint_run.py`

**Interfaces:**

- Produces: `pages.page_sources(page: Page) -> list[SourceRef]`. It returns
  `page.fm.sources` when `fm` is valid. Otherwise it re-reads `page.path`, runs
  `split_frontmatter`, and returns each item of `data["sources"]` that
  `SourceRef.model_validate` accepts. It returns `[]` on any read, YAML or shape
  error.

- [ ] **Step 1: Write the failing tests.**
  - `test_page_sources_valid_fm` returns `fm.sources`.
  - `test_page_sources_invalid_fm_keeps_valid_entries`: a page with
    `type: bogus` and two `sources`, one with a 64-hex sha and one with
    `sha256: xyz`, returns exactly the first.
  - `test_page_sources_lenient_cases`: malformed YAML, no frontmatter,
    `sources: "a string"` and a non-UTF-8 file each give `[]`.
  - In `test_maint_run.py`, `test_fix_frontmatter_invalid_page_with_sources`
    seeds a page with valid `sources` but `type: bogus`. The fake edit restores
    a valid frontmatter with the same `sources`. Assert the job is `done` and no
    `maint-sha-changed` finding is reported.
- [ ] **Step 2: Run them; expect FAIL** (`page_sources` undefined;
      `maint-sha-changed`).
- [ ] **Step 3: Implement `page_sources` and use it.** Use it in `cited_paths`
      and for `_JobEdit.shas`, where `self.shas` becomes
      `{(s.path, s.sha256) for p in inputs for s in page_sources(p)}`.
- [ ] **Step 4: Run `.venv/bin/pytest -q`; all pass.**
- [ ] **Step 5: Commit.** Message:
      `fix: keep the sha pairs of a frontmatter-invalid page in lint --fix`.

### Task 2: a flagged `open-questions.md` gets no open-questions instructions

M5 residual: when `wiki/open-questions.md` is flagged, it already leaves the
allowed list, but the plan prompt still lets the plan propose `open_questions`,
and `maint_edit.md` tells the edit to add them.

**Files:**

- Modify: `src/llm_wiki/maint.py` (`_job_block`, `build_maint_plan_prompt`,
  `validate_maint_plan`)
- Modify: `src/llm_wiki/maint_job.py` (`plan_job` passes the lock)
- Modify: `src/llm_wiki/prompts/maint_edit.md` ("Open questions" section)
- Test: `tests/test_maint.py`, `tests/test_maint_run.py`

**Interfaces:**

- Produces: `validate_maint_plan(..., oq_locked: bool = False)`. When it is
  true, a non-empty `plan.open_questions` or `plan.resolved_questions` adds the
  error

  ```python
  "open_questions/resolved_questions must be empty: wiki/open-questions.md is flagged this run"
  ```

- `build_maint_plan_prompt`: when `OPEN_QUESTIONS in exclude`, the Job block
  gains the line

  ```text
  - open questions: none this run (wiki/open-questions.md is flagged); leave open_questions and resolved_questions empty
  ```

- [ ] **Step 1: Write the failing tests.**
  - `test_maint_prompt_flagged_oq_line`: the prompt holds the line above when
    flagged, and lacks it when not flagged.
  - `test_validate_maint_plan_oq_locked`: the error appears only with
    `oq_locked=True` and non-empty questions.
  - `test_flagged_oq_plan_with_questions_rejected` (run level): the fake plan
    has `open_questions` on its first reply and none on its second. Assert two
    plan calls, the job done, and `open-questions.md` byte-identical.
- [ ] **Step 2: Run them; expect FAIL.**
- [ ] **Step 3: Implement it.**
  - `plan_job` passes `oq_locked=OPEN_QUESTIONS in ctx.exclude`.
  - In `maint_edit.md`, the "Open questions" section opens with "Only when the
    allowed list names `wiki/open-questions.md`:".
- [ ] **Step 4: Run `.venv/bin/pytest -q`; all pass.**
- [ ] **Step 5: Commit.** Message:
      `fix: give no open-questions instructions when the file is flagged`.

### Task 3: deny-read permission rules for edit sessions

Spec §8 left this to M6. Edit sessions are told not to read flagged pages and
`.wiki/quarantine/`; now the CLI enforces it.

**Files:**

- Modify: `src/llm_wiki/claude.py`:
  - `Call` gains `deny_read: tuple[str, ...] = ()`;
  - `make_call` gains `deny_read` (keyword);
  - `build_argv`;
  - `REQUIRED_FLAGS` gains `"--disallowed-tools"`.
- Modify: `src/llm_wiki/session.py`:
  - `EditSession.deny: set[str]`, which starts as `{".wiki/quarantine/"}`;
  - `edit()` passes `deny_read=deny_rules(self.deny)`.
- Modify: `src/llm_wiki/maint_job.py`: `_JobEdit` adds `ctx.exclude` to
  `session.deny`.
- Modify: `src/llm_wiki/doctor.py` (new `probe_read_denial`, run after
  `probe_edit_confinement`)
- Test: `tests/test_claude.py`, `tests/test_session.py`, `tests/test_doctor.py`

**Interfaces:**

- Produces: `claude.deny_rules(rels: Iterable[str]) -> tuple[str, ...]`. It is
  sorted and has no duplicates. For each wiki-root-relative path, it emits
  `Read(./<rel>)` and `Grep(./<rel>)`; a path ending in `/` becomes `<rel>**`,
  so `.wiki/quarantine/` yields `Read(./.wiki/quarantine/**)`.
- `build_argv` adds `["--disallowed-tools", *call.deny_read]` only when
  `call.edit and call.deny_read`.
- `doctor.probe_read_denial(settings) -> Check`, named `read-denial`. It works
  in a temp dir holding `secret.md` with a random canary word. An edit call
  denies `secret.md` and is asked to read it and reply with its content.
  - `fail` when the canary appears in the result text;
  - `ok` when the result's `permission_denials` names it, or when the canary is
    absent and the model reports the read was denied;
  - `warn` "denial unproven" when the model never tried to read the file.

- [ ] **Step 1: Verify the rule syntax on the real CLI before coding.** In the
      scratchpad, run `claude -p` with `--tools Read,Grep`,
      `--setting-sources ""`, `--permission-mode acceptEdits`, `--restricted`
      and `--disallowed-tools "Read(./secret.md)" "Grep(./secret.md)"`, in a dir
      holding `secret.md`, and ask it to read the file. Then repeat with a
      directory rule (`Read(./q/**)`). - **Expect:** a `permission_denials`
      entry and no canary in the reply. - **If `./` is not honoured:** try `/…`
      (relative to the project root), then record which form works in a comment
      above `deny_rules`. Also confirm whether a `Read` deny alone already
      blocks `Grep`. If it does, emit `Read` rules only.
- [ ] **Step 2: Write the failing tests.**
  - `test_deny_rules_dirs_and_files`: covers `deny_rules`, including dedupe and
    sorting.
  - `test_build_argv_deny_only_for_edit`: a non-edit call never carries
    `--disallowed-tools`.
  - `test_deny_rules_relative_with_spaced_root`: the root is
    `tmp/"my wiki (x)"`. Every rule starts `Read(./` or `Grep(./`, and no argv
    item contains the absolute root.
  - `test_edit_session_denies_quarantine_and_flagged`: the fake claude records
    argv. Assert the quarantine rule always appears, and that for a maint job
    with a flagged page its rule appears too.
  - `test_required_flags_include_disallowed_tools`.
  - Doctor tests for `probe_read_denial`: ok, fail (canary leaked) and warn,
    with the fake claude, following the `probe_edit_confinement` tests.
- [ ] **Step 3: Run them; expect FAIL.**
- [ ] **Step 4: Implement to the Interfaces above.**
- [ ] **Step 5: Run `.venv/bin/pytest -q`; all pass.**
- [ ] **Step 6: Run `wiki doctor` (with probes) on the real CLI.** Expect
      `read-denial ok`. Report the cost.
- [ ] **Step 7: Commit.** Message:
      `feat: deny edit sessions reads of flagged pages and the quarantine`.

### Task 4: kill the whole process group on timeout

Parked: a timed-out `claude` can leave orphaned grandchildren.

**Files:**

- Modify: `src/llm_wiki/claude.py` (`_spawn`)
- Test: `tests/test_claude.py`

**Interfaces:**

- `_spawn` keeps its signature and return contract (`None` on timeout).

- [ ] **Step 1: Write the failing tests.**
  - `test_timeout_kills_grandchild`: `LLM_WIKI_CLAUDE` is a scratch shell script
    that starts `sleep 60 &`, writes the sleep's pid to a file, then waits. With
    `timeout_s=1`, assert `_spawn` returns `None`, and that within 2 s
    `os.kill(pid, 0)` raises `ProcessLookupError`.
  - `test_timeout_kill_tolerates_gone_group`: monkeypatch `os.killpg` to raise
    `ProcessLookupError`. The timeout still returns `None`.
- [ ] **Step 2: Run them; expect the first to FAIL.**
- [ ] **Step 3: Implement it.**
  - Spawn with `subprocess.Popen(..., start_new_session=True)` and
    `communicate(input, timeout=...)`.
  - On `TimeoutExpired`, call `os.killpg(proc.pid, signal.SIGKILL)` (catch
    `ProcessLookupError`), then `proc.communicate()` to reap, and return `None`.
  - Return a `CompletedProcess` built from `returncode`, stdout and stderr, so
    callers do not change.
  - Keep the existing `except` branches.
- [ ] **Step 4: Run `.venv/bin/pytest -q`; all pass.**
- [ ] **Step 5: Commit.** Message:
      `fix: kill the claude process group when a call times out`.

### Task 5: lint warns on a footnote block glued to the text

Seen on demo-m5: maintenance edits drop the blank line before footnote
definitions, and lint does not flag it.

**Files:**

- Modify: `src/llm_wiki/lint.py` (`_footnote_issues`)
- Modify: `src/llm_wiki/prompts/edit.md` and
  `src/llm_wiki/prompts/maint_edit.md` (one rule line each, in the Pages and
  Maintenance rules lists)
- Test: `tests/test_lint.py`

**Interfaces:**

- New finding: `warn`, code `footnote-spacing`, message
  `footnote [^<label>] follows text with no blank line`. It fires when a
  footnote definition line, outside code, directly follows a line that is
  neither blank nor itself a footnote definition (or its indented continuation).
- Prompt rule text, in both prompts: "Keep one blank line between the text and
  the first footnote definition."

- [ ] **Step 1: Write the failing tests.**
  - `test_footnote_spacing_glued`: a paragraph line, then `[^1]: raw/a.md — §1`,
    gives one `footnote-spacing` warn.
  - `test_footnote_spacing_ignores_code_and_runs`: these give none:
    - consecutive definitions after a blank line;
    - a glued definition inside a fenced block;
    - a definition on the body's first line.
- [ ] **Step 2: Run them; expect FAIL.**
- [ ] **Step 3: Implement the check and the prompt rule lines.**
- [ ] **Step 4: Run `.venv/bin/pytest -q`; all pass.** Fix any existing fixture
      that now warns, if its test asserts exit 0. Prefer fixing the fixture text
      over loosening the check.
- [ ] **Step 5: Commit.** Message:
      `feat: warn on footnote definitions glued to the text`.

### Task 6: baseline real use

Ingest the user's sources one at a time, with the prompts as they are after Task
5, and score each run.

**Files:**

- Scratchpad only: the wiki `m6` (`wiki init`), with `LLM_WIKI_HOME` or the
  config pointing at a scratch home as in M5, a `snapshots/` folder, and
  `scorecard.md`.

The scorecard has one row per source:

- the format and its kind (diagram, annotated screenshot, document, deck, sheet,
  scanned or text PDF);
- the cost;
- plan attempts (1 or 2) and validator errors;
- repairs, and strays reverted;
- lint errors and warns after accept, by code;
- the quality checks that apply:
  - **numbers kept:** every number in the source's key facts appears on a page;
  - **flows:** a diagram's flows appear as `A -> B: label (protocol)` lines;
  - **marker order:** an annotated screenshot gives a howto in marker order;
  - **locators:** they point inside the source (slide, sheet range, §), not the
    whole file;
  - **routing:** right section and type;
  - **titles:** a src page title names the source, not the topic;
  - **footnote spacing.**
- defects, each tagged with a class (`prompt`, `validator`, `converter`,
  `wontfix`).

- [ ] **Step 1: Check the sources.** `ls -la "$M6_SOURCES"` holds 5–10 files in
      supported formats. If not, stop and ask the user.
- [ ] **Step 2: Order the sources and record the order** in the scorecard. Order
      them simple text first, then office, then images, then PDFs, so that later
      sources can update pages made by earlier ones.
- [ ] **Step 3: Run each source.**
  - Before each source: `cp -a m6 snapshots/before-<n>` (`<n>` is the 1-based
    order).
  - Copy that one file into `m6/inbox/`, then run `wiki ingest --yes`.
  - Then run `wiki lint`, and fill the scorecard row from the run summary,
    `.wiki/runs/*` and the pages.
  - Keep the running total of spend; stop at $10.
- [ ] **Step 4: Run `wiki lint --deep` once on the result** and add its findings
      to the defects.
- [ ] **Step 5: Summarise the defects for the user and wait.** Group them by
      class, name the proposed fix for each, and let the user choose which to
      tune before Task 7. No commit; the scratchpad is not in the repo.

### Task 7: prompt tuning rounds

At most 3 rounds. Each round fixes one or more defect classes the user chose.

**Files:**

- Modify:
  `src/llm_wiki/prompts/{plan,edit,fix,consolidate,maint_edit,lint_deep}.md`,
  only the ones a chosen defect concerns
- Modify, only for defects tagged `validator`: `src/llm_wiki/plan.py` or
  `src/llm_wiki/maint.py`
- Test: the matching `tests/test_*.py`

- [ ] **Step 1: Pin each `validator` defect with an offline test.** Write a
      failing test that reproduces it with a fake plan, then make the smallest
      validator change that passes it. A `prompt` defect gets no unit test: its
      proof is the re-run in Step 3.
- [ ] **Step 2: Make each `prompt` change.** Keep it one or two rule lines in
      the prompt's existing style, worded as an instruction. Never name a real
      source or quote its content.
- [ ] **Step 3: Re-run the source that showed each defect.**
  - Start from `snapshots/before-<n>`, copied to `rerun-<round>-<n>`.
  - Ingest the same file with `--yes`, then score the new row next to the old
    one.
  - A change that fixes nothing, or makes another check worse, is reverted.
- [ ] **Step 4: Run `.venv/bin/pytest -q`, `ruff check`, `ruff format --check`
      and `pyright`.** All must be clean.
- [ ] **Step 5: Commit each round.** Message:
      `fix(prompts): <defect class in plain words>`. The body lists the
      before/after scorecard checks, with no source names.

### Task 8: docs, memory and final verification

**Files:**

- Modify: `README.md`, the consolidate paragraph (line 77). Ids are never reused
  for topic pages; a source page's id follows its raw path, so a raw file
  re-added at the same path gets the same `src-…` id back. Confirm this against
  `intake.py` and `archive.py` before writing it.
- Modify: `README.md`, the doctor section: name the `read-denial` probe.
- Modify: `docs/superpowers/specs/2026-10-26-llm-wiki-design.md`:
  - §8: the deny-read rule is enforced, and how;
  - §8: the `footnote-spacing` warn in the lint list;
  - §11: M6 done, with the test count, the number and formats of sources, the
    total cost, the tuning rounds and the remaining defects.
- Memory: delete the carry-forward items this plan closed. Keep only what
  remains: the inbox same-name replace (by spec), the automated `AGENTS.md`
  upgrade, and any `wontfix` defects worth keeping.

- [ ] **Step 1: Write the docs and memory changes above.** Load the markdownlint
      skills first, and run markdownlint on the changed `.md` files.
- [ ] **Step 2: Verify.**
  - `.venv/bin/pytest -q`: all pass.
  - `ruff check`, `ruff format --check` and `pyright`: clean.
  - Sonar on the changed files: no new findings.
  - `wiki lint` on `m6`: exit 0 or warns only.
  - `wiki doctor`: all ok.
- [ ] **Step 3: Commit.** Message:
      `docs: record M6 real use and the tuned prompts; mark M6 done`.

## Out of scope (recorded rulings)

- **Inbox same-name replacement of an uncommitted raw file:** by spec §5.9;
  unchanged.
- **Automated `AGENTS.md` upgrade:** stays parked. This is why tuning lands in
  the package prompts only.
- **src-page `duplicate-title` noise:** already silenced in M5 (lint skips
  `source-summary` pages). Only the title wording is checked on the scorecard.
- **New formats, connectors or caps:** not part of tuning.

## Verification (end of M6)

- `.venv/bin/pytest -q`: green offline, above the 787 tests M5 ended with.
- `wiki doctor` passes on the real CLI, `read-denial` included.
- 5–10 real sources are ingested and accepted one at a time; the scorecard and
  the tuning re-runs are recorded (counts and classes only) in spec §11.
- A whole-branch review on the most capable model, then the merge choice is put
  to the user.
