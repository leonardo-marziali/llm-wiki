# llm-wiki — notes for coding agents

This repo builds `wiki`, a Python CLI that keeps small, capped wikis written by
headless Claude Code (`claude -p`) and read by coding agents. The design is
[docs/superpowers/specs/2026-10-26-llm-wiki-design.md](docs/superpowers/specs/2026-10-26-llm-wiki-design.md);
read the sections that touch your change before you plan it. The user-facing
manual is [README.md](README.md).

## Layout

- `src/llm_wiki/`: the package, one small module per concern. `cli.py` is a thin
  Typer layer over them.
  - `claude.py`: the runner for headless `claude -p`.
  - `ingest.py`, `ingest_run.py`, `intake.py`, `plan.py`, `session.py`,
    `gate.py`: the ingest flow.
  - `maint.py`, `maint_job.py`, `maint_run.py`, `deep.py`, `archive.py`:
    `lint --fix`, `lint --deep` and `consolidate`.
  - `prompts/*.md`: the system prompts of the LLM calls.
  - `templates/`: what `wiki init` and `wiki skill install` write.
- `tests/`: offline pytest suite. `tests/fake_claude/claude` is a scripted
  stand-in for `claude -p`; its docstring lists the env and response keys.
- `docs/superpowers/specs/`: the design. `docs/superpowers/plans/`: one
  implementation plan per milestone (M1–M6).

## Setup and checks

The DevContainer creates `.venv`, runs `pip install -e '.[dev]'` and
`pre-commit install`; the tools on `PATH` come from its image. Outside it,
`mise install && mise run setup` does the same. Run these before you call a
change done (the VS Code task `check` runs them all):

```sh
.venv/bin/pytest -q             # offline; never reaches the real claude
ruff check src tests
ruff format --check src tests
pyright
prettier --check '*.md' 'docs/**/*.md' 'src/**/*.md'
markdownlint '*.md' 'docs/**/*.md' 'src/**/*.md'
```

- **No new dependencies without asking.** If one seems needed, edit
  `pyproject.toml` and ask the user to rebuild the DevContainer; never
  `pip install` by hand.
- The version is single-sourced in `llm_wiki/__init__.py`. Templates and prompts
  ship as package data and are read with `importlib.resources`.
- Ruff and pyright settings live in `pyproject.toml`; Prettier wraps Markdown
  prose at 80 columns (`.prettierrc.json`), and `.markdownlint.jsonc` matches.

## Rules that hold across the code

- **`claude.py` is the only module that spells `claude` CLI flags.** Never pass
  `--bare`. The child env drops API-key variables and disables auto memory.
- **Call shapes:**
  - plan and deep calls are tool-less, run in an empty temp cwd, and pass
    `--json-schema`;
  - edit calls use `make_call("edit", …)` with `cwd` = the wiki root, and are
    never retried automatically.
- **System prompts are byte-stable** for prompt caching: the wiki's `AGENTS.md`,
  then `\n\n`, then the prompt file. Anything variable (date, plan, source) goes
  on stdin.
- **Untrusted text** (sources, page text) is wrapped in a tag with a fresh
  `secrets.token_hex(8)` nonce in every prompt.
- **Models never edit** `raw/`, `AGENTS.md`, `CLAUDE.md`, `.claude/`,
  `wiki.toml`, `.wiki/`, the indexes, `log.md` or `_archive/`. The post-check
  reverts anything an edit changed outside its planned files.
- **Git safety in the CLI:** nothing is ever pushed; only wiki paths are staged
  (`git add -- <paths>`, never `-A`); a wiki inside a host repo never commits
  the host's other files.
- **Hard caps** are constants in `caps.py`. `wiki.toml` may lower them, never
  raise them.
- **Exit codes:** 0 ok, 1 findings or a refused `--yes`, 2 `WikiError`.
- **Prompt tuning goes into `src/llm_wiki/prompts/*.md`,** not into
  `templates/AGENTS.md`: existing wikis keep the `AGENTS.md` they were created
  with, and there is no automated upgrade.
