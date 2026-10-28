# llm-wiki M5: maintenance (`lint --deep`, `lint --fix`, `consolidate`, cap-reached behaviour) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three new maintenance paths, each ending in the same single gate as
ingest:

- `wiki lint --deep` adds an LLM pass that finds semantic problems.
- `wiki lint --fix` repairs lint findings through plan → edit.
- `wiki consolidate` splits, merges, promotes and archives pages.

At the page cap, consolidate is what makes room again.

**Architecture:**

- **Two modules pulled out of ingest, with no change in behaviour:**
  - `session.py` holds the confined edit session, the post-check and the single
    repair. It comes from `ingest._Source`.
  - `gate.py` holds the git preflight, the undo, the branch and the
    accept/reject gate. They come from `ingest_run`.
- **`maint.py`** turns findings, or deterministic consolidate candidates, into
  **jobs**. A job is to maintenance what a source is to ingest:
  1. one tool-less plan call against the `maint.v1` schema;
  2. the validator;
  3. plan-text screening;
  4. one edit session;
  5. the post-check;
  6. one commit, or a rollback of that job only.
- **`maint_run.py`** runs the jobs on a branch. Its finalize step:
  - archives unlinked deprecated and superseded pages (consolidate only, no
    LLM);
  - rebuilds the indexes;
  - refreshes the manifest's `pages_touched`;
  - writes the log and runs lint;
  - squashes into one commit.

  Then comes the gate.

- **`deep.py`** is the read-only `--deep` pass: one tool-less call that returns
  `deep.v1` findings, which are written to the lint report.

