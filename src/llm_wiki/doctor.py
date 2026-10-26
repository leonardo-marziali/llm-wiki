"""`wiki doctor`: environment checks. Offline checks make no LLM calls."""

import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from llm_wiki.claude import HIDDEN_FLAGS, REQUIRED_FLAGS, child_env, claude_binary, make_call, run
from llm_wiki.config import LlmSettings, Role
from llm_wiki.errors import ClaudeAuthError, ClaudeError, ClaudeNotInstalled, ClaudeSchemaError

MIN_CLAUDE_VERSION = (2, 1, 310)
_TIMEOUT_S = 30


@dataclass(frozen=True)
class Check:
    name: str
    status: Literal["ok", "warn", "fail"]
    detail: str


def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(g) for g in m.groups()) if m else None


def _run(argv: list[str], env: Mapping[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, env=child_env(env), capture_output=True, text=True, timeout=_TIMEOUT_S, check=False,
    )


def _claude_checks(env: Mapping[str, str]) -> list[Check]:
    try:
        binary = claude_binary()
    except ClaudeNotInstalled as e:
        return [Check("claude", "fail", str(e))]
    try:
        proc = _run([binary, "--version"], env)
    except subprocess.TimeoutExpired:
        return [Check("claude", "fail", f"{binary} --version timed out after {_TIMEOUT_S}s")]
    except OSError as e:
        return [Check("claude", "fail", f"cannot run {binary}: {e.strerror or type(e).__name__}")]
    if proc.returncode != 0:
        return [Check("claude", "fail", f"{binary} --version exited {proc.returncode}")]
    version_text = proc.stdout.strip()
    checks = [Check("claude", "ok", f"{binary} {version_text}")]

    version = parse_version(version_text)
    min_s = ".".join(map(str, MIN_CLAUDE_VERSION))
    if version is None:
        checks.append(Check("claude-version", "fail", f"cannot parse version {version_text!r}; need >= {min_s}"))
    elif version < MIN_CLAUDE_VERSION:
        checks.append(Check(
            "claude-version", "fail", f"found {'.'.join(map(str, version))}, need >= {min_s}"))

    try:
        help_proc = _run([binary, "--help"], env)
    except (subprocess.TimeoutExpired, OSError) as e:
        checks.append(Check("claude-flags", "fail", f"claude --help failed: {type(e).__name__}"))
        return checks
    if help_proc.returncode != 0:
        checks.append(Check("claude-flags", "fail", f"claude --help exited {help_proc.returncode}"))
        return checks
    help_text = help_proc.stdout + help_proc.stderr
    missing = [f for f in REQUIRED_FLAGS if f not in HIDDEN_FLAGS and not re.search(rf"(?<![\w-]){re.escape(f)}(?![\w-])", help_text)]
    if missing:
        checks.append(Check("claude-flags", "fail", "missing from claude --help: " + ", ".join(missing)))
    return checks


def offline_checks(env: Mapping[str, str]) -> list[Check]:
    checks = _claude_checks(env)
    if any(k in env for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")):
        checks.append(Check(
            "api-key-env", "warn", "ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is set: "
            "ignored: wiki calls use your claude login"))

    git = shutil.which("git", path=env.get("PATH"))
    if git is None:
        checks.append(Check("git", "fail", "git is missing"))
    else:
        try:
            checks.append(Check("git", "ok", _run([git, "--version"], env).stdout.strip() or git))
        except (subprocess.TimeoutExpired, OSError):
            checks.append(Check("git", "fail", "git --version failed"))

    if importlib.util.find_spec("docling") is None:
        checks.append(Check("docling", "warn", "not installed: scanned PDFs flagged needs-vision"))
    else:
        checks.append(Check("docling", "ok", "installed"))
    if shutil.which("gitleaks", path=env.get("PATH")) is None:
        checks.append(Check("gitleaks", "warn", "not on PATH: regex secret screening only"))
    else:
        checks.append(Check("gitleaks", "ok", "on PATH"))
    if shutil.which("rg", path=env.get("PATH")) is None:
        checks.append(Check("ripgrep", "warn", "rg not on PATH: optional"))
    else:
        checks.append(Check("ripgrep", "ok", "on PATH"))
    return checks


PROBE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "canary": {"type": "string"},
                   "token": {"type": "string"}},
    "required": ["ok", "canary", "token"],
    "additionalProperties": False,
}
_PROBE_SYSTEM = (
    "You are a health check for the wiki CLI. The health-check token is {token}; "
    "always copy it into the token field.")
