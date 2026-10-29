"""Headless `claude -p` runner. The only module that spells CLI flags."""

import base64
import contextlib
import dataclasses
import json
import os
import random
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from llm_wiki.config import LlmSettings, Role
from llm_wiki.errors import (
    ClaudeAuthError,
    ClaudeBudgetExceeded,
    ClaudeError,
    ClaudeNotInstalled,
    ClaudeSchemaError,
    ClaudeUsageLimit,
)
from llm_wiki.images import prepare_for_vision

EDIT_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")

# Every flag build_argv can emit; `wiki doctor` checks them against `claude --help`.
REQUIRED_FLAGS = (
    "--output-format",
    "--input-format",
    "--verbose",
    "--model",
    "--system-prompt-file",
    "--tools",
    "--no-session-persistence",
    "--setting-sources",
    "--strict-mcp-config",
    "--permission-prompts",
    "--max-budget-usd",
    "--json-schema",
    "--permission-mode",
    "--restricted",
    "--disallowed-tools",
)

# Accepted by claude 2.1.310 but absent from --help; doctor's model probe proves it is
# honoured.
HIDDEN_FLAGS = ("--system-prompt-file",)

# Never forwarded: API keys would bill the API instead of the subscription, and the
# nested-session markers make the child believe it runs inside another Claude Code.
# CLAUDE_CODE_OAUTH_TOKEN is the subscription login and stays.
_STRIPPED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
)


@dataclass(frozen=True)
class Call:
    prompt: str
    system_prompt: str
    model: str
    tools: tuple[str, ...]
    budget_usd: float
    cwd: Path | None = None  # None: a fresh empty temp dir
    json_schema: dict | None = None
    edit: bool = False
    timeout_s: float = 600
    role: Role | None = None  # names the settings key in budget errors
    max_retries: int = 3  # transient retries; the one schema retry is separate
    images: tuple[Path, ...] = ()  # raw files; `run` prepares them for the model
    deny_read: tuple[str, ...] = ()  # permission rules, `deny_rules`; edit calls only


@dataclass(frozen=True)
class Result:
    text: str
    structured: dict | None
    cost_usd: float
    duration_ms: int
    session_id: str
    raw: dict


def make_call(
    role: Role,
    settings: LlmSettings,
    *,
    prompt: str,
    system_prompt: str,
    cwd: Path | None = None,
    json_schema: dict | None = None,
    images: tuple[Path, ...] = (),
    deny_read: tuple[str, ...] = (),
) -> Call:
    edit = role == "edit"
    # Edit sessions are never re-run: a half-applied edit is ingest's
    # post-check/rollback to handle.
    return Call(
        prompt=prompt,
        system_prompt=system_prompt,
        model=settings.model(role),
        tools=EDIT_TOOLS if edit else (),
        budget_usd=settings.budget(role),
        cwd=cwd,
        json_schema=json_schema,
        edit=edit,
        timeout_s=900 if edit else 300,
        role=role,
        max_retries=0 if edit else 3,
        images=images,
        deny_read=deny_read,
    )


def deny_rules(rels: Iterable[str]) -> tuple[str, ...]:
    """Deny-read permission rules for paths relative to the call's cwd; a path
    ending in ``/`` denies the whole folder.

    The ``./`` form is relative to the working directory, so the wiki root's own
    path (spaces included) never enters a rule. Checked on claude 2.1.312: a
    ``Read`` deny also keeps the file out of Grep, so no ``Grep`` rule is needed;
    `wiki doctor`'s read-denial probe proves it.
    """
    return tuple(
        sorted({f"Read(./{r}**)" if r.endswith("/") else f"Read(./{r})" for r in rels})
    )


def claude_binary() -> str:
    override = os.environ.get("LLM_WIKI_CLAUDE")
    if override:
        if "/" not in override:  # a bare command name is looked up on PATH
            found = shutil.which(override)
            if not found:
                raise ClaudeNotInstalled(
                    f"LLM_WIKI_CLAUDE={override} not found on PATH"
                )
            return found
        if not Path(override).exists():
            raise ClaudeNotInstalled(
                f"LLM_WIKI_CLAUDE points to a missing file: {override}"
            )
        return override
    found = shutil.which("claude")
    if not found:
        raise ClaudeNotInstalled("claude not found on PATH — install Claude Code")
    return found