**Tech Stack:** Python ≥ 3.12, Typer, Pydantic v2, pytest with the fake
`claude`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-26-llm-wiki-design.md`. The relevant
sections:

- §1: caps, and the at-cap rule.
- §2: the `wiki lint` and `wiki consolidate` rows.
- §6: models and budgets. Opus (role `deep`) runs `consolidate` and
  `lint --deep`.
- §8: the `--deep` checks.
- §11: item 5.
- §12.

The carry-forward items come from memory `llm-wiki-carry-forward.md`.

## Context

M3 (ingest) and M4 (skill and packaging) are done on `feat/m4-skill-packaging`,
which is stacked on `feat/m3-ingest`. Neither is merged, and 639 tests pass.
What already exists:

- **Cap behaviour.** `plan.validate_plan` refuses creates past `max_pages` or
  `max_section_pages`, ending with `_HINT` ("run `wiki consolidate` or start a
  new wiki"). `_within_source_cap` refuses new sources past `max_sources`. M5
  adds the way out: consolidate frees page slots, and status and lint point to
  it.
- **`_archive/`.** It is already excluded from `iter_pages`, `is_page` and
  lint's system files. Nothing writes to it yet.
- **The edit machinery.** `ingest._Source` covers the confined edit, the stray
  revert, the append-only `open-questions.md` and one lint repair. `ingest_run`
  covers the preflight, `_Undo`, the gate, accept and rollback.

  These are generic apart from three things: the source, the prep commit and the
  manifest commit. Task 2 extracts the generic part rather than copying it.

- **Roles.** `config.Role = Literal["plan", "edit", "deep"]`. The `deep` role
  defaults to `claude-opus-5-5` with a $3 budget, and `make_call("deep", …)` is
  already tool-less with 3 retries.

**Rulings.** These are my calls, where the spec leaves the choice to the
builder:

1. **Maintenance never adds evidence.** Every raw path that a planned or edited
   page cites must already be cited by one of the job's input pages. Valid
   citations are never lost: each footnote whose raw file exists must still
   appear on some page the job touched, either as is or struck through.
   Footnotes to missing raw files may go.
2. **Maintenance never refreshes `last_verified` or `review_after`,** because
   nothing is re-checked against sources. A created page takes the oldest
   `last_verified` and `review_after` of its input pages. The `stale` and
   `sha-mismatch` findings are left to the user, with a hint.
3. **Where each finding goes:**
   - `--fix` repairs page-contract findings.
   - `not-in-index` is fixed by finalize's index rebuild.
   - `page-too-long`, `duplicate-title` and `cap-pages`/`cap-section` (and their
     `-warn` forms) are left for `wiki consolidate`.
   - `secret-in-page`, `injection-in-page`, `log-format`, `index-too-long`,
     `stale`, `sha-mismatch`, `duplicate-id` and `cap-sources` are never sent to
     a model. They are listed under "left for you", each with a hint.
4. **A page with a `secret-in-page` or `injection-in-page` finding (error or
   warn) is excluded from every maintenance job and from the `--deep` sample.**
   Its text never reaches a model again.
5. **Archiving is deterministic, consolidate only.** A `deprecated` or
   `superseded` page that no live page links to moves to
   `<section>/_archive/<slug>.md`, via `git mv`, with `-2`, `-3` on a name
   collision.

   Linked ones are first given a `relink` job, whose edit retargets the links: a
   superseded page's links go to its `superseded_by`, and a deprecated page's
   links are removed. They are then archived in the same run once unlinked. The
   archived id is removed from every manifest `pages_touched`.

6. **`open-questions.md` during maintenance:** appends are allowed, and so is
   removing the exact lines listed in the plan's `resolved_questions`. Anything
   else is put back, with an error.
7. **At most `MAX_JOBS = 10` jobs per run,** errors first. The summary says how
   many are left for the next run.
8. **`.wiki/reports/`** joins the gitignore template. The preflight also
   tolerates it as dirt, so wikis created before M5, which lack that gitignore
   line, still work.
9. **Maintenance rules live in the package prompts** (`prompts/fix.md`,
   `consolidate.md`, `maint_edit.md` and `lint_deep.md`), not in `AGENTS.md`.
   Wikis created before M5 get them without the manual `AGENTS.md` upgrade.

## Global Constraints

- **Branch:** `feat/m5-maintenance`, created from `feat/m4-skill-packaging` in
  this checkout. No worktree, because `.venv` points at the checkout.
- **Dependencies:** none new. If one ever seems needed, edit `pyproject.toml`
  and ask the user to rebuild the DevContainer; never `pip install`.
- **`claude.py` stays the only module that spells CLI flags.** Never pass
  `--bare`.
- **Call shapes:**
  - Plan and deep calls are tool-less, run in an empty temp cwd, and pass
    `--json-schema`.
  - Edit calls use `make_call("edit", …)` with `cwd` = the wiki root. Edits are
    never auto-retried.
  - Models: the `--fix` plan uses role `plan`; the consolidate plan and `--deep`
    use role `deep`.
- **System prompts are byte-stable:** the wiki's `AGENTS.md`, then `\n\n`, then
  the prompt file. Anything variable goes into stdin.
- **Page text in any prompt** is wrapped in `<untrusted-page-{nonce}>`, with
  `nonce = secrets.token_hex(8)`, as ingest does for sources.
- **Files an edit may change:** the planned pages and `wiki/open-questions.md`
  (rule 6). Everything else is reverted and reported.
- **Never touched by a model:** `raw/`, `AGENTS.md`, `CLAUDE.md`, `.claude/`,
  `wiki.toml`, `.wiki/`, the indexes, `log.md` and `_archive/`.
- **Git safety:**
  - Nothing is pushed.
  - Only wiki paths are staged.
  - The own-repo and host-repo rules are the same as for ingest
    (`gate.preflight_git`).
  - The branches are `wiki/fix-<yyyymmdd>` and `wiki/consolidate-<yyyymmdd>`,
    plus `-2` and so on.
- **Exit codes:**
  - `consolidate`: 0 for accept, no-op or a user reject; 1 when `--yes` was
    refused; 2 for `WikiError`.
  - `lint --fix`: 2 for `WikiError`, 1 when `--yes` was refused. Otherwise it
    re-lints the base after the gate and exits 1 if errors remain (or warnings,
    with `--strict`), else 0.
  - `lint --deep` keeps lint's exit codes. Deep findings are always `warn`.
- **Cost line:** the sum of `Result.cost_usd` and `ClaudeError.cost_usd` over
  every call.
- **Commits:** messages end with this session's attribution trailers.

## Review Focus

1. **A merge or split that drops cited facts.** The model rewrites two pages
   into one and loses three footnoted claims.
   - Expected: the citation-preservation check errors, one repair runs, then the
     job rolls back and is reported as failed. The pages stay as they were.
   - Pinned in Task 5 by `test_merge_dropping_citations_rolls_back`.
2. **A page whose text holds an injection or a secret** (an `injection-in-page`
   warn from a trusted source, or a `secret-in-page` error).
   - Expected: it is never in a job's inputs, never in the deep sample, and
     never in a prompt. It is listed under "left for you".
   - Pinned in Task 3 by `test_deep_sample_excludes_flagged_pages` and in Task 4
     by `test_flagged_pages_never_in_jobs`.
3. **Archiving a page that a live page still links to.**
   - Expected: it is never archived while linked. After a successful relink it
     is archived in the same run, and `wiki lint` then has no `broken-link`.
   - A later changed or deleted source whose `pages_touched` named the archived
     id does not fail.
   - Pinned in Task 6 by `test_archive_only_unlinked`,
     `test_relink_then_archive_same_run` and
     `test_changed_source_after_archive`.
4. **Ctrl-C or a crash during consolidate, after an archive move.**
   - Expected: the base branch is checked out, the tree is byte-identical, and
     the branch is deleted, as with ingest.
   - Pinned in Task 6 by `test_consolidate_interrupt_rolls_back`.
5. **A `--deep` reply that names pages that don't exist or weren't sampled, or
   that carries instructions.**
   - Expected: such findings are dropped and counted ("dropped 2 findings naming
     unknown pages"). Findings whose text matches `find_injection` are dropped
     too. Nothing from them reaches a `--fix` plan.
   - Pinned in Task 3 by `test_deep_drops_unknown_pages_and_injection`.

---

## File Structure

```text
src/llm_wiki/session.py     EditSession: allowed set, baseline, edit, changed/revert, check, one repair, undo (from ingest._Source)
src/llm_wiki/gate.py        OP_PREFIXES, preflight_git, Undo, fresh, fresh_branch, plain, append_log, show_diff, decide, accept, rollback (from ingest_run)
src/llm_wiki/ingest.py      _Source now built on EditSession (no behaviour change)
src/llm_wiki/ingest_run.py  uses gate.py (no behaviour change)
src/llm_wiki/deep.py        sample_pages, run_deep -> DeepResult
src/llm_wiki/maint.py       Job, fix_jobs, consolidate_jobs, validate_maint_plan, build prompts, process_job
src/llm_wiki/maint_run.py   run_maintenance, archive, finalize, summarize_maint
src/llm_wiki/schemas.py     + DEEP_SCHEMA/DeepReply, MAINT_SCHEMA/MaintPlan
src/llm_wiki/lint.py        duplicate-title skips source-summary; cap hints; write_report(deep=…)
src/llm_wiki/status.py      + near_cap hint
src/llm_wiki/cli.py         lint --deep/--fix/--yes; consolidate
src/llm_wiki/prompts/       lint_deep.md fix.md consolidate.md maint_edit.md
src/llm_wiki/templates/gitignore  + .wiki/reports/
tests/test_session.py test_gate.py test_deep.py test_maint.py test_maint_run.py test_consolidate.py test_e2e_m5.py
```

---

### Task 1: Carry-forward, and cap-reached hints

**Files:** `src/llm_wiki/lint.py`, `src/llm_wiki/status.py`,
`src/llm_wiki/cli.py`, `src/llm_wiki/templates/gitignore`, `tests/test_lint.py`,
`tests/test_status.py`, `tests/test_init.py`

**Interfaces:**

- Produces `lint.CONSOLIDATE_HINT = "run \`wiki consolidate\` or start a new
  wiki"`.`plan._HINT` imports it, so there is one copy.
