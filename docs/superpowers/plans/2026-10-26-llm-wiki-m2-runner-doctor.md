# llm-wiki M2 — Claude Runner + `wiki doctor` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the single module that runs headless `claude -p` (argv, env,
result parsing, error classification, retries), its model and budget settings,
an offline fake `claude` for tests, and `wiki doctor`, which proves against the
real CLI that the flags, login, models, isolation and edit permissions behave as
M3 ingest will need.

**Architecture:**

- `claude.py` is the only place that knows `claude` CLI flags. It turns a `Call`
  into argv, runs it with a cleaned env and the prompt on stdin, and returns a
  parsed `Result` or raises a typed `ClaudeError`.
- `config.py` gains `LlmSettings`, merged in this order: built-in defaults, then
  `$LLM_WIKI_HOME/config.toml [llm]`, then `wiki.toml [llm]`.
- `doctor.py` runs a list of checks. The offline checks are free; the probes are
  three tiny paid calls, and `--no-probe` skips them.
- Tests never touch the network. A scripted fake `claude` executable stands in,
  selected with `LLM_WIKI_CLAUDE`.

**Tech Stack:** Python ≥ 3.12, stdlib `subprocess` / `json` / `tempfile`, Typer,
Pydantic v2, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-26-llm-wiki-design.md` (§2
`wiki doctor` row, §5.6a/c call shapes, §6 runner, §11 M2, §12 runner tests).

## Context

M1 (the deterministic core) is merged on `main` (45c52f0, 258 tests). M3 ingest
needs two LLM calls per source:

- a tool-less plan call that returns JSON;
- an edit session confined to the wiki.

Both need a runner that is safe, cheap to retry and testable offline. M2 builds
that runner and the `doctor` command that checks the real CLI before anyone pays
for an ingest. kbpipe's `runner.py` is no longer on disk (`/tmp` was wiped), so
the code is written fresh from this plan.

## Global Constraints

- Work on branch `feat/m2-runner-doctor` off `main`, in this checkout. Do not
  use a worktree, because `.venv` points at the checkout.
- **No new dependencies.** If one ever seems needed, edit `pyproject.toml` and
  ask the user to rebuild the DevContainer. Never run `pip install`.
- `claude.py` is the only module that spells CLI flags. `doctor.py` gets flag
  names from `claude.REQUIRED_FLAGS`.
- `--bare` is never passed. API-key env vars are never forwarded to the child.
- The child env removes these variables:
  - `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDECODE`,
    `CLAUDE_CODE_ENTRYPOINT`.
  - It keeps `CLAUDE_CODE_OAUTH_TOKEN`, which is subscription login and not an
    API key.
- The child env sets `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`.
- Default models: `plan` and `edit` use `claude-sonnet-5-5`; `deep` (consolidate
  and `lint --deep`) uses `claude-opus-5-5`.
- Default budgets: plan $1.00, edit $2.00, deep $3.00. The deep budget is my
  call; the spec gives none.
- User config is `$LLM_WIKI_HOME/config.toml`, the same directory as
  `wikis.toml`. The default is `platformdirs.user_config_dir("llm-wiki")`.
- Each wiki can override `[llm]` keys in its own `wiki.toml`.
- Retries:
  - Transient failures get 3 retries. The delays are `2**n` seconds (n = 1, 2,
    3), each multiplied by a jitter factor between 0.5 and 1.5.
  - A schema failure gets one retry, with the error appended to the prompt.
  - A usage limit, an auth failure or a budget overrun is never retried.
- Exit codes: 0 ok; 1 findings (a doctor check failed); 2 error (`WikiError`).
- `MIN_CLAUDE_VERSION = (2, 1, 310)`, the version tested here.
- Commit messages end with this session's attribution trailers.

## Review Focus

1. **`wiki` launched from inside a Claude Code session**, where `CLAUDECODE=1`
   and `CLAUDE_CODE_ENTRYPOINT` are set. Expected: the child does not inherit
   them, so the nested `claude -p` behaves like a top-level one. Pinned in Task
   2 (`test_child_env_strips_nested_session_markers`).
2. **A system prompt larger than Linux's 128 KiB single-argument limit**, which
   `AGENTS.md` plus the prompts could reach. Expected: it goes through
   `--system-prompt-file` and never as an argv string. Pinned in Task 2
   (`test_large_system_prompt_goes_through_file`).
3. **`claude` exits 0 but prints non-JSON or nothing** (a crash banner or a
   truncated pipe). Expected: this is treated as transient and retried; after
   the retries run out, `ClaudeError` quotes the first 200 characters of the
   output. Pinned in Task 3 (`test_unparseable_output_retried_then_error`).
4. **`claude` hangs.** Expected: after `timeout_s` the process is killed, the
   attempt counts as transient, and the error names the timeout. Pinned in Task
   3 (`test_hang_times_out_and_retries`).
5. **`ANTHROPIC_API_KEY` is exported in the user's shell.** Expected: the runner
   strips it, so calls bill the subscription, and `wiki doctor` warns that it is
   being ignored. Pinned in Task 2 (`test_child_env_strips_api_keys`) and Task 4
   (`test_doctor_warns_on_api_key_env`).

---

## File Structure

```text
src/llm_wiki/config.py      + LlmSettings, LlmOverrides, Role, home_dir(), user_config_file(), load_llm_settings(); WikiConfig.llm; dump [llm]; warn floor fix
src/llm_wiki/registry.py    registry_file() uses config.home_dir()
src/llm_wiki/errors.py      + ClaudeError, ClaudeNotInstalled, ClaudeAuthError, ClaudeUsageLimit, ClaudeBudgetExceeded, ClaudeSchemaError
src/llm_wiki/claude.py      Call, Result, make_call, build_argv, child_env, parse_output, classify, run, claude_binary, REQUIRED_FLAGS, EDIT_TOOLS
src/llm_wiki/doctor.py      Check, offline checks, probes, run_doctor
src/llm_wiki/cli.py         + `wiki doctor`
tests/fake_claude/claude    executable scripted fake (stdlib only)
tests/conftest.py           + fake_claude fixture
tests/test_llm_settings.py  tests/test_claude.py  tests/test_doctor.py
docs/superpowers/specs/2026-10-26-llm-wiki-design.md   §6 argv line updated (Task 2)
```

---

### Task 1: LLM settings (models and budgets) + cap warn-floor fix

**Files:**

- Modify: `src/llm_wiki/config.py`, `src/llm_wiki/registry.py:25-28`
- Test: `tests/test_llm_settings.py`, `tests/test_config.py`

**Interfaces:**

- Produces:
  - `Role = Literal["plan", "edit", "deep"]`.
  - `class LlmSettings(BaseModel, extra="forbid")` with fields:
    - `plan_model: str = "claude-sonnet-5-5"`,
      `edit_model: str = "claude-sonnet-5-5"`,
      `deep_model: str = "claude-opus-5-5"`;
    - `plan_budget_usd: float = 1.0`, `edit_budget_usd: float = 2.0`,
      `deep_budget_usd: float = 3.0`;
    - methods `model(role) -> str` and `budget(role) -> float`.
  - `class LlmOverrides(BaseModel, extra="forbid")` with the same fields, each
    optional (`None`).
  - `WikiConfig.llm: LlmOverrides = LlmOverrides()`.
  - `home_dir() -> Path`, using `$LLM_WIKI_HOME` or else
    `platformdirs.user_config_dir("llm-wiki")`.
  - `user_config_file() -> Path`, which returns `home_dir() / "config.toml"`.
  - `load_llm_settings(wiki_root: Path | None) -> LlmSettings`.

- [ ] **Step 1: Write failing tests** in `tests/test_llm_settings.py`:
  - `test_defaults_without_any_config`: `load_llm_settings(None)` returns
    `plan_model == "claude-sonnet-5-5"`, `deep_model == "claude-opus-5-5"` and
    `budget("edit") == 2.0`.
  - `test_user_config_overrides_defaults`: write `$LLM_WIKI_HOME/config.toml`
    containing `[llm]\nplan_model = "claude-haiku-5-5"`. Then
    `.model("plan") == "claude-haiku-5-5"` and `.model("edit")` is still the
    default.
  - `test_wiki_overrides_user_config`: the user config sets
    `plan_budget_usd = 0.5` and the wiki's `wiki.toml [llm]` sets
    `plan_budget_usd = 0.25`. The result is `0.25`.
  - `test_non_positive_budget_is_error`: `edit_budget_usd = 0` raises
    `ConfigError` matching `edit_budget_usd`.
  - `test_unknown_llm_key_is_error`: `[llm] modle = "x"` raises `ConfigError`,
    from either file.
  - `test_invalid_user_config_toml_is_error`: an unparseable `config.toml`
    raises `ConfigError` naming the file.
  - `test_dump_config_round_trips_llm`: a `WikiConfig` with
    `llm=LlmOverrides(deep_model="x")` survives `dump_config` → `load_config`
    with an equal `.llm`. With no overrides, the dump contains no `[llm]` table,
    so M1 `wiki.toml` output is byte-identical.
- [ ] **Step 2: Write the failing carry-item test**
      `test_derived_warn_never_zero` in `tests/test_config.py`. With
      `caps={"max_sources": 1}`, `effective_caps().warn_sources == 1`. Also lint
      on an empty wiki with that cap reports no `cap-sources-warn`.
- [ ] **Step 3: Run**
      `.venv/bin/pytest -q tests/test_llm_settings.py tests/test_config.py`.
      Expected: FAIL (ImportError / assertion).
- [ ] **Step 4: Implement.**
  - The derived warn level is `max(1, int(value * WARN_RATIO))`.
  - Budgets are validated `> 0` with a pydantic `field_validator`. Wrap
    `ValidationError` into `ConfigError` with the file path, following the
    pattern in `load_config`.
  - `dump_config` emits `[llm]` after `[caps]`, and only the keys that are not
    `None`, using `_s` for strings and `repr(float)` for budgets.
  - `registry.registry_file()` becomes `config.home_dir() / "wikis.toml"`
    (registry already imports config, so this adds no cycle).
- [ ] **Step 5: Run** `.venv/bin/pytest -q`. Expected: all pass (258 + new).
- [ ] **Step 6: Commit:**
      `feat: add per-role model and budget settings for claude calls`. Delete
      the max=1 bullet from the memory file `llm-wiki-carry-forward.md`.

---

### Task 2: Runner core (argv, env, output parsing) + fake claude

**Files:**

- Create: `src/llm_wiki/claude.py`, `tests/fake_claude/claude` (chmod +x),
  `tests/test_claude.py`
- Modify: `src/llm_wiki/errors.py`, `tests/conftest.py`, spec §6 argv line

**Interfaces:**

- Consumes: `LlmSettings`, `Role` (Task 1).
- Produces:
  - **Errors (`errors.py`):** `ClaudeError(WikiError)`, with subclasses
    `ClaudeNotInstalled`, `ClaudeAuthError`, `ClaudeUsageLimit`,
    `ClaudeBudgetExceeded` and `ClaudeSchemaError`.
  - **`Call`**, a `@dataclass(frozen=True)` with fields:
    - `prompt: str`, `system_prompt: str`, `model: str`;
    - `tools: tuple[str, ...]`, `budget_usd: float`;
    - `cwd: Path | None = None` (`None` means a fresh empty temp dir);
    - `json_schema: dict | None = None`, `edit: bool = False`,
      `timeout_s: int = 600`.
  - **`Result`**, a `@dataclass(frozen=True)` with fields `text: str`,
    `structured: dict | None`, `cost_usd: float`, `duration_ms: int`,
    `session_id: str` and `raw: dict`.
  - **Constants:** `EDIT_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")`, and
    `REQUIRED_FLAGS: tuple[str, ...]`, every flag that `build_argv` can emit.
  - **Functions:**
    - `make_call`:

      ```python
      make_call(role, settings, *, prompt, system_prompt, cwd=None, json_schema=None) -> Call
      ```

      The `edit` role gets `EDIT_TOOLS`, `edit=True` and `timeout_s=900`. `plan`
      and `deep` get no tools and `timeout_s=300`.

    - `claude_binary() -> str` returns `$LLM_WIKI_CLAUDE`, else
      `shutil.which("claude")`, else raises
      `ClaudeNotInstalled("claude not found on PATH — install Claude Code")`.
    - `build_argv(call, binary, system_prompt_file: Path) -> list[str]`.
    - `child_env(base: Mapping[str, str]) -> dict[str, str]`.
    - `parse_output(stdout: str) -> Result`, which raises `ClaudeError` on
      non-JSON.