# Images travel as stream-json blocks on stdin; its documented pairing is stream-json
# output, which needs --verbose.
_TEXT_IO = ("--output-format", "json")
_IMAGE_IO = (
    "--output-format",
    "stream-json",
    "--verbose",
    "--input-format",
    "stream-json",
)


def build_argv(call: Call, binary: str, system_prompt_file: Path) -> list[str]:
    argv = [
        binary,
        "-p",
        *(_IMAGE_IO if call.images else _TEXT_IO),
        "--model",
        call.model,
        "--system-prompt-file",
        str(system_prompt_file),
        "--tools",
        ",".join(call.tools),
        "--no-session-persistence",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--permission-prompts",
        "none",
        "--max-budget-usd",
        f"{call.budget_usd:.2f}",
    ]
    if call.json_schema:
        argv += [
            "--json-schema",
            json.dumps(call.json_schema, separators=(",", ":"), sort_keys=True),
        ]
    if call.edit:
        argv += ["--permission-mode", "acceptEdits", "--restricted"]
        if call.deny_read:
            argv += ["--disallowed-tools", *call.deny_read]
    return argv


def child_env(base: Mapping[str, str]) -> dict[str, str]:
    env = {k: v for k, v in base.items() if k not in _STRIPPED_ENV}
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


def _num(value, kind, default):
    try:
        return kind(value or default)
    except (TypeError, ValueError, OverflowError):
        return default


def parse_output(stdout: str) -> Result:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeError(f"claude returned non-JSON output: {exc}") from None
    if not isinstance(data, dict):
        raise ClaudeError("claude returned unexpected JSON (not an object)")
    structured = data.get("structured_output")
    return Result(
        text=_str_field(data, "result"),
        structured=structured if isinstance(structured, dict) else None,
        cost_usd=_num(data.get("total_cost_usd"), float, 0.0),
        duration_ms=_num(data.get("duration_ms"), int, 0),
        session_id=_str_field(data, "session_id"),
        raw=data,
    )


def _str_field(data: dict | None, key: str) -> str:
    """``data[key]`` when it is a string, else ""."""
    value = data.get(key) if data else None
    return value if isinstance(value, str) else ""


Kind = Literal["ok", "transient", "usage_limit", "auth", "budget", "schema", "fatal"]

# Status codes need word boundaries: bare "500" would match inside timestamps and token
# counts. Bare "budget" would misread usage errors such as "option --max-budget-usd:
# invalid number".
_RULES: tuple[tuple[Kind, re.Pattern[str]], ...] = (
    ("budget", re.compile(r"exceeded.{0,40}budget|budget.{0,40}exceeded")),
    (
        "auth",
        re.compile(
            r"/login|not logged in|invalid api key|authentication|oauth token|\b401\b"
        ),
    ),
    (
        "usage_limit",
        re.compile(r"usage limit|(?<!rate )limit reached|limit will reset"),
    ),
    (
        "transient",
        re.compile(
            r"overloaded|\b(?:429|500|502|503|504|529)\b|rate limit"
            r"|econnreset|etimedout|socket hang up"
            r"|network"
        ),
    ),
)


def _json_object(stdout: str) -> dict | None:
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _success_kind(data: dict | None, has_schema: bool) -> Kind:
    if data is None:
        return "transient"  # exit 0 with empty or non-JSON stdout
    if has_schema and not isinstance(data.get("structured_output"), dict):
        return "schema"
    return "ok"


def _failure_text(data: dict | None, stdout: str, stderr: str) -> str:
    if data is not None:
        text = f"{stderr} {data.get('result') or ''} {data.get('subtype') or ''}"
    else:
        text = f"{stderr} {stdout}"
    return text.lower()


def classify(
    returncode: int, stdout: str, stderr: str, *, has_schema: bool = False
) -> Kind:
    data = _json_object(stdout)
    failed = returncode != 0 or (data is not None and bool(data.get("is_error")))
    if not failed:
        return _success_kind(data, has_schema)
    if data is not None and data.get("subtype") == "error_max_budget_usd":
        return "budget"
    text = _failure_text(data, stdout, stderr)
    for kind, pattern in _RULES:
        if pattern.search(text):
            return kind
    return "fatal"