- Produces `WikiStatus.near_cap: list[str]`, for example
  `["pages 410/500: run \`wiki consolidate\`"]`. A sources entry reads`sources
  85/100: start a new wiki or delete obsolete raw sources`.

- [ ] **Step 1: Write the failing tests:**
  - `test_duplicate_title_ignores_source_summaries`: a `src-…` page and a topic
    page with the same title give no `duplicate-title`. Two topic pages with the
    same title still give one. This is the parked M6 item: the lint noise would
    otherwise feed false merge jobs to consolidate.
  - `test_cap_findings_carry_hint`: `cap-pages`, `cap-pages-warn`, `cap-section`
    and `cap-section-warn` messages end with `CONSOLIDATE_HINT`.
  - `test_status_near_cap`: with `max_pages = 5` in `wiki.toml` and 4 pages,
    `near_cap == ["pages 4/5: run \`wiki
    consolidate\`"]`. The text output prints a`near cap:` line, and it is empty
    below the warn level.
  - `test_gitignore_template_ignores_reports`.
- [ ] **Step 2: Run**
      `.venv/bin/pytest -q tests/test_lint.py tests/test_status.py tests/test_init.py`.
      Expected: the new tests FAIL.
- [ ] **Step 3: Implement.** The warn levels are `warn_pages` and `warn_sources`
      from `effective_caps()`.