**argv (exact order):**

- **Every call:**

  ```python
  [binary, "-p", "--output-format", "json", "--model", M, "--system-prompt-file", F, "--tools", ",".join(tools) or "", "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config", "--permission-prompts", "none", "--max-budget-usd", f"{budget:.2f}"]
  ```

- **Then, if `json_schema`:**
  `["--json-schema", json.dumps(schema, separators=(",", ":"), sort_keys=True)]`.
- **Then, if `edit`:** `["--permission-mode", "acceptEdits", "--restricted"]`.

These argv changes are my calls on top of spec §6:

- `--system-prompt-file` replaces `--system-prompt` (Review Focus 2);
- `--permission-prompts none` makes anything that would prompt get denied
  instead of hanging;
- `--restricted` confines edit sessions to the cwd and protects git and settings
  files.

**Fake claude (`tests/fake_claude/claude`):** a `#!/usr/bin/env python3` script
that uses only the stdlib.

- **Inputs:**
  - It reads the queue file `$FAKE_CLAUDE_QUEUE`, a JSON list of responses, pops
    the first, and rewrites the file.
  - It appends one JSON line per invocation to `$FAKE_CLAUDE_LOG`:

    ```json
    {"argv", "stdin", "cwd", "env": {the keys listed in the Global Constraints env bullets}, "system_prompt": <contents of the --system-prompt-file>}
    ```