def _write_prompt_file(text: str) -> Path:
    """The system prompt as a temp file the caller deletes."""
    try:
        fd, name = tempfile.mkstemp(suffix=".md")
    except OSError as exc:
        raise ClaudeError(f"cannot create the system prompt temp file: {exc}") from None
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise ClaudeError(f"cannot write the system prompt temp file: {exc}") from None
    return path


_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def _stdin_text(call: Call) -> str:
    """The prompt, or for an image call one stream-json user message line."""
    if not call.images:
        return call.prompt
    content: list[dict] = [{"type": "text", "text": call.prompt}]
    for path in call.images:
        media_type = _MEDIA_TYPES.get(path.suffix.lower())
        if media_type is None:
            raise ClaudeError(f"image was not prepared for the model: {path.name}")
        try:
            data = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            raise ClaudeError(f"cannot read image {path.name}: {exc}") from None
        source = {"type": "base64", "media_type": media_type, "data": data}
        content.append({"type": "image", "source": source})
    message = {"type": "user", "message": {"role": "user", "content": content}}
    return json.dumps(message, separators=(",", ":")) + "\n"


def _final_result(stdout: str) -> str:
    """The last `type: result` line of a stream-json stdout, or "" if there is none."""
    for line in reversed(stdout.splitlines()):
        data = _json_object(line)
        if data is not None and data.get("type") == "result":
            return line
    return ""


def _communicate(
    proc: subprocess.Popen, stdin: str, timeout_s: float
) -> subprocess.CompletedProcess | None:
    """Feed stdin and collect the output. On timeout, kill the child's whole
    process group (the tool processes it started included), reap it, give None.
    Ctrl-C and other exits kill it too: in its own session, the terminal's
    SIGINT never reaches it, and an edit must not outlive a rollback.
    """
    try:
        stdout, stderr = proc.communicate(stdin, timeout=timeout_s)
    except BaseException as exc:
        with contextlib.suppress(ProcessLookupError):  # the group is already gone
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        if isinstance(exc, subprocess.TimeoutExpired):
            return None
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _spawn(call: Call, binary: str, cwd: Path) -> subprocess.CompletedProcess | None:
    """One subprocess; None on timeout (its process group has been killed)."""
    # The system prompt goes through a file: AGENTS.md-sized prompts can exceed the argv
    # limit.
    prompt_file = _write_prompt_file(call.system_prompt)
    try:
        # every exit code is classified by the caller; its own session, so a
        # timeout can kill the grandchildren too
        with subprocess.Popen(
            build_argv(call, binary, prompt_file),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=cwd,
            env=child_env(os.environ),
            start_new_session=True,
        ) as proc:
            return _communicate(proc, _stdin_text(call), call.timeout_s)
    except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
        if not cwd.is_dir() or not os.access(cwd, os.X_OK):
            raise ClaudeError(
                f"working directory is not an accessible directory: {cwd}"
            ) from None
        if isinstance(exc, PermissionError):
            raise ClaudeNotInstalled(f"claude at {binary} is not executable") from None
        if binary == os.environ.get("LLM_WIKI_CLAUDE"):
            raise ClaudeNotInstalled(
                f"LLM_WIKI_CLAUDE points to a missing file: {binary}"
            ) from None
        raise ClaudeNotInstalled(
            f"claude not found at {binary} — install Claude Code"
        ) from None
    except OSError as exc:  # ENOEXEC (not an executable format), E2BIG, ...
        raise ClaudeError(
            f"cannot execute claude at {binary}: {exc.strerror or exc}"
        ) from None
    finally:
        prompt_file.unlink(missing_ok=True)


def _snippet(stdout: str, stderr: str) -> str:
    result = _str_field(_json_object(stdout), "result")
    return (result.strip() or stderr.strip() or stdout.strip())[:200]


def _message(stdout: str, stderr: str) -> str:
    text = _str_field(_json_object(stdout), "result")
    return (text.strip() or stderr.strip() or stdout.strip())[:500]