- [ ] **Step 4: Run** `.venv/bin/pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit.** Message:
      `fix: drop src-page duplicate-title noise and point cap findings to consolidate`.
      Delete the duplicate-title bullet from the carry-forward memory.

---

### Task 2: Extract `session.py` and `gate.py` from ingest (no behaviour change)

**Files:**

- Create: `src/llm_wiki/session.py`, `src/llm_wiki/gate.py`,
  `tests/test_session.py`, `tests/test_gate.py`.
- Modify: `src/llm_wiki/ingest.py`, `src/llm_wiki/ingest_run.py`, and
  `tests/test_ingest_run.py` (its monkeypatches of `ingest_run._Undo.keep` and
  `llm_wiki.ingest_run._show_diff` move to `gate.Undo.keep` and
  `llm_wiki.gate.show_diff`).

**Interfaces:**

- **`session.py`:**
  - `class SessionFailed(Exception)`. Its message is the reason.
  - `class EditSession`:
    - `__init__(self, root: Path, cfg: WikiConfig, settings: LlmSettings, today: date, run_dir: Path, name: str)`.
      `name` prefixes the run-dir files, as in `<name><suffix>.<kind>.json`.
    - Attributes:
      - `allowed: set[str]`, starting as `{OPEN_QUESTIONS}`;
      - `reverted: list[str]`;
      - `cost_usd: float`;
      - `oq_removable: set[str]`, the exact lines that may be removed (rule 6;
        empty for ingest);
      - `baseline`, `run_rel` and `oq_before`, as in `_Source` today.
    - `edit(prompt: str, suffix: str, kind: str) -> None`.
    - `changed() -> list[str]`; `revert(paths: list[str]) -> None`;
      `undo() -> None`.
    - `check(pages_to_lint: Callable[[list[str]], list[str]] | None = None, extra: Callable[[], list[Finding]] | None = None) -> list[Finding]`.
      - It reverts strays and records them in `reverted`.
      - It reports `missing-page` for allowed pages that don't exist.
      - It runs the open-questions check, which allows removing `oq_removable`
        lines.
      - It lints the pages returned by `pages_to_lint(changed_allowed_pages)`;
        by default, the changed allowed pages.
      - It adds `extra()` and returns the error findings.
    - `apply(prompt: Callable[[list[Finding] | None], str], suffix: str, *, pages_to_lint=None, extra=None) -> None`.
      It runs the edit, the check, and one repair, and raises `SessionFailed`
      when errors remain.
- **`ingest._Source`** holds an `EditSession` (composition, not inheritance) for
  everything above. It keeps its own plan, commit and manifest logic. `_Failed`
  stays as ingest's exception, and `SessionFailed` is converted into it.
- **`gate.py`:**
  - `OP_PREFIXES = ("wiki/ingest-", "wiki/fix-", "wiki/consolidate-")`.
  - `DIRT_OK = ("inbox/", "raw/", ".wiki/reports/")`.
  - `preflight_git(root: Path) -> str`. It refuses a checked-out branch with any
    of `OP_PREFIXES`, naming it.
  - `class Undo`: today's `_Undo`, unchanged.
  - `fresh(path) -> Path`; `fresh_branch(root, name) -> str`;
    `plain(text) -> str`; `append_log(root, lines) -> None`;
    `show_diff(root, base, branch) -> None`.
  - `decide(root, base, branch, summary: str, lint_errors: int, yes: bool, decide, confirm) -> Decision`.
  - `accept(root, base, branch, undo) -> None`;
    `rollback(undo, base, branch) -> None`.

  Their bodies are today's `_gate`, `_accept` and `_rollback`, with the summary
  and the error count passed in instead of `IngestReport`.

- [ ] **Step 1: Write the new tests:**
  - `test_session_reverts_strays_and_lints_changed` (fake edit writing one
    allowed page and one stray);
  - `test_session_repair_then_fail` (`SessionFailed` after the repair);
  - `test_session_open_questions_resolved_lines_removable`: a listed line may go
    and appends are kept; removing an unlisted line puts it back with
    `open-questions-append-only`;
  - `test_preflight_refuses_any_op_branch`, parametrized over the three
    prefixes;
  - `test_preflight_tolerates_reports_dirt`.
- [ ] **Step 2: Run them.** Expected: FAIL (the modules don't exist yet).
- [ ] **Step 3: Move the code.** Keep every message string byte-identical,
      because the ingest tests assert on them.
- [ ] **Step 4: Run** `.venv/bin/pytest -q`. Expected: all pass, including the
      639 existing tests with no assertion changed, only the two monkeypatch
      targets.
- [ ] **Step 5: Commit.** Message:
      `refactor: extract the edit session and the gate from ingest`.

---

### Task 3: `wiki lint --deep`

**Files:**

- Create: `src/llm_wiki/deep.py`, `src/llm_wiki/prompts/lint_deep.md`,
  `tests/test_deep.py`.
- Modify: `src/llm_wiki/schemas.py`, `src/llm_wiki/lint.py` (`write_report`),
  `src/llm_wiki/cli.py`.

**Interfaces:**

- **`schemas.py`:**
  - `DEEP_SCHEMA` (deep.v1, hand-written, `additionalProperties: false`) has one
    property:

    ```json
    {
      "findings": [
        {
          "kind": "contradiction|stale-claim|missing-page|missing-link",
          "pages": ["<id>"],
          "message": "…",
          "evidence": "…"
        }
      ]
    }
    ```

  - The models are `DeepFinding` and `DeepReply`.
- **`deep.py`:**
  - `DEEP_CHARS = 120_000`.
  - `sample_pages(root, cfg, report: LintReport) -> tuple[list[Page], int]`
    returns the sample and the number of eligible pages.
    - It skips flagged pages (Ruling 4).
    - It always takes `overview` and `glossary` when present.
    - It then takes the other pages ordered by `last_verified` ascending, then
      id, until their total text exceeds `DEEP_CHARS`.
  - `@dataclass DeepResult`: `findings: list[Finding]`, `sampled: int`,
    `eligible: int`, `dropped: int`, `cost_usd: float`.
  - `run_deep(root, cfg, settings, report: LintReport, run_dir: Path) -> DeepResult`.
- **The prompt:**
  - every `_index.md` in full;
  - the sampled pages, each wrapped in `<untrusted-page-{nonce}>`;
  - the four kinds, with their rules:
    - `contradiction`: one or more pages;
    - `stale-claim`: a claim likely outdated, with its page;
    - `missing-page`: a concept mentioned on 3 or more pages that has no page;
      list the mentioning pages;
    - `missing-link`: exactly 2 pages, from and to.
- **Validation:** drop a finding when any of these is true, and count each drop
  in `dropped`:
  - an id is not in the sample;
  - the kind's page-count rule fails;
  - `find_injection` hits its `message` or `evidence`.
- **A kept finding** becomes
  `Finding("warn", f"deep-{kind}", page_rel(pages[0]), f"{message} (pages: {', '.join(pages)}; evidence: {evidence})")`.
- **`write_report(root, report, today, deep: DeepResult | None = None)`** adds
  this line under the header:
  `deep: sampled S of E pages, D dropped, cost $X.XX`. The deep findings sit in
  the normal per-code groups.
- **CLI:** `wiki lint --deep`:
  - runs `claude_binary()` first, so a missing CLI exits 2 before any work;
  - merges the deep findings into the printed list;
  - always writes the report and prints `report: <path>` and `cost $X.XX`.

  The json format gains
  `"deep": {"sampled", "eligible", "dropped", "cost_usd"}`.

- [ ] **Step 1: Write the failing tests:**
  - `test_deep_call_shape`: role `deep`, model `claude-opus-5-5`, no tools, an
    empty cwd, `--json-schema` = DEEP_SCHEMA, and a system prompt equal to the
    `AGENTS.md` bytes + `\n\n` + `lint_deep.md`.
  - `test_deep_sample_order_and_budget`: with `DEEP_CHARS` monkeypatched small,
    overview comes first, then the oldest `last_verified`.
  - `test_deep_sample_excludes_flagged_pages` (Review Focus 2): a page with a
    trusted-source injection warn is absent from the prompt bytes.
  - `test_deep_drops_unknown_pages_and_injection` (Review Focus 5).
  - `test_deep_findings_are_warnings_exit_code_unchanged`: a clean wiki plus 2
    deep findings exits 0, and exits 1 with `--strict`.
  - `test_deep_writes_report_with_header`.
  - `test_deep_page_text_wrapped_with_nonce`: a page containing
    `</untrusted-page-` cannot close the wrapper.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_deep.py`. Expected: FAIL.