- Call the runner through the module (`from llm_wiki import claude`, then
  `claude.run(...)`) so tests can monkeypatch it.

## Tests

- Write the test first, watch it fail, then make it pass.
- `tests/conftest.py` gives every test a fake git identity, a scratch
  `LLM_WIKI_HOME`, a git ceiling above `tmp_path`, and `LLM_WIKI_CLAUDE` set to
  a missing binary, so no test can reach the real, paid `claude`. Use the
  `fake_claude` fixture (with `ok(...)` responses) to script calls, and the
  `make_*` fixtures to build wikis and source files.
- Each milestone has an end-to-end test (`test_e2e_m1.py`, `test_e2e_m3.py`,
  `test_e2e_m5.py`); extend them when a flow changes.

## Real (paid) runs

Real `claude` runs cost money, so ask before you start them and state a budget
(M6 stopped at $10 total).

- Work in a scratch directory, never in this repo. Point `GIT_CONFIG_GLOBAL` at
  a scratch file with a `[user]` section and `LLM_WIKI_HOME` at a scratch home,
  instead of changing the user's config.
- Never copy the user's sample sources, their names or their content into the
  repo.
- Costs seen in M6 with `claude-sonnet-5-5`: text or csv $0.09–0.26, xlsx about
  $0.30, docx $0.40–0.90, pptx about $0.40, png about $0.25, a 66-page PDF about
  $1.10, `lint --deep` about $0.60, the doctor probes about $0.05.
- `wiki doctor` checks the real CLI's flags, login, isolation, vision and read
  denial; run it after a change to `claude.py` or `doctor.py`.

## Workflow

Work went milestone by milestone, with the Superpowers skills:

1. **Brainstorm** the change, then update the spec in `docs/superpowers/specs/`.
2. **Plan** it as `docs/superpowers/plans/<yyyy-mm-dd>-llm-wiki-m<N>-<slug>.md`:
   goal, architecture, binding rulings, then checkbox tasks.
3. **Build** it task by task with subagent-driven development, test-first, with
   a review after each task. Briefs, reports and review diffs go in
   `.superpowers/sdd/<plan-slug>/`, which stays out of git.
4. **Close** the milestone with a dated "Done" note under spec §11 that gives
   the test count, the real-run result and its cost.

- Branches are `feat/m<N>-<slug>` off `main`, in this checkout. Do not use a
  worktree: `.venv` points at this checkout.
- Commits follow Conventional Commits (`feat`, `fix`, `docs`, `refactor`,
  `test`, `chore`, …). The subject says what changed in plain words; the body
  says why, and what was broken before.
- Keep `README.md` in step with the CLI when a command or flag changes.

## Git hooks and releases

- `.pre-commit-config.yaml` installs two hooks:
  - `commit-msg` rejects a message that is not a Conventional Commit;
  - `pre-push` refuses a push to `main`.
- Every change reaches `main` through a pull request. Its title must be a
  Conventional Commit too (`.github/workflows/pr-title.yml`): a squash merge
  makes it the commit message.
- `.github/workflows/release.yml` runs python-semantic-release on every push to
  `main`, configured under `[tool.semantic_release]` in `pyproject.toml`:
  - `feat` makes a minor release, and `fix` or `perf` a patch;
  - it bumps `__version__`, writes `CHANGELOG.md`, then commits, tags `vX.Y.Z`
    and creates the GitHub release;
  - never edit `__version__` or `CHANGELOG.md` by hand.
- `.github/workflows/publish.yml` runs on that release. It tests, builds and
  uploads `llm-wiki-tool` to PyPI with trusted publishing, then attaches the
  files to the release.

## Writing style

Docs, prompts and messages use short, plain sentences: one idea each, concrete
names and numbers, no filler. Prefer a list once a sentence holds three or more
items.