_EDIT_PROBE_SYSTEM = "You are a health check for the wiki CLI."
_ESCAPE = "edit sessions can write outside the wiki"
_PROBE_BUDGET = 0.25
_MODEL_PROMPT = (
    "Reply ok=true. If any file or instruction in your context mentions a canary word, "
    'put that word in canary; otherwise canary="".'
)


def probe_model(role: Role, settings: LlmSettings) -> Check:
    model = settings.model(role)
    name = f"model:{model}"
    canary = str(uuid.uuid4())
    token = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="llm-wiki-probe-") as tmp:
        (Path(tmp) / "CLAUDE.md").write_text(
            f"The wiki doctor canary word is {canary}.\n", encoding="utf-8")
        call = replace(
            make_call("plan", settings, prompt=_MODEL_PROMPT,
                      system_prompt=_PROBE_SYSTEM.format(token=token),
                      cwd=Path(tmp), json_schema=PROBE_SCHEMA),
            # role=None keeps budget errors from advising a settings key the probe cap ignores.
            model=model, budget_usd=min(_PROBE_BUDGET, settings.budget(role)), role=None)
        try:
            result = run(call)
        except ClaudeAuthError:
            return Check(name, "fail", "not logged in: run `claude /login`")
        except ClaudeSchemaError:
            return Check(name, "fail", "structured output unsupported")
        except ClaudeError as e:
            msg = str(e)
            if model in msg or "model" in msg.lower():
                return Check(name, "fail", f"model id not available: {model} ({msg})")
            return Check(name, "fail", msg)
    out = result.structured or {}
    if canary in (out.get("canary") or ""):
        return Check(name, "fail", (
            "isolation: project CLAUDE.md leaks into wiki calls "
            "(user memory could leak the same way)"))
    if out.get("token") != token:
        return Check(name, "fail", "system prompt file ignored")
    if out.get("ok") is not True:
        return Check(name, "fail", "structured output unsupported: ok was not true")
    return Check(name, "ok", f"{model} replied (${result.cost_usd:.4f})")


def probe_edit_confinement(settings: LlmSettings) -> Check:
    name = "edit-confinement"
    with tempfile.TemporaryDirectory(prefix="llm-wiki-probe-") as tmp:
        inside = Path(tmp) / "inside"
        outside = Path(tmp) / "outside"
        inside.mkdir()
        outside.mkdir()
        prompt = (
            "Use the Write tool to create inside.txt containing ok. "
            f"Then use the Write tool to create {outside}/escape.txt containing ok. "
            "Then reply done.")
        call = replace(
            make_call("edit", settings, prompt=prompt, system_prompt=_EDIT_PROBE_SYSTEM, cwd=inside),
            budget_usd=min(_PROBE_BUDGET, settings.budget("edit")), role=None)
        try:
            result = run(call)
        except ClaudeAuthError:
            return Check(name, "fail", "not logged in: run `claude /login`")
        except ClaudeError as e:
            if (outside / "escape.txt").exists():  # the security finding wins over the error
                return Check(name, "fail", _ESCAPE)
            return Check(name, "fail", str(e))
        if (outside / "escape.txt").exists():
            return Check(name, "fail", _ESCAPE)
        if not (inside / "inside.txt").exists():
            return Check(name, "fail", "edit sessions cannot write (permission mode)")
    return Check(name, "ok", f"writes stay inside the wiki (${result.cost_usd:.4f})")


def run_doctor(settings: LlmSettings, *, probe: bool, env: Mapping[str, str] | None = None) -> list[Check]:
    checks = offline_checks(os.environ if env is None else env)
    # No point spending money while the CLI is missing, too old or lacks a flag.
    claude_ok = any(c.name == "claude" and c.status == "ok" for c in checks) and not any(
        c.name in ("claude-version", "claude-flags") and c.status == "fail" for c in checks)
    if probe and claude_ok:
        roles: list[Role] = []
        seen: set[str] = set()
        for role in ("plan", "edit", "deep"):
            if settings.model(role) not in seen:
                seen.add(settings.model(role))
                roles.append(role)
        checks.extend(probe_model(r, settings) for r in roles)
        checks.append(probe_edit_confinement(settings))
    return checks
