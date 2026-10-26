"""Headless `claude -p` runner. The only module that spells CLI flags."""

import dataclasses
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from llm_wiki.config import LlmSettings, Role
from llm_wiki.errors import (
    ClaudeAuthError, ClaudeBudgetExceeded, ClaudeError, ClaudeNotInstalled, ClaudeSchemaError,
    ClaudeUsageLimit,
)

EDIT_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")

# Every flag build_argv can emit; `wiki doctor` checks them against `claude --help`.
REQUIRED_FLAGS = (
    "--output-format", "--model", "--system-prompt-file", "--tools", "--no-session-persistence",
    "--setting-sources", "--strict-mcp-config", "--permission-prompts", "--max-budget-usd",
    "--json-schema", "--permission-mode", "--restricted",
)

# Never forwarded: API keys would bill the API instead of the subscription, and the nested-session
# markers make the child believe it runs inside another Claude Code. CLAUDE_CODE_OAUTH_TOKEN is the
# subscription login and stays.
_STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


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


@dataclass(frozen=True)
class Result:
    text: str
    structured: dict | None
    cost_usd: float
    duration_ms: int
    session_id: str
    raw: dict


def make_call(
    role: Role, settings: LlmSettings, *, prompt: str, system_prompt: str,
    cwd: Path | None = None, json_schema: dict | None = None,
) -> Call:
    edit = role == "edit"
    return Call(
        prompt=prompt, system_prompt=system_prompt, model=settings.model(role),
        tools=EDIT_TOOLS if edit else (), budget_usd=settings.budget(role), cwd=cwd,
        json_schema=json_schema, edit=edit, timeout_s=900 if edit else 300, role=role,
    )


def claude_binary() -> str:
    found = os.environ.get("LLM_WIKI_CLAUDE") or shutil.which("claude")
    if not found:
        raise ClaudeNotInstalled("claude not found on PATH — install Claude Code")
    return found


def build_argv(call: Call, binary: str, system_prompt_file: Path) -> list[str]:
    argv = [
        binary, "-p", "--output-format", "json", "--model", call.model,
        "--system-prompt-file", str(system_prompt_file), "--tools", ",".join(call.tools),
        "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
        "--permission-prompts", "none", "--max-budget-usd", f"{call.budget_usd:.2f}",
    ]
    if call.json_schema:
        argv += ["--json-schema", json.dumps(call.json_schema, separators=(",", ":"), sort_keys=True)]
    if call.edit:
        argv += ["--permission-mode", "acceptEdits", "--restricted"]
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
        text=data.get("result") if isinstance(data.get("result"), str) else "",
        structured=structured if isinstance(structured, dict) else None,
        cost_usd=_num(data.get("total_cost_usd"), float, 0.0),
        duration_ms=_num(data.get("duration_ms"), int, 0),
        session_id=data.get("session_id") if isinstance(data.get("session_id"), str) else "",
        raw=data,
    )


Kind = Literal["ok", "transient", "usage_limit", "auth", "budget", "schema", "fatal"]

# Status codes need word boundaries: bare "500" would match inside timestamps and token counts.
_RULES = (
    ("budget", re.compile(r"budget")),
    ("auth", re.compile(r"/login|not logged in|invalid api key|authentication|oauth token|\b401\b")),
    ("usage_limit", re.compile(r"usage limit|(?<!rate )limit reached|limit will reset")),
    ("transient", re.compile(
        r"overloaded|\b(?:429|500|502|503|504|529)\b|rate limit|econnreset|etimedout|socket hang up"
        r"|network")),
)


def _json_object(stdout: str) -> dict | None:
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def classify(returncode: int, stdout: str, stderr: str, *, has_schema: bool = False) -> Kind:
    data = _json_object(stdout)
    failed = returncode != 0 or (data is not None and bool(data.get("is_error")))
    if not failed:
        if data is None:
            return "transient"  # exit 0 with empty or non-JSON stdout
        if has_schema and not isinstance(data.get("structured_output"), dict):
            return "schema"
        return "ok"
    if data is not None:
        text = f"{stderr} {data.get('result') or ''} {data.get('subtype') or ''}"
    else:
        text = f"{stderr} {stdout}"
    text = text.lower()
    if data is not None and data.get("subtype") == "error_max_budget_usd":
        return "budget"
    for kind, pattern in _RULES:
        if pattern.search(text):
            return kind
    return "fatal"


