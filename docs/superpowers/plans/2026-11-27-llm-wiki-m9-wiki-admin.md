# M9: move, rename and describe a wiki

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** `wiki move NEWPATH`, `wiki rename NEWNAME` and `wiki describe TEXT`
change a wiki's path, name and description with no hand edits.

**Architecture:** a new module `manage.py` holds the three operations. `cli.py`
gets three thin commands over it. `registry.py` gains `set_path` and `rename`.
Rename and describe rewrite text that `init` wrote only when it is still
verbatim, and warn otherwise.

**Tech Stack:** Python, Typer, pytest. No LLM calls.

**Spec:** [the design](../specs/2026-10-26-llm-wiki-design.md), §2 (the three
rows) and §11 item 9.

## Global Constraints

- No new dependencies. Offline tests only; no real `claude` run, cost $0.
- Changing the profile is out of scope.
- Git: only wiki paths are staged, through `gitops.stage_commit`. Nothing is
  pushed. A wiki inside a host repo is never committed: the CLI says so, as
  `init` does.
- Exit codes: 0 ok, 2 `WikiError`.
- Names match `init.NAME_RE` (kebab-case). A description is collapsed with
  `" ".join(text.split())` and must not be empty.
- Log lines match `lint.LOG_RE`: `## [YYYY-MM-DD] rename | old → new`, and
  `## [YYYY-MM-DD] describe | <text>`.
- Commit messages: `chore: rename wiki <old> to <new>`, and
  `chore: describe wiki <name>`.

## The text `init` writes (the verbatim rewrite sites)

`D` below means the effective description,
`cfg.description or f"The {name} wiki."`. A plain description is
`cfg.description`, which may be empty.

Each site is an exact full line, except the `index.md` header block:

- `AGENTS.md`:
  - `# {name}: wiki rules` (rename);
  - `{D}` (describe, and rename¹).
- `wiki/overview.md`:
  - `summary: Entry point of the {name} wiki.` (rename);
  - `**TL;DR** {D}` (describe, and rename¹).
- `wiki/glossary.md`:
  - `summary: Terms used in the {name} wiki.` (rename);
  - `**TL;DR** Terms used across the {name} wiki.` (rename).
- `wiki/index.md`: the block above `GENERATED_MARK`, when it equals
  `f"# {name}\n\n{cfg.description}\n\n"` (both).

¹ Rename changes `D` only when `cfg.description` is empty, because then `D`
holds the name.

`build_indexes` keeps an existing `index.md` header (`index._header`), so it
never refreshes the header. That is why the header block is rewritten here.

## Review Focus

- A summary line or TL;DR edited after init (by hand or by ingest): it is left
  untouched and gets a warning naming the file. Nothing fails.
- A wiki with uncommitted changes: rename and describe refuse, so their commit
  holds only their own change. Move does not care.
- `wiki rename` run on the registry's default wiki: the default follows the new
  name.
- `wiki move` to a target inside the wiki itself (`--wiki a` → `a/sub`): it is
  refused before anything moves.
- A line that appears twice in a file: it is treated as not verbatim and gets a
  warning. It is never a partial rewrite.

---

### Task 1: registry `set_path` and `rename`

**Files:**

- Modify: `src/llm_wiki/registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**

- Produces: `set_path(name: str, path: Path) -> None`. It registers or re-points
  `name`, storing the resolved path, and keeps `default`. It is `register`
  without `make_default`, so it may simply call `register`.
- Produces: `rename(old: str, new: str) -> None`. It moves the entry and carries
  over `default` when `default == old`. Raises `WikiError` when `old` is not
  registered or `new` already is.

- [ ] **Step 1: Write the failing tests**
  - `test_rename_moves_entry_and_default`: register `a` (the default) and `b`.
    After `rename("a", "c")`, `registered_path("c")` is a's path,
    `registered_path("a") is None`, and `_read()[0] == "c"`.
  - `test_rename_keeps_other_default`: with `a` the default, `rename("b", "c")`
    leaves the default at `a`.
  - `test_rename_unknown_or_taken_raises`: `rename("x", "y")` and
    `rename("a", "b")` raise `WikiError`.
  - `test_set_path_repoints`: after `set_path("a", p2)`, a's path is
    `p2.resolve()` and the default is unchanged.
- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_registry.py -q`. Expected:
      FAIL (`AttributeError`).
- [ ] **Step 3:** Implement both in `registry.py` on top of `_read` and
      `_write`.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit: `feat: let the registry rename and re-point a wiki`.

### Task 2: `manage.describe_wiki` and the shared helpers

**Files:**

- Create: `src/llm_wiki/manage.py`
- Modify: `src/llm_wiki/init.py`: rename `_check_name_free` to the public
  `check_name_free` and update its callers.
