"""Headless `claude -p` runner. The only module that spells CLI flags."""

import json
import os
import random
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from llm_wiki.config import LlmSettings, Role
from llm_wiki.errors import ClaudeError, ClaudeNotInstalled

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
    timeout_s: int = 600


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
        json_schema=json_schema, edit=edit, timeout_s=900 if edit else 300,
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


def parse_output(stdout: str) -> Result:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeError(f"claude returned non-JSON output: {exc}") from None
    if not isinstance(data, dict):
        raise ClaudeError("claude returned unexpected JSON (not an object)")
    return Result(
        text=data.get("result") or "",
        structured=data.get("structured_output"),
        cost_usd=float(data.get("total_cost_usd") or 0.0),
        duration_ms=int(data.get("duration_ms") or 0),
        session_id=data.get("session_id") or "",
        raw=data,
    )


def _attempt(call: Call, binary: str, cwd: Path) -> Result:
    # The system prompt goes through a file: AGENTS.md-sized prompts can exceed the argv limit.
    with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as fh:
        fh.write(call.system_prompt)
    try:
        proc = subprocess.run(
            build_argv(call, binary, Path(fh.name)), input=call.prompt, capture_output=True,
            text=True, encoding="utf-8", cwd=cwd, env=child_env(os.environ), timeout=call.timeout_s,
        )
    except FileNotFoundError:
        raise ClaudeNotInstalled("claude not found on PATH — install Claude Code") from None
    finally:
        Path(fh.name).unlink(missing_ok=True)
    return parse_output(proc.stdout)


def run(call: Call, *, binary: str | None = None, sleep=time.sleep, rand=random.random) -> Result:
    binary = binary or claude_binary()
    if call.cwd is not None:
        return _attempt(call, binary, call.cwd)
    with tempfile.TemporaryDirectory(prefix="llm-wiki-") as tmp:  # plan calls see no files
        return _attempt(call, binary, Path(tmp))