- [ ] **Step 3: Implement.** Reuse `plan.system_prompt`, `plan._save_run` (the
      redacted run record) and the nonce helper from plan.py. Factor the nonce
      into a function there if it isn't one already.
- [ ] **Step 4: Run** `.venv/bin/pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit.** Message:
      `feat: add wiki lint --deep, a sampled semantic pass written to the lint report`.

---

### Task 4: Maintenance jobs, plan schema and validator (offline)

**Files:**

- Create: `src/llm_wiki/maint.py` (jobs, validator and prompts),
  `src/llm_wiki/prompts/fix.md`, `src/llm_wiki/prompts/consolidate.md`,
  `tests/test_maint.py`.
- Modify: `src/llm_wiki/schemas.py`, `src/llm_wiki/plan.py` (expose the id,
  existence, routing and cap helpers to `maint`).

**Interfaces:**

- **`schemas.py`:** `MAINT_SCHEMA` (maint.v1), with the models `MaintPage` and
  `MaintPlan`:
  - **`MaintPlan`:**
    - `summary: str`, one line, used in the log and the commit;
    - `pages: list[MaintPage]`;
    - `resolved_questions: list[str]`, exact lines of `open-questions.md`;
    - `open_questions: list[str]`.
  - **`MaintPage`:**
    - `id`;
    - `action: Literal["create", "update"]`;
    - `type`, `status`, `adr_status | None`, `title`, `summary`;
    - `superseded_by: str | None`;
    - `sources: list[str]`, the raw paths the page will cite;
    - `instructions: str`, the concrete change;
    - `reason: str`.
- **`maint.py`:**
  - `JobKind = Literal["fix", "split", "merge", "promote", "relink"]`.
  - `@dataclass Job`:
    - `kind: JobKind`;
    - `targets: list[str]`, the ids the job is about;
    - `inputs: list[str]`, the ids whose text goes into the prompt, with
      `targets` first;
    - `findings: list[Finding]`;
    - `questions: list[str]`, the `open-questions.md` lines naming a target.
  - `FIX_CODES`: `broken-link`, `orphan`, `missing-tldr`, `missing-sources`,
    `citation-outside-raw`, `citation-missing`, `uncited-paragraph`,
    `section-routing`, `superseded-no-successor`, `id-mismatch`,
    `frontmatter-invalid`, and the four `deep-*` codes.
  - `HINTS: dict[str, str]`, one hint per Ruling 3 "left for you" code plus the
    consolidate codes.
    - For example, `stale` → "review the page against its sources, then re-run
      `wiki ingest` or edit `last_verified` by hand".
    - `sha-mismatch` → "run `wiki ingest`".
    - The consolidate codes → "run `wiki consolidate`".
  - `flagged(report: LintReport) -> set[str]` returns the page rels with
    `secret-in-page` or `injection-in-page` findings.
  - `fix_jobs(root, cfg, report: LintReport) -> tuple[list[Job], list[Finding]]`
    returns the jobs and the "left for you" findings.
    - There is one job per page with `FIX_CODES` findings.
    - A deep finding joins the job of its first page, and its other pages become
      inputs.
    - An `orphan` job adds the top 3 `search` hits for the page's title and
      summary, plus `overview`.
    - Flagged pages get no job and are never inputs.
    - Jobs with error findings come first, then ids in order. The list is capped
      at `MAX_JOBS = 10`.
  - `consolidate_jobs(root, cfg, report) -> list[Job]`, in this order, capped at
    `MAX_JOBS`, skipping flagged pages:
    - **`relink`:** for each deprecated or superseded page with live inbound
      links. The inputs are the page plus its linkers, at most 8.
    - **`promote`:** for each to-be page linked from an `open-questions.md`
      line. The inputs are the page plus its as-is linkers, and `questions`
      holds those lines.
    - **`merge`:** for each `duplicate-title` pair. The inputs are both pages
      plus their linkers, at most 8.
    - **`split`:** for each `page-too-long` page. The inputs are the page plus
      its linkers, at most 8.
  - `validate_maint_plan(plan: MaintPlan, job: Job, *, root, cfg, mode: Literal["fix", "consolidate"]) -> list[str]`.
    The rules:
    1. `len(pages) ≤ max_pages_per_ingest`, and the ids are unique.
    2. The id grammar holds, the section exists and is not of kind `sources`,
       and no system names are used. This is shared with `plan._id_errors`.
    3. A `create` needs the page to be absent and an `update` needs it present.
       Every `update` id is in `job.inputs`.
    4. `routing_error` is `None`.
    5. **Caps.**
       - `fix`: the same as ingest, with no source-page reserve.
       - `consolidate`: current pages + creates − (input pages this plan sets to
         `superseded` or `deprecated`) ≤ `max_pages`, and the same per section.
         A split or merge can therefore run at the cap. The message ends with
         `CONSOLIDATE_HINT`.
    6. Every `sources` entry is a raw path cited by some input page (Ruling 1).
    7. A page with `status: superseded` has a `superseded_by` that is a live
       input or a create in this plan, and not itself.
    8. Every `resolved_questions` line is an exact line of `open-questions.md`,
       and appears in `job.questions` (for promote jobs) or names an input page.
  - `build_maint_plan_prompt(root, cfg, job, mode) -> str` contains:
    - the date;
    - the job kind and targets;
    - the findings (`code path: message`);
    - every `_index.md`;
    - the input pages, wrapped in `<untrusted-page-{nonce}>`;
    - the job's `questions`;
    - the validation errors, on a retry.
- **Prompts:**
  - `fix.md`: fix only the listed findings, the minimal change, never new facts,
    cite only the raw paths already cited, and keep `last_verified`.
  - `consolidate.md` gives a recipe per kind:
    - **split:** the page becomes a `hub` linking each new sub-page, and the
      sub-pages carry its facts with their footnotes.
    - **merge:** the survivor keeps all the facts and lists every source, and
      the other page becomes a `superseded` stub with `superseded_by` and
      retargeted links.
    - **promote:** the to-be page becomes `superseded` by the as-is page, or
      `deprecated` when the facts spread over several pages; the as-is page
      carries every fact that is now live; resolve the question lines.
    - **relink:** retarget or remove the links to the page.

- [ ] **Step 1: Write the failing tests:**
  - `test_fix_jobs_group_by_page_and_route_codes`: `broken-link` and
    `missing-tldr` on one page give 1 job; `stale`, `sha-mismatch` and
    `secret-in-page` go to "left for you" with their `HINTS`; `not-in-index`
    appears in neither.
  - `test_flagged_pages_never_in_jobs` (Review Focus 2): a flagged page is
    neither a target nor an input in either mode, even as an orphan's search
    hit.
  - `test_fix_jobs_capped_errors_first`.
  - `test_consolidate_jobs_kinds_and_order`: a seeded wiki gives relink,
    promote, merge and split, in that order.
  - `test_src_pages_never_merged` (depends on Task 1).
  - Validator, one test per rule:
    - `test_update_outside_inputs_rejected`;
    - `test_new_evidence_rejected`;
    - `test_superseded_needs_live_successor`;
    - `test_consolidate_cap_offsets_superseded`: at `max_pages = N`, one create
      plus one supersede passes, and one create alone fails, ending with the
      hint;
    - `test_fix_cap_like_ingest`;
    - `test_resolved_question_must_exist`;
    - `test_routing_and_grammar_shared_with_ingest`.
  - `test_maint_prompt_wraps_pages_with_nonce`.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_maint.py`. Expected: FAIL.