def _spawn(call: Call, binary: str, cwd: Path) -> subprocess.CompletedProcess | None:
    """One subprocess; None on timeout (subprocess.run has already killed the child)."""
    # The system prompt goes through a file: AGENTS.md-sized prompts can exceed the argv limit.
    fh = tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False)
    try:
        with fh:
            fh.write(call.system_prompt)
        return subprocess.run(
            build_argv(call, binary, Path(fh.name)), input=call.prompt, capture_output=True,
            text=True, encoding="utf-8", cwd=cwd, env=child_env(os.environ), timeout=call.timeout_s,
        )
    except subprocess.TimeoutExpired:
        return None
    except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
        if not cwd.is_dir() or not os.access(cwd, os.X_OK):
            raise ClaudeError(f"working directory is not an accessible directory: {cwd}") from None
        if isinstance(exc, PermissionError):
            raise ClaudeNotInstalled(f"claude at {binary} is not executable") from None
        raise ClaudeNotInstalled("claude not found on PATH — install Claude Code") from None
    finally:
        Path(fh.name).unlink(missing_ok=True)


def _snippet(stdout: str, stderr: str) -> str:
    data = _json_object(stdout)
    result = data.get("result") if data and isinstance(data.get("result"), str) else ""
    return (result.strip() or stderr.strip() or stdout.strip())[:200]


def _message(stdout: str, stderr: str) -> str:
    data = _json_object(stdout)
    text = data.get("result") if data and isinstance(data.get("result"), str) else ""
    return (text.strip() or stderr.strip() or stdout.strip())[:500]


def _raise_for(kind: Kind, call: Call, proc) -> None:
    if kind == "auth":
        raise ClaudeAuthError("claude is not logged in — run `claude /login`")
    if kind == "usage_limit":
        raise ClaudeUsageLimit(f"{_message(proc.stdout, proc.stderr)} — wait and re-run")
    if kind == "budget":
        key = f"{call.role}_budget_usd" if call.role else "the budget setting"
        raise ClaudeBudgetExceeded(
            f"claude exceeded the ${call.budget_usd:.2f} budget cap — raise {key} and re-run")
    raise ClaudeError(f"claude failed (exit {proc.returncode}): {_snippet(proc.stdout, proc.stderr)}")


def _execute(call: Call, binary: str, cwd: Path, sleep, rand) -> Result:
    max_retries = 3
    transient = 0
    schema_retried = False
    while True:
        proc = _spawn(call, binary, cwd)
        if proc is None:
            kind: Kind = "transient"
            failure = f"claude timed out after {call.timeout_s}s"
        else:
            kind = classify(proc.returncode, proc.stdout, proc.stderr, has_schema=bool(call.json_schema))
            failure = f"claude failed (exit {proc.returncode}): {_snippet(proc.stdout, proc.stderr)}"
            if kind == "ok":
                return parse_output(proc.stdout)
        if kind == "transient":
            if transient >= max_retries:
                raise ClaudeError(failure)
            transient += 1
            sleep(2 ** transient * (0.5 + rand()))
            continue
        if kind == "schema":
            if schema_retried:
                raise ClaudeSchemaError("claude reply did not match the required JSON schema: "
                                        "structured_output missing")
            schema_retried = True
            call = dataclasses.replace(call, prompt=call.prompt + (
                "\n\nYour previous reply did not match the required JSON schema: "
                "structured_output missing. Reply again with only the structured output."))
            continue
        _raise_for(kind, call, proc)


def run(call: Call, *, binary: str | None = None, sleep=time.sleep, rand=random.random) -> Result:
    binary = binary or claude_binary()
    if call.cwd is not None:
        return _execute(call, binary, call.cwd, sleep, rand)
    with tempfile.TemporaryDirectory(prefix="llm-wiki-") as tmp:  # plan calls see no files
        return _execute(call, binary, Path(tmp), sleep, rand)