- **Response keys:**
  - `stdout`: a dict, which is JSON-dumped, or a raw str.
  - `stderr: str` and `exit: int`.
  - `sleep: float`.
  - `write: {relpath: text}`, written relative to cwd. An absolute path is
    written as given, which lets the doctor probes simulate an escape.
  - `--version` and `--help` print `$FAKE_CLAUDE_VERSION` (default
    `2.1.310 (Claude Code)`) and `$FAKE_CLAUDE_HELP` (default: every flag in a
    stub help text).
  - An empty queue exits 3 with `fake claude: queue empty`.
- **`ok(result="done", structured=None, cost=0.01)` helper** in conftest builds
  the success dict

  ```json
  {"type": "result", "subtype": "success", "is_error": false, "result": …, "structured_output": …, "total_cost_usd": …, "duration_ms": 5, "session_id": "s1"}
  ```

- **`fake_claude` fixture** sets `LLM_WIKI_CLAUDE`, `FAKE_CLAUDE_QUEUE` and
  `FAKE_CLAUDE_LOG` under `tmp_path`. It returns an object with
  `.queue(*responses)` and `.calls() -> list[dict]`.

- [ ] **Step 1: Write failing tests** in `tests/test_claude.py`:
  - `test_plan_argv_exact`: compare the full
    `build_argv(make_call("plan", LlmSettings(), …), "claude", Path("/s"))`
    against the literal list above, with `--tools ""`, no `--permission-mode`
    and no `--bare`.
  - `test_edit_argv_adds_permission_flags`: the edit role ends with
    `--permission-mode acceptEdits --restricted` and has
    `--tools Read,Edit,Write,Glob,Grep`.
  - `test_schema_is_compact_sorted_json`.
  - `test_required_flags_cover_build_argv`: every `--x` token produced for plan,
    edit and schema calls is in `REQUIRED_FLAGS`.
  - `test_child_env_strips_api_keys`: `ANTHROPIC_API_KEY` and
    `ANTHROPIC_AUTH_TOKEN` are removed, and `CLAUDE_CODE_OAUTH_TOKEN` and `PATH`
    are kept.
  - `test_child_env_strips_nested_session_markers`: `CLAUDECODE` and
    `CLAUDE_CODE_ENTRYPOINT` are removed, and
    `CLAUDE_CODE_DISABLE_AUTO_MEMORY == "1"`.
  - `test_parse_output_success`: from the `ok(...)` shape, `text`, `structured`,
    `cost_usd == 0.01` and `session_id`.
  - `test_run_passes_prompt_on_stdin_and_system_prompt_via_file`, using
    `fake_claude`:
    - the logged stdin equals the prompt;
    - the logged system_prompt equals the input;
    - the temp file is gone after `run`.
  - `test_plan_call_cwd_is_empty_tempdir`: the logged cwd is not the test cwd,
    and the directory was empty at call time (the fake also logs
    `os.listdir(cwd)`).
  - `test_edit_call_cwd_is_given_dir`.
  - `test_large_system_prompt_goes_through_file`: with a 200 000-char system
    prompt, no argv element exceeds 131 072 bytes and the logged system_prompt
    matches.
  - `test_missing_binary_raises_not_installed`: set `LLM_WIKI_CLAUDE` to a
    nonexistent path, unset it, and empty `PATH`. The result is
    `ClaudeNotInstalled`, rendered by the CLI as exit 2.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_claude.py`. Expected: FAIL
      (ModuleNotFoundError).
- [ ] **Step 3: Implement** `claude.py`, the fake, the fixture and the errors.
  - For now,
    `run(call, *, binary=None, sleep=time.sleep, rand=random.random) -> Result`
    does a single attempt:
    - write the system prompt to a `NamedTemporaryFile` and delete it in
      `finally`;
    - call

      ```python
      subprocess.run(argv, input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=…, env=child_env(os.environ), timeout=call.timeout_s)
      ```

    - call `parse_output`.
  - Task 3 adds classification and retries.
  - A `FileNotFoundError` from `subprocess` becomes `ClaudeNotInstalled`.
- [ ] **Step 4: Update the spec §6 argv line** to the exact argv above
      (`--system-prompt-file`, `--permission-prompts none`, and `--restricted`
      for edit) and add one line that names `CLAUDE_CODE_DISABLE_AUTO_MEMORY`.
- [ ] **Step 5: Run** `.venv/bin/pytest -q`. Expected: all pass.
- [ ] **Step 6: Commit:**
      `feat: add claude -p runner core and scripted fake claude`.

---

### Task 3: Error classification, retries, schema retry

**Files:**

- Modify: `src/llm_wiki/claude.py`
- Test: `tests/test_claude.py`

**Interfaces:**

- Produces:
  - `classify`:

    ```python
    classify(returncode: int, stdout: str, stderr: str) -> Literal["ok", "transient", "usage_limit", "auth", "budget", "schema", "fatal"]
    ```

  - `run(...)`: full behaviour, same signature as in Task 2.
- **Classification rules.** These are checked in this order against `stderr`
  plus `result` text plus `subtype`, case-insensitive:
  - **`budget`:** subtype `error_max_budget_usd`, or text `budget`.
  - **`auth`:** `/login`, `not logged in`, `invalid api key`, `authentication`,
    `oauth token`, or `401`.
  - **`usage_limit`:** `usage limit`, `limit reached`, or `limit will reset`.
  - **`transient`:** `overloaded`, `529`, `500`, `502`, `503`, `504`, `429`,
    `rate limit`, `econnreset`, `etimedout`, `socket hang up`, or `network`.
    Also: non-JSON stdout, empty stdout, or a timeout.
  - **`schema`:** the call had `json_schema` and the parsed result lacks a dict
    `structured_output`.
  - **`ok`:** exit 0 and `is_error` false.
  - **`fatal`:** everything else.
- **Errors raised:**
  - ``ClaudeAuthError("claude is not logged in — run `claude /login`")``.
  - `ClaudeUsageLimit`, carrying the CLI's own message, which includes the reset
    time when the CLI gives one, plus "wait and re-run".
  - `ClaudeBudgetExceeded`, naming the `$` cap and the role's settings key.
  - `ClaudeSchemaError`, raised after one retry. The retry appends

    ```text
    "\n\nYour previous reply did not match the required JSON schema: <detail>. Reply again with only the structured output."
    ```

    to the prompt.

  - `ClaudeError`: transient failures after 3 retries (4 attempts) and fatal
    failures, quoting the first 200 chars of stderr or stdout. A timeout says
    `claude timed out after <n>s`.

- [ ] **Step 1: Write failing tests**, all with `fake_claude` and `sleep`
      recorded into a list:
  - `test_transient_then_success`: one 529 response and then `ok`. Returns ok;
    one sleep in `[1.0, 3.0]`.
  - `test_transient_exhausts_after_three_retries`: four 529 responses. Raises
    `ClaudeError`, with 4 calls and 3 sleeps; the expected delays are
    `[2, 4, 8]` with `rand` pinned to `0.5`, so the factor is 1.0.
  - `test_auth_failure_not_retried`: one call; the message contains
    `claude /login`.
  - `test_usage_limit_not_retried_and_message_kept`: stderr
    `Claude AI usage limit reached|1761500000` → `ClaudeUsageLimit`, with one
    call.
  - `test_budget_exceeded`: stdout with
    `is_error: true, subtype: "error_max_budget_usd"` → `ClaudeBudgetExceeded`;
    the message contains `plan_budget_usd`.
  - `test_schema_missing_retried_once_with_error`: a schema call returns
    `ok(structured=None)` twice. Raises `ClaudeSchemaError`; the second logged
    stdin ends with the retry suffix.
  - `test_schema_retry_recovers`: the second response has `structured={"a": 1}`,
    and `result.structured == {"a": 1}`.
  - `test_unparseable_output_retried_then_error`: `stdout="<html>oops"` four
    times. The message contains `<html>oops`.
  - `test_hang_times_out_and_retries`: the call has `timeout_s=1`, and the
    responses are `{"sleep": 5}` and then `ok`. Returns ok after one retry.
  - `test_fatal_not_retried`: exit 1 and stderr `unknown option --frob` give one
    call and `ClaudeError`.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_claude.py`. Expected: the
      new tests FAIL.