- [ ] **Step 3: Implement.** `MAINT_SCHEMA` is written by hand and kept
      byte-stable, like `PLAN_SCHEMA`.
- [ ] **Step 4: Run** `.venv/bin/pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit.** Message:
      `feat: add maintenance jobs, the maint.v1 plan and its validator`.

---

### Task 5: Job processing, run, finalize, gate; `wiki lint --fix`

**Files:**

- Create: `src/llm_wiki/maint_run.py`, `src/llm_wiki/prompts/maint_edit.md`,
  `tests/test_maint_run.py`.
- Modify: `src/llm_wiki/maint.py` (`process_job`), `src/llm_wiki/cli.py`.

**Interfaces:**

- **`@dataclass JobOutcome`:**
  - `job: Job`;
  - `status: Literal["done", "failed", "skipped"]`;
  - `pages_created`, `pages_updated`, `reviewed_touched`, `reverted: list[str]`;
  - `summary: str`, `reason: str`, `cost_usd: float`.
- **`process_job(ctx: MaintContext, job: Job) -> JobOutcome`.** `MaintContext`
  holds `root`, `cfg`, `settings`, `today`, `run_dir`, `n` and `mode`. The
  steps:
  1. **The plan call:** role `plan` for fix, role `deep` for consolidate. Then
     `validate_maint_plan`, one retry with the errors, then fail.
  2. **Screening:** `redact` and `screen` the plan strings, as
     `plan.redact_plan` and `screen_plan` do. An injection hit gives `skipped`,
     with the reason `plan-text injection`.
  3. **The edit:** an `EditSession` whose `allowed` holds the planned pages and
     whose `oq_removable` is `set(plan.resolved_questions)`. The edit prompt
     holds:
     - `maint_edit.md` and the plan JSON;
     - the allowed files;
     - each page's dates: the inputs keep theirs, and creates take the oldest of
       the inputs' dates (Ruling 2).
  4. **`extra` checks, each an `error` Finding:**
     - `maint-new-evidence`: a changed page cites a raw path that no input page
       cited;
     - `maint-lost-citation`: a footnote `(raw path, locator)` of an input page
       whose raw file exists appears on no allowed page after the edit, not even
       struck through;
     - `maint-dates-changed`: an updated page's `last_verified` or
       `review_after` differs from before.
  5. **Commit or roll back:** the commit message is `<mode>(<kind>): <targets>`.
     On `SessionFailed`, `WikiError` or `ClaudeError`, the session runs `undo()`
     and the outcome is `failed`. `ClaudeUsageLimit` and `ClaudeAuthError` (the
     `STOP` set) undo and re-raise, as ingest does.
- **`maint_run.py`:**
  - `@dataclass MaintReport`:
    - `mode`;
    - `decision: Literal["noop", "accept", "reject"]`;
    - `base`, `branch`;
    - `outcomes: list[JobOutcome]`;
    - `archived: list[str]`;
    - `left: list[Finding]`;
    - `more: int` (the jobs over `MAX_JOBS`);
    - `lint_errors: list[Finding]`;
    - `cost_usd`;
    - `stopped`;
    - `summary`;
    - `exit_code`.
  - `run_maintenance(ref, *, mode, deep: bool = False, yes: bool, interactive: bool, today=None, decide=None, confirm=None) -> MaintReport`.
    The order:
    1. Load the config and settings, then `claude_binary()`.
    2. `interactive or yes` must hold, or it fails with "the gate needs a
       terminal; pass --yes".
    3. `gate.preflight_git`.
    4. Run `lint`, plus `run_deep` when `deep`.
    5. Build the jobs.
    6. With no jobs, and for consolidate no archive candidates either: print
       `nothing to fix` or `nothing to consolidate` and return `noop`, with no
       branch. The "left for you" lines are still printed.
    7. Create the branch, run the jobs, finalize, summarize, gate, then accept
       or roll back. The rollback covers any `BaseException` too.
  - **Finalize:**
    1. Archive (consolidate only, Task 6).
    2. `build_indexes`.
    3. Manifest: every live page changed on the branch is added to
       `pages_touched` of each raw path it cites.
    4. Log lines: `## [date] fix | <targets>` or
       `## [date] consolidate | <kind> <targets>` for each job that finished,
       and `## [date] archive | <id>` for each archived page.
    5. `stage_commit`, lint the changed pages, then squash. The squash message
       is `wiki lint --fix: <n> jobs (<targets…>)` or
       `wiki consolidate: <n> jobs (…)`.
  - **`summarize_maint(report) -> str`** prints, all passed through
    `gate.plain`:
    - the run line;
    - one line per job (`done`, `failed` or `skipped`, the kind, the targets and
      the reason);
    - created and updated, marking reviewed pages with `!`;
    - archived;
    - reverted;
    - `left for you:`, one line per finding: `code path: hint`;
    - `N more jobs: run again` when `more` > 0;
    - the lint errors;
    - `cost $X.XX`.
