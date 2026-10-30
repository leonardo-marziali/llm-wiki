# llm-wiki M4 — Lookup skill + packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the read-only `wiki-lookup` skill (`wiki skill install`) and a
wheel that installs and works in a clean venv. Verify both with real Claude Code
answering evaluation questions from an unrelated repo.

**Architecture:**

- `skill.py` renders two package templates, stamped with `__version__`:
  - `templates/skill-wiki-lookup.md` becomes `<skills>/wiki-lookup/SKILL.md`;
  - `templates/claude-pointer.md` becomes a marked block in `DIR/CLAUDE.md`.
- `cli.py` adds a `skill` sub-app with an `install` command.
- Packaging changes only `pyproject.toml` metadata and adds a README.
- The verification is a real build, a real install, and real Claude Code runs.

**Tech Stack:** Python ≥ 3.12, Typer, `importlib.resources`, and `pip wheel`
from the existing venv. There are no new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-26-llm-wiki-design.md`: §2
(`wiki skill install`), §7 (agent consumption), §1 (always-loaded budget), §11
item 4, §12 (real checks).

## Global Constraints

- **Always-loaded cost:** the skill `description` is ≤ 1,024 chars, written in
  the third person, and says what the skill does and when to use it.
- **The `--pointer` block** is ≤ 15 lines.
- **Skill frontmatter:**
  - `name: wiki-lookup`;
  - `allowed-tools: Read Grep Glob Bash(wiki status *) Bash(wiki search *)`;
  - `disallowed-tools: Write Edit`;
  - `metadata` carries `llm-wiki-version: <__version__>`.
- **The skill's steps:**
  1. `wiki status --all --format json`, and pick the wiki by its description;
  2. read that wiki's `index.md`;
  3. read the relevant `_index.md`;
  4. `wiki search … --format json`;
  5. read ≤ 5 pages, TL;DR first.
- **Answers:**
  - label each fact `[AS-IS]` or `[TO-BE]`;
  - give the page link, the raw footnote, the confidence and `last_verified`;
  - flag a page past `review_after` as `STALE`;
  - when the wiki doesn't have the answer, say "not in wiki" and suggest
    `wiki ingest --note "…"`;
  - agents never write.
- **Install locations:**
  - user (the default): `$CLAUDE_CONFIG_DIR/skills`, else `~/.claude/skills`;
  - `--project DIR`: `DIR/.claude/skills`.
- **Never** hard-code devcontainer or registry specifics. Nothing reads this
  source checkout at runtime.
- **Dependencies:** none new. The wheel is built with
  `.venv/bin/pip wheel --no-deps`. The clean-venv install is a throwaway venv in
  the scratchpad.

## Review Focus

1. **An existing `wiki-lookup/SKILL.md` the user wrote by hand**, without our
   version stamp. Expected: it is refused with a message naming `--force`, and
   nothing is overwritten. Test: `test_install_refuses_foreign_skill`.
2. **Reinstalling the same version.** Expected: the files are byte-identical and
   the command prints "already installed". Reinstalling a different version
   rewrites the file. Tests: `test_install_idempotent` and
   `test_install_upgrades_older_stamp`.
3. **A `CLAUDE.md` that already has a pointer block, or user text around it.**
   Expected: only the marked block is replaced, the user's text is kept byte for
   byte, and a missing `CLAUDE.md` is created. Test:
   `test_pointer_replaces_only_marked_block`.
4. **`--pointer` with no `--project`.** Expected: refused with "--pointer needs
   --project DIR", because a user-level skill has no repo to point from. Test:
   `test_pointer_requires_project`.
5. **The wheel is missing a template.** Expected: a test proves every template
   and prompt is read through `importlib.resources`, and the clean-venv check
   runs `wiki skill install` and `wiki init`. Test:
   `test_package_data_complete`, plus Task 3.

---

### Task 1: M3 carry-forward

**Files:** `src/llm_wiki/ingest_run.py`, `src/llm_wiki/plan.py`,
`src/llm_wiki/caps.py` or `status.py` (wherever sources are counted),
`tests/test_ingest_run.py`, `tests/test_plan.py`

- [ ] **Step 1: Write the failing tests:**
  - `test_finalize_forces_tool_owned_index`: a host repo ignores
    `wiki/**/_index.md`, and the ingest is accepted.
  - `test_run_dir_result_is_redacted`: a fake plan whose claim holds the
    fake-secret fixture. `*.plan.result.json` does not contain the secret value.
  - `test_refused_entry_without_raw_not_counted`: a failed+refused entry whose
    raw file is missing does not count toward `max_sources`.
- [ ] **Step 2: Implement.**
  - `_finalize` passes `force=files`.
  - The run-dir writer runs `screen_text` over the raw result text before
    writing.
  - The source count skips `failed` entries flagged `refused` whose raw file is
    missing.
- [ ] **Step 3: Run** `.venv/bin/pytest -q`. Expected: all pass. Commit:
      `fix: fold in M3 parked ingest items`.

### Task 2: `wiki skill install`

**Files:**

- Create: `src/llm_wiki/skill.py`,
  `src/llm_wiki/templates/skill-wiki-lookup.md`,
  `src/llm_wiki/templates/claude-pointer.md`, `tests/test_skill.py`.
- Modify: `src/llm_wiki/cli.py`, `tests/test_cli.py`.

**Interfaces:**

- `user_skills_dir() -> Path`.
- `render_skill() -> str`.
- `render_pointer(wiki_name: str) -> str`.
- `install_skill(target: Path, *, force: bool = False) -> tuple[Path, bool]`,
  where `target` is the skills dir and the bool is True when the file changed.
  It raises `WikiError` for a foreign skill.
- `write_pointer(project: Path, wiki_name: str) -> tuple[Path, bool]`.
- The pointer markers are `<!-- llm-wiki pointer: begin -->` and
  `<!-- llm-wiki pointer: end -->`.
- CLI: `wiki skill install [--project DIR] [--pointer] [--wiki W] [--force]`.
  - The wiki name for the pointer is resolved through
    `registry.resolve(wiki, DIR)`.
  - It prints `wiki-lookup <version> installed at <path>`, or
    `… already installed at …`.

- [ ] **Step 1: Write the failing tests.**
  - The Review Focus tests 1–4.
  - `test_description_within_budget`: the description is ≤ 1024 chars and
    contains "wiki".
  - `test_frontmatter_fields`: the exact `allowed-tools`, `disallowed-tools`,
    `name` and version stamp.
  - `test_user_dir_honours_claude_config_dir`.
  - `test_pointer_at_most_15_lines`.
  - `test_cli_skill_install_project`.
  - `test_package_data_complete`: every `templates/**` and `prompts/*` file
    listed in the source tree is readable through
    `importlib.resources.files("llm_wiki")`.
- [ ] **Step 2: Implement.** Write the skill body in the template, with the §7
      steps and answer rules from Global Constraints.
- [ ] **Step 3: Run** `.venv/bin/pytest -q`, `ruff check .` and
      `ruff format --check`. Expected: all pass. Commit:
      `feat: add wiki skill install with the read-only wiki-lookup skill`.

### Task 3: Packaging + clean-venv check

**Files:** `pyproject.toml` (description, readme, keywords, classifiers, `urls`
left out), create `README.md`, the spec's §11 note

- [ ] **Step 1: Update the package.**
  - `description`:

    ```toml
    description = "CLI that keeps small, capped, LLM-maintained wikis for coding agents"
    ```

  - `readme = "README.md"`.
  - The README covers install, quick start (`init`, `ingest`, `search`,
    `skill install`), and a migration note: wikis created before 0.1.0 keep
    their `AGENTS.md`; compare it with the packaged template and copy the new
    rows by hand.
- [ ] **Step 2: Build the wheel.** Run
      `.venv/bin/pip wheel --no-deps -w $SCRATCH/dist .`. Expected:
      `llm_wiki-<v>-py3-none-any.whl`, whose `unzip -l` listing has
      `templates/skill-wiki-lookup.md` and `prompts/plan.md`.
- [ ] **Step 3: Check it in a clean venv.**
  1. `python3 -m venv $SCRATCH/cv && $SCRATCH/cv/bin/pip install $SCRATCH/dist/*.whl`.
  2. From `/`, run `wiki --version`, `wiki doctor`,
     `wiki skill install --project $SCRATCH/evalrepo`.
  3. `LLM_WIKI_HOME=$SCRATCH/home LLM_WIKI_PATH=<demo wiki> wiki status --all`
     lists the wiki.
  4. `wiki init <a clone of the demo wiki>` only registers it.

  Expected: every command exits 0. Record the output in the commit body. Commit:
  `build: package metadata and README for the first release`.

### Task 4: Real evaluation (paid, about $1)

- [ ] **Step 1:** create an unrelated scratch repo, `evalrepo`, holding a tiny
      Python app. Install the skill with `--project evalrepo`, not into the
      user's global dir, which is a shared volume. Make the M3 demo wiki
      discoverable through `LLM_WIKI_PATH`.
- [ ] **Step 2:** run 5 questions with
      `claude -p --output-format stream-json --verbose --model claude-sonnet-5-5`
      from `evalrepo`:
  1. the branch name for ticket PAY-7, a refund export;
  2. the merge strategy;
  3. how private-subnet EC2 instances reach the internet;
  4. how to remove Labels from the Jira Story issue type;
  5. which Kubernetes ingress controller the team uses (unanswerable).
- [ ] **Step 3: Pass criteria, checked from the transcripts:**
  - the skill is invoked;
  - there are ≤ 5 `Read` calls on wiki pages;
  - questions 1–4 carry `[AS-IS]` or `[TO-BE]` and a `raw/` citation;
  - question 5 says "not in wiki";
  - there are no Write or Edit calls. Tune the template if a criterion fails.
- [ ] **Step 4:** mark M4 done in spec §11 with the results. Commit:
      `docs: mark M4 done with the lookup evaluation`.
