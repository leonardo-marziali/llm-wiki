"""`wiki doctor`: environment checks. Offline checks make no LLM calls."""

import importlib.util
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from llm_wiki.claude import REQUIRED_FLAGS, child_env, claude_binary
from llm_wiki.config import LlmSettings
from llm_wiki.errors import ClaudeNotInstalled

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
        help_text = help_proc.stdout + help_proc.stderr
        if help_proc.returncode != 0:
            raise OSError(f"exited {help_proc.returncode}")
    except (subprocess.TimeoutExpired, OSError) as e:
        checks.append(Check("claude-flags", "fail", f"claude --help failed: {type(e).__name__}"))
        return checks
    missing = [f for f in REQUIRED_FLAGS if f not in help_text]
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


def run_doctor(settings: LlmSettings, *, probe: bool, env: Mapping[str, str] | None = None) -> list[Check]:
    checks = offline_checks(os.environ if env is None else env)
    # Task 5: paid probes (only when `probe`) are appended here, after the offline checks.
    return checks