- **CLI:**
  `wiki lint [--deep] [--fix] [--yes] [--strict] [--format] [--report]`.
  - `--yes` without `--fix` is a usage error (exit 2).
  - `--fix --format json` is a usage error.
  - `--fix` calls `run_maintenance(mode="fix", deep=deep, …)`, then re-lints and
    exits as Global Constraints say.

- [ ] **Step 1: Write the failing tests** (fake claude: a queued plan, then an
      edit with `write`):
  - `test_fix_broken_link_accept`: the link is retargeted, there is one squashed
    commit on `main`, the log line `fix | concepts.a` is present and the indexes
    are rebuilt.
  - `test_fix_noop_nothing_to_fix`: no branch and no calls. A `stale` finding is
    still printed under "left for you" with its hint.
  - `test_merge_dropping_citations_rolls_back` (Review Focus 1, run in
    consolidate mode with a scripted merge job): both edits drop footnotes, the
    outcome is `failed` with `maint-lost-citation`, and the pages are
    byte-identical.
  - `test_new_evidence_edit_rolls_back`.
  - `test_dates_changed_repaired`: the first edit bumps `last_verified`, and the
    repair restores it.
  - `test_plan_text_injection_skips_job`: the edit session never runs.
  - `test_failed_job_does_not_stop_next_job`.
  - `test_usage_limit_stops_run`.
  - `test_fix_reject_restores_tree`.
  - `test_fix_yes_refuses_when_lint_fails_exit_1`.
  - `test_fix_non_tty_without_yes_refused_before_spending`.
  - `test_fix_with_deep_findings`: `--deep --fix` gives a deep `missing-link`
    job that adds the link.
  - `test_manifest_pages_touched_refreshed`.
  - `test_cli_fix_flag_errors`: `--yes` alone, and `--fix --format json`.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_maint_run.py`. Expected:
      FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `.venv/bin/pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit.** Message:
      `feat: add wiki lint --fix: planned, confined repairs behind the gate`.

---

### Task 6: `wiki consolidate` and archiving

**Files:** `src/llm_wiki/maint_run.py` (`archive_unlinked`),
`src/llm_wiki/cli.py`, `tests/test_consolidate.py`

**Interfaces:**

- **`archive_unlinked(root, manifest) -> list[str]`** returns the archived ids.
  - Candidates: every live `deprecated` or `superseded` page that no live page
    links to. Index files don't count as linkers, since they are rebuilt.
  - Each is moved with `git mv` to `<section>/_archive/<slug>.md`, or
    `wiki/_archive/<slug>.md` for top-level pages, with `-2` and so on when the
    name is taken.
  - Its id is removed from every `pages_touched`.
  - It runs after all jobs, so a page relinked in this run is archived in this
    run.
- **CLI:** `wiki consolidate [--wiki W] [--yes]` calls
  `run_maintenance(mode="consolidate")`. It exits per Global Constraints and
  handles KeyboardInterrupt like ingest: "interrupted: the consolidate was
  rolled back", exit 130.