- [ ] **Step 3: Implement** `classify` and the retry loop in `run`.
  - The schema retry does not consume a transient retry slot.
  - On a timeout, `subprocess.run` kills the child. Catch
    `subprocess.TimeoutExpired`.
- [ ] **Step 4: Run** `.venv/bin/pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit:**
      `feat: classify claude failures with retries and one schema retry`.

---

### Task 4: `wiki doctor` offline checks + CLI

**Files:**

- Create: `src/llm_wiki/doctor.py`, `tests/test_doctor.py`
- Modify: `src/llm_wiki/cli.py`

**Interfaces:**

- Consumes: `claude_binary`, `child_env`, `REQUIRED_FLAGS` (Task 2);
  `load_llm_settings` (Task 1); `registry.resolve`.
- Produces:
  - `Check`:

    ```python
    @dataclass(frozen=True) class Check: name: str; status: Literal["ok", "warn", "fail"]; detail: str
    ```

  - `MIN_CLAUDE_VERSION = (2, 1, 310)`.
  - `parse_version(text: str) -> tuple[int, ...] | None`.
  - `offline_checks(env: Mapping[str, str]) -> list[Check]`.
  - `run_doctor`:

    ```python
    run_doctor(settings: LlmSettings, *, probe: bool, env: Mapping[str, str]) -> list[Check]
    ```

    Task 5 fills in the probes.
- **Checks, in order. Each name is fixed:**

  | Check name       | Status | Detail                                                                                            |
  | ---------------- | ------ | ------------------------------------------------------------------------------------------------- |
  | `claude`         | ok     | binary path and version                                                                           |
  | `claude`         | fail   | not installed, or `--version` fails                                                               |
  | `claude-version` | fail   | version below the minimum; the detail shows found vs `2.1.310`                                    |
  | `claude-flags`   | fail   | the detail lists each name from `REQUIRED_FLAGS` missing from `claude --help`                     |
  | `api-key-env`    | warn   | `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` is set: "ignored: wiki calls use your claude login" |
  | `git`            | ok     | `git --version` works                                                                             |
  | `git`            | fail   | git is missing                                                                                    |
  | `docling`        | warn   | not installed: "scanned PDFs flagged needs-vision"                                                |
  | `gitleaks`       | warn   | not on PATH: "regex secret screening only"                                                        |
  | `ripgrep`        | warn   | `rg` not on PATH: "optional"                                                                      |

  When the `claude` check fails, the version and flag checks are skipped.

- **CLI:** `wiki doctor [--wiki W] [--no-probe] [--format text|json]`.
  - It resolves the wiki only when `--wiki` is given, or when the cwd is inside
    a wiki: try `registry.resolve(wiki, cwd)` and fall back to `None` on
    `WikiNotFound` only when `--wiki` is None.
  - Text output: one line per check, as `f"{status:<4}  {name:<16}  {detail}"`.
  - JSON output: a list of dicts.
  - Exits 1 if any check is `fail`, and 0 otherwise.

- [ ] **Step 1: Write failing tests**, using `fake_claude`, `monkeypatch` for
      PATH, and `CliRunner`:
  - `test_parse_version`: `"2.1.310 (Claude Code)"` → `(2, 1, 310)`, and garbage
    → `None`.
  - `test_doctor_all_ok_offline`.
  - `test_doctor_old_version_fails`: with
    `FAKE_CLAUDE_VERSION=2.0.1 (Claude Code)`, `claude-version` fails and the
    CLI exits 1.
  - `test_doctor_missing_flag_fails`: with `FAKE_CLAUDE_HELP` lacking
    `--permission-prompts`, the detail names it.
  - `test_doctor_missing_claude_fails_without_traceback`: CLI exit 1, with a
    `claude` fail line.
  - `test_doctor_warns_on_api_key_env`.
  - `test_optional_tools_missing_are_warnings_not_failures`: with PATH limited
    to the fake dir and git's dir, the CLI exits 0.
  - `test_doctor_json_format`.
  - `test_doctor_runs_outside_any_wiki`: no registry; exit 0.
  - `test_doctor_unknown_wiki_is_error`: `--wiki nope` exits 2.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_doctor.py`. Expected:
      FAIL.