def _raise_for(kind: Kind, call: Call, proc) -> None:
    if kind == "auth":
        raise ClaudeAuthError("claude is not logged in — run `claude /login`")
    if kind == "usage_limit":
        raise ClaudeUsageLimit(
            f"{_message(proc.stdout, proc.stderr)} — wait and re-run"
        )
    if kind == "budget":
        key = f"{call.role}_budget_usd" if call.role else "the budget setting"
        raise ClaudeBudgetExceeded(
            f"claude exceeded the ${call.budget_usd:.2f} budget cap — "
            f"raise {key} and re-run"
        )
    raise ClaudeError(
        f"claude failed (exit {proc.returncode}): {_snippet(proc.stdout, proc.stderr)}"
    )


def _attempt(
    call: Call, binary: str, cwd: Path
) -> tuple[Kind, str, float, subprocess.CompletedProcess | None]:
    """One run: its kind, failure message, cost and process (None on timeout)."""
    proc = _spawn(call, binary, cwd)
    if proc is None:
        return "transient", f"claude timed out after {call.timeout_s}s", 0.0, None
    if call.images:  # stream-json: only the final result line is the reply
        proc = subprocess.CompletedProcess(
            proc.args, proc.returncode, _final_result(proc.stdout), proc.stderr
        )
    data = _json_object(proc.stdout)
    cost = _num(data.get("total_cost_usd"), float, 0.0) if data else 0.0
    kind = classify(
        proc.returncode, proc.stdout, proc.stderr, has_schema=bool(call.json_schema)
    )
    snippet = _snippet(proc.stdout, proc.stderr)
    return kind, f"claude failed (exit {proc.returncode}): {snippet}", cost, proc


def _with_schema_reminder(call: Call) -> Call:
    # Sonar types dataclasses.replace() as DataclassInstance; it returns a Call
    return dataclasses.replace(  # NOSONAR
        call,
        prompt=call.prompt
        + (
            "\n\nYour previous reply did not match the required JSON schema: "
            "structured_output missing. "
            "Reply again with only the structured output."
        ),
    )


def _execute(call: Call, binary: str, cwd: Path, sleep, rand) -> Result:
    transient = 0
    schema_retried = False
    spent = 0.0  # every attempt bills, not only the one that succeeds
    try:
        while True:
            kind, failure, cost, proc = _attempt(call, binary, cwd)
            spent += cost
            if kind == "ok":
                assert proc is not None
                result = parse_output(proc.stdout)
                # Sonar types dataclasses.replace() as DataclassInstance; it returns
                # a Result
                return dataclasses.replace(result, cost_usd=spent)  # NOSONAR
            if kind == "transient":
                if transient >= call.max_retries:
                    raise ClaudeError(failure)
                transient += 1
                sleep(2**transient * (0.5 + rand()))
                continue
            if kind == "schema":
                if schema_retried:
                    raise ClaudeSchemaError(
                        "claude reply did not match the required JSON schema: "
                        "structured_output missing"
                    )
                schema_retried = True
                call = _with_schema_reminder(call)
                continue
            _raise_for(kind, call, proc)
    except ClaudeError as exc:
        exc.cost_usd = spent  # the ingest gate's cost line counts failed calls too
        raise


def _temp_dir(stack: contextlib.ExitStack, prefix: str) -> Path:
    try:
        tmp = tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True)
    except OSError as exc:
        raise ClaudeError(f"cannot create a temp directory: {exc}") from None
    return Path(stack.enter_context(tmp))


def run(
    call: Call, *, binary: str | None = None, sleep=time.sleep, rand=random.random
) -> Result:
    binary = binary or claude_binary()
    with contextlib.ExitStack() as stack:
        if call.images:
            # Prepared copies stay out of the cwd, which an edit call shows the model.
            img_dir = _temp_dir(stack, "llm-wiki-img-")
            try:
                prepared = tuple(prepare_for_vision(p, img_dir) for p in call.images)
            except OSError as exc:
                raise ClaudeError(f"cannot prepare images: {exc}") from None
            # Sonar types dataclasses.replace() as DataclassInstance; it returns a Call
            call = dataclasses.replace(call, images=prepared)  # NOSONAR
        # Plan calls see no files: a fresh empty temp dir.
        cwd = call.cwd if call.cwd is not None else _temp_dir(stack, "llm-wiki-")
        return _execute(call, binary, cwd, sleep, rand)