- [ ] **Step 1: Write the failing tests:**
  - `test_archive_only_unlinked` (Review Focus 3): a linked deprecated page
    stays, an unlinked one moves, `wiki lint` has no `broken-link`, and `search`
    no longer finds it.
  - `test_archive_only_run_costs_nothing`: only archive candidates exist, so
    there are zero fake calls and the gate shows `archived:`.
  - `test_archive_name_collision`.
  - `test_relink_then_archive_same_run` (Review Focus 3).
  - `test_split_creates_hub_and_subpages`: the scripted edit makes the page a
    `hub` with 2 sub-pages, the citations are preserved and `page-too-long` is
    gone.
  - `test_merge_supersedes_and_archives`: the stub is superseded, its linkers
    are retargeted, and the stub is then archived in the same run.
  - `test_promote_resolves_question`: the to-be page is superseded by the as-is
    page and the question line is removed; the other lines are byte-identical.
  - `test_consolidate_interrupt_rolls_back` (Review Focus 4): KeyboardInterrupt
    in the second job's plan call, after the first job is committed. The tree,
    the branches and `_archive/` all match the pre-run snapshot.
  - `test_changed_source_after_archive` (Review Focus 3): later, `wiki ingest`
    of a changed source that backed the archived page succeeds, and the page
    stays archived.
  - `test_cli_consolidate_exit_codes`.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_consolidate.py`. Expected:
      FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `.venv/bin/pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit.** Message:
      `feat: add wiki consolidate with split, merge, promote, relink and archive`.

---

### Task 7: Cap-reached end to end, the real CLI, docs

**Files:** `tests/test_e2e_m5.py`, `README.md`, the spec (§2, §8, §11), memory

- [ ] **Step 1: Write the offline test `test_e2e_m5_cap_reached`.** Set up a
      software wiki with `max_pages = 6` and 6 pages: two near-duplicates and
      one unlinked deprecated page.
  1. `wiki ingest --yes` of a new source fails with the cap error and
     `CONSOLIDATE_HINT`, and the inbox file stays.
  2. `wiki status` prints a `near cap:` line.
  3. `wiki consolidate --yes`, with a scripted merge, supersedes one duplicate
     and archives the stub and the deprecated page, leaving 4 live pages.
  4. The same `wiki ingest --yes` now succeeds.
  5. `wiki lint` exits 0.
- [ ] **Step 2: Real-CLI run (paid; about $3–6 with Opus).**
  1. Clone the M3 demo wiki (`demo-m3` in the scratchpad) to `demo-m5`, and
     register it with `wiki init <path> --name demo-m5`.
  2. Seed it by hand and commit:
     - a near-duplicate of the Jira howto page, with a different wording and the
       same title;
     - an unlinked deprecated page;
     - a to-be requirement page whose fact an as-is page now states, with the
       `open-questions.md` line linking it;
     - one broken link.
  3. Run `wiki lint --deep`. Record the sample line, the findings and the cost.
  4. Run `wiki consolidate` interactively: summary, then `diff`, then `accept`.

     Expected: a merge (the duplicate becomes a superseded stub, its links are
     retargeted and the stub is archived), a promote (the question line is
     removed), the deprecated page archived, and no `maint-lost-citation`.

  5. Run `wiki lint --fix --yes`. Expected: the broken link is fixed.
  6. Run `wiki lint`. Expected: exit 0.

  Tune `prompts/*.md` if the results miss these expectations. Record the gate
  summaries and the costs in the commit body. Do not mark this step done on fake
  results. Afterwards, offer to remove `demo-m5` from the user's registry.

- [ ] **Step 3: Update the docs.**
  - README: the commands table (`wiki lint --deep/--fix`, `wiki consolidate`),
    plus one paragraph on caps and consolidate.
  - Spec §2: the lint row gains `--yes`, and the consolidate row gains `[--yes]`
    and archiving.
  - Spec §8: deep sampling, warn severity, and the dropped findings.
  - Spec §11: M5 done, with the test count and the demo.
  - Memory: delete the done carry-forward items.
- [ ] **Step 4: Verify.**
  - `.venv/bin/pytest -q`: all pass.
  - `ruff check`, `ruff format --check` and `pyright`: clean.
  - Sonar on the changed files: no new findings.
  - `grep -rn -- '"--' src/llm_wiki/*.py`: flag literals only in `claude.py` and
    the git/doctor helpers.
- [ ] **Step 5: Commit.** Message:
      `test: cap-reached end to end and a real consolidate run`.

## Out of scope (recorded rulings)

- **Refreshing dates through maintenance.** `stale` stays the user's to resolve,
  with a hint (Ruling 2).
- **Automated `AGENTS.md` upgrade.** It is not needed for M5, because the
  maintenance rules ship in the package prompts (Ruling 9). It stays parked.
- **Orphaned grandchildren after a timeout** stay parked, because no hang has
  been seen.
- **Prompt quality on real sources** belongs to M6.
- **Merges at a lower title-similarity threshold under cap pressure** are left
  out (YAGNI): exact duplicate-title pairs and archiving free enough room.

## Verification (end of M5)

- `.venv/bin/pytest -q`: all green, offline.
- The Task 7 real run: `lint --deep` writes its report, `consolidate` is
  accepted with a merge, a promote and archives, `lint --fix --yes` is accepted,
  and `wiki lint` exits 0 on `demo-m5`.
- The cap-reached e2e test passes.
- A whole-branch review on the most capable model, then the merge choice is put
  to the user. M3, M4 and M5 are stacked and unmerged.