- [ ] **Step 3: Implement** `doctor.py` and the `doctor` command in `cli.py`,
      following the `status` command's `--format` pattern.
  - `subprocess` calls to `claude --version` and `--help` use `child_env` and a
    30 s timeout.
- [ ] **Step 4: Run** `.venv/bin/pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit:** `feat: add wiki doctor offline checks`.

---

### Task 5: Doctor probes + real-CLI verification

Probes: login, models, schema, isolation, edit confinement.

**Files:**

- Modify: `src/llm_wiki/doctor.py`, `tests/fake_claude/claude`, which gains two
  response keys:
  - `echo_canary: true` reads the canary word from `cwd/CLAUDE.md` into
    `structured.canary`;
  - `write_abs_from_prompt: true` writes `ok` to the first absolute path found
    in stdin.
- Test: `tests/test_doctor.py`

**Interfaces:**

- Consumes: `run`, `make_call` and the error classes (Tasks 2–3).
- Produces:
  - `PROBE_SCHEMA: dict`.
  - `probe_model(role: Role, settings: LlmSettings) -> Check`.
  - `probe_edit_confinement(settings: LlmSettings) -> Check`.
  - `run_doctor(..., probe=True)` appends the probes after the offline checks,
    and only when the `claude` check is ok.

**Probes.** Each probe call uses budget `min(0.25, settings.budget(role))`.

1. **Model probes:** one `probe_model` per distinct model among plan/edit/deep,
   so 2 calls with the defaults. Each is named `model:<model-id>` and is a
   plan-shaped call.
   - **Setup:**
     - Make a temp dir with a `CLAUDE.md` containing
       `The wiki doctor canary word is <uuid4>.`
     - Pass `cwd` = that dir and `json_schema` = `PROBE_SCHEMA`, which is

       ```json
       {
         "type": "object",
         "properties": {
           "ok": { "type": "boolean" },
           "canary": { "type": "string" }
         },
         "required": ["ok", "canary"],
         "additionalProperties": false
       }
       ```

     - The prompt is:

       ```text
       Reply ok=true. If any file or instruction in your context mentions a canary word, put that word in canary; otherwise canary="".
       ```

   - **Outcomes:**
     - `ok`: structured `ok` is true and the canary is empty. The detail shows
       the model and cost.
     - `ClaudeAuthError` → fail, with "run `claude /login`".
     - A model rejected (a fatal whose text mentions the model) → fail, "model
       id not available: `<id>`".
     - `ClaudeSchemaError` → fail, "structured output unsupported".
     - The canary is echoed → fail: "isolation: project CLAUDE.md leaks into
       wiki calls". The probe adds this to the detail: user memory could leak
       the same way.
2. **Edit-confinement probe** (`edit-confinement`): a `make_call("edit", …)`
   with `cwd` = temp `inside/`, plus a sibling temp `outside/`.
   - **Prompt:**

     ```text
     Use the Write tool to create inside.txt containing ok. Then use the Write tool to create <abs outside>/escape.txt containing ok. Then reply done.
     ```

   - **Outcomes:**
     - ok: `inside/inside.txt` exists and `outside/escape.txt` does not.
     - fail "edit sessions cannot write (permission mode)": `inside.txt` is
       missing.
     - fail "edit sessions can write outside the wiki": `escape.txt` exists.

- [ ] **Step 1: Write failing tests**, all with the fake:
  - `test_probe_model_ok`.
  - `test_probe_model_detects_canary_leak`: the fake returns
    `structured={"ok": true, "canary": <the uuid>}`. The test reads the uuid
    back from the logged cwd's `CLAUDE.md`, and the fake's response reads the
    canary from the file via a `"echo_canary": true` response key.
  - `test_probe_model_auth_fail`.
  - `test_probe_model_unknown_model_fail`.
  - `test_probes_dedupe_models`: the defaults make 2 model calls plus 1 edit
    call.
  - `test_edit_probe_ok`: the fake `write: {"inside.txt": "ok"}`.
  - `test_edit_probe_detects_escape`: the fake also writes the absolute escape
    path, which is passed to the fake through a `"write_abs_from_prompt": true`
    key that parses the path from stdin.
  - `test_edit_probe_detects_no_write`.
  - `test_no_probe_makes_no_calls`: `fake_claude.calls()` is empty.
- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_doctor.py`. Expected: the
      new tests FAIL.