- Test: `tests/test_manage.py`

**Interfaces:**

- Produces in `manage.py`:

  ```python
  @dataclass
  class ChangeResult:
      ref: WikiRef            # the wiki after the change
      # one per site left alone: "<rel>: <what> was edited; left as is"
      warnings: list[str]
      committed: bool         # False in a host repo
      host_repo: Path | None  # the enclosing repo when not committed

  def describe_wiki(
      ref: WikiRef, text: str, today: date | None = None
  ) -> ChangeResult
  ```

- Private helpers, which Task 3 reuses:
  - `_require_clean(root)`: raises
    `WikiError(f"{root} has uncommitted changes: commit or discard them first")`
    when `gitops.changed_paths(root)` is non-empty;
  - `_replace_line(root, rel, old, new, what, warnings) -> bool`: replaces the
    one full line equal to `old`. It appends a warning when the file is missing
    or the line occurs 0 or 2+ times. It returns True when it wrote the file;
  - `_replace_index_header(root, old_header, new_header, warnings) -> bool`;
  - `_save_config(root, cfg)`;
  - `_finish(root, ref, touched, log_line, message, warnings) -> ChangeResult`:
    it calls `gate.append_log`, then commits `touched + ["wiki/log.md"]` with
    `gitops.stage_commit` when `gitops.enclosing_repo(root) == root`.

- [ ] **Step 1: Write the failing tests** (build the wiki with
      `init.init_wiki(tmp_path / "w", name="acme", description="Old text.")`):
  - `test_describe_rewrites_every_site_and_commits`: after
    `describe_wiki(ref, "  New   text. ")`:
    - `load_config(root).description == "New text."`;
    - `AGENTS.md` line 3 is `New text.`;
    - `overview.md` contains `**TL;DR** New text.`;
    - `index.md` starts `# acme\n\nNew text.\n\n`;
    - the last log line is `## [<today>] describe | New text.`;
    - `git log -1 --format=%s` is `chore: describe wiki acme`;
    - `changed_paths(root) == []`;
    - `lint(root, cfg).errors == 0`;
    - `res.warnings == []`.
  - `test_describe_leaves_edited_overview_and_warns`: rewrite the TL;DR line of
    `overview.md` and commit it. After the describe, `overview.md` is unchanged,
    one warning names `wiki/overview.md`, and the other sites changed.
  - `test_describe_refuses_dirty_wiki`: an untracked `inbox/x.md` raises
    `WikiError`, and `wiki.toml` is unchanged.
  - `test_describe_refuses_empty_or_same`: `"   "` and `"Old text."` raise
    `WikiError`.
  - `test_describe_in_host_repo_does_not_commit`: `git init` a host first, then
    `init_wiki(host / "w")` and commit it in the host. After the describe,
    `res.committed is False`, `res.host_repo == host`, and the host's HEAD is
    unchanged.
  - `test_describe_replaces_fallback_description`: `init_wiki` with no
    description gives `The acme wiki.`. After `describe_wiki(ref, "X.")`,
    `AGENTS.md` line 3 and the overview TL;DR say `X.`.
- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_manage.py -q`. Expected: FAIL
      (`ModuleNotFoundError`).
- [ ] **Step 3:** Implement `describe_wiki` and the helpers. Its sites come from
      the list above: the `D` lines plus the index header built from the old and
      new `cfg.description`.
- [ ] **Step 4:** Run the tests, then the whole suite. Expected: PASS.
- [ ] **Step 5:** Commit: `feat: add manage.describe_wiki`.

### Task 3: `manage.rename_wiki`

**Files:**

- Modify: `src/llm_wiki/manage.py`
- Test: `tests/test_manage.py`

**Interfaces:**

- Consumes: Task 1 `registry.rename`; Task 2's helpers and `ChangeResult`;
  `init.NAME_RE` and `init.check_name_free(name, root)`.
- Produces: `rename_wiki(ref, new_name, today=None) -> ChangeResult`, with
  `ref: WikiRef`, `new_name: str` and `today: date | None`.
  `res.ref.name == new_name`, and `ref.origin` is kept.

- [ ] **Step 1: Write the failing tests**
  - `test_rename_rewrites_every_name_site`: on
    `init_wiki(..., name="acme", description="")`, run
    `rename_wiki(ref, "beta")`. Then every site above reads `beta`, including
    `The beta wiki.` in `AGENTS.md` and the overview TL;DR. Also:
    - `load_config(root).name == "beta"`;
    - `registered_path("beta") == root` and `registered_path("acme") is None`;
    - the log line is `## [<today>] rename | acme → beta`;
    - the commit subject is `chore: rename wiki acme to beta`;
    - lint has 0 errors.
  - `test_rename_keeps_custom_description`: with `description="Ours."`, the
    `Ours.` lines are untouched and no warning is raised.
  - `test_rename_moves_default`: when the wiki is the default,
    `registry.resolve(None, tmp_path)` returns `beta` afterwards.
  - `test_rename_refuses_bad_taken_or_same`: `"Bad Name"`, the name of another
    live wiki, and `"acme"` raise `WikiError`. Nothing changed on disk.
  - `test_rename_env_wiki_skips_registry`: a wiki found only through
    `LLM_WIKI_PATH` (origin `env`) is renamed in `wiki.toml`, and the registry
    file is not created or changed.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement. Validate first, then `_require_clean`, then the
      config, the sites, the registry (skipped for `env`, and registering a
      `path` origin under the new name) and `_finish`.