- [ ] **Step 3: Implement** the probes. Remove the temp dirs in `finally`.
- [ ] **Step 4: Run** `.venv/bin/pytest -q`. Expected: all pass.
- [ ] **Step 5: Real-CLI verification (paid, about 3 small calls).** Run
      `.venv/bin/wiki doctor` against the installed `claude` 2.1.310.
  - Expected: every line `ok`, or `warn` for optional tools only, and exit 0.
  - If a flag is rejected (for example `--setting-sources ""`), or the canary
    leaks, or the edit escapes, fix `build_argv` and the matching Task 2 test,
    then re-run.
  - Record the final output in the commit body. Do not mark this step done on
    fake results.
- [ ] **Step 6: Commit:**
      `feat: add wiki doctor probes for login, models, isolation and edit confinement`.

---

## Verification (end of M2)

- `.venv/bin/pytest -q`: all green, offline, with no network.
- `.venv/bin/wiki doctor --no-probe`: exit 0.
- `.venv/bin/wiki doctor`: exit 0 against the real CLI; the output is pasted in
  the Task 5 commit.
- `grep -rn -- '--' src/llm_wiki/*.py`: CLI flag literals appear only in
  `claude.py`.
- Whole-branch review, then fast-forward merge to `main` (as with M1).

## Execution

Subagent-driven, the same as M1: an implementer, then a reviewer per task, then
a final branch review. After approval, the first action is to save this plan as
`docs/superpowers/plans/2026-10-26-llm-wiki-m2-runner-doctor.md` and commit it
on `feat/m2-runner-doctor`.