- [ ] **Step 4:** Run `tests/test_manage.py` and the whole suite. Expected:
      PASS.
- [ ] **Step 5:** Commit: `feat: add manage.rename_wiki`.

### Task 4: `manage.move_wiki`

**Files:**

- Modify: `src/llm_wiki/manage.py`
- Test: `tests/test_manage.py`

**Interfaces:**

- Consumes: Task 1 `registry.set_path`; `gitops.enclosing_repo`,
  `gitops.inside_git_dir`.
- Produces: `move_wiki(ref: WikiRef, new_path: Path) -> WikiRef`. It returns
  `WikiRef(name, target.resolve(), "registry")`.

- [ ] **Step 1: Write the failing tests**
  - `test_move_to_missing_path`: target `tmp_path / "a" / "b"`, whose parents
    don't exist. The old root is gone, `target / "wiki.toml"` exists,
    `registered_path("acme") == target`, `git -C target status` works, and
    `registry.resolve("acme", tmp_path).path == target`.
  - `test_move_into_empty_dir`: an existing empty target dir works.
  - `test_move_refuses_non_empty_target`, `test_move_refuses_inside_itself`
    (`root / "sub"`), `test_move_refuses_same_path`: each raises `WikiError`,
    and the root is untouched.
  - `test_move_refuses_host_repo_wiki`: the message names `git mv` and
    `wiki init`.
  - `test_move_refuses_env_wiki`: the message names `LLM_WIKI_PATH`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement. Check everything, then `mkdir` the parents. Remove
      an empty target dir before `shutil.move(root, target)`, then call
      `registry.set_path`.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit: `feat: add manage.move_wiki`.

### Task 5: CLI commands, end-to-end test and docs

**Files:**

- Modify: `src/llm_wiki/cli.py`, `tests/test_cli.py`, `tests/test_e2e_m1.py`,
  `README.md` (the "Commands" section), `AGENTS.md` (the layout list gains
  `manage.py`)
- Modify: the spec, `docs/superpowers/specs/2026-10-26-llm-wiki-design.md` §11
  item 9: add the "Done" note.

**Interfaces:**

- Consumes: the three `manage` functions and
  `registry.resolve(wiki, Path.cwd())`.
- CLI output (exact):
  - move: `moved wiki <name> to <path>`;
  - describe: `described wiki <name>`;
  - rename: `renamed wiki <old> to <new>`, then
    `update project pointers with: wiki skill install --project DIR --pointer`.
  - Each warning goes to stderr as `warning: <text>`. In a host repo, the first
    line ends with `(not committed — review and commit it in <host>)`, after one
    space.

- [ ] **Step 1: Write the failing CLI tests** with `CliRunner`:
  - each command exits 0 and prints its exact line;
  - `wiki rename "Bad Name"` exits 2;
  - `wiki move` onto a non-empty dir exits 2.
- [ ] **Step 2:** Run them. Expected: FAIL (no such command).
- [ ] **Step 3:** Add `move(new_path: Path = Argument)`,
      `rename(new_name: str = Argument)` and `describe(text: str = Argument)` to
      `cli.py`, each with `--wiki`.
- [ ] **Step 4:** Extend `tests/test_e2e_m1.py`: init → describe → rename → move
      → `status --wiki <new>` → `lint --wiki <new>` exits 0 →
      `git log --format=%s` shows both chore commits.
- [ ] **Step 5:** Docs:
  - `README.md` gets the three commands and the verbatim-only rule;
  - the `AGENTS.md` layout list gains `manage.py`;
  - spec §11 item 9 gets a dated "Done" note with the test count and "no real
    run, $0".
- [ ] **Step 6:** Run every check in the `AGENTS.md` "Setup and checks" list.
      Expected: all pass.
- [ ] **Step 7:** Commit: `feat: add wiki move, rename and describe commands`.
