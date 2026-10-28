"""`wiki doctor`: environment checks. Offline checks make no LLM calls."""

import importlib.util
import os
import random
import re
import shutil
import string
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from llm_wiki.claude import (
    HIDDEN_FLAGS,
    REQUIRED_FLAGS,
    Call,
    Result,
    child_env,
    claude_binary,
    make_call,
    run,
)
from llm_wiki.config import LlmSettings, Role
from llm_wiki.errors import (
    ClaudeAuthError,
    ClaudeError,
    ClaudeNotInstalled,
    ClaudeSchemaError,
)

MIN_CLAUDE_VERSION = (2, 1, 310)
_TIMEOUT_S = 30


@dataclass(frozen=True)
class Check:
    name: str
    status: Literal["ok", "warn", "fail"]
    detail: str


def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.search(r"(?<!\d)(\d{1,9})\.(\d{1,9})\.(\d{1,9})", text)
    return tuple(int(g) for g in m.groups()) if m else None


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        env=child_env(os.environ),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
    )


def _claude_checks() -> list[Check]:
    try:
        binary = claude_binary()
    except ClaudeNotInstalled as e:
        return [Check("claude", "fail", str(e))]
    try:
        proc = _run([binary, "--version"])
    except subprocess.TimeoutExpired:
        return [
            Check("claude", "fail", f"{binary} --version timed out after {_TIMEOUT_S}s")
        ]
    except OSError as e:
        return [
            Check(
                "claude",
                "fail",
                f"cannot run {binary}: {e.strerror or type(e).__name__}",
            )
        ]
    if proc.returncode != 0:
        return [Check("claude", "fail", f"{binary} --version exited {proc.returncode}")]
    version_text = proc.stdout.strip()
    checks = [Check("claude", "ok", f"{binary} {version_text}")]

    version = parse_version(version_text)
    min_s = ".".join(map(str, MIN_CLAUDE_VERSION))
    if version is None:
        checks.append(
            Check(
                "claude-version",
                "fail",
                f"cannot parse version {version_text!r}; need >= {min_s}",
            )
        )
    elif version < MIN_CLAUDE_VERSION:
        checks.append(
            Check(
                "claude-version",
                "fail",
                f"found {'.'.join(map(str, version))}, need >= {min_s}",
            )
        )

    try:
        help_proc = _run([binary, "--help"])
    except (subprocess.TimeoutExpired, OSError) as e:
        checks.append(
            Check("claude-flags", "fail", f"claude --help failed: {type(e).__name__}")
        )
        return checks
    if help_proc.returncode != 0:
        checks.append(
            Check(
                "claude-flags", "fail", f"claude --help exited {help_proc.returncode}"
            )
        )
        return checks
    help_text = help_proc.stdout + help_proc.stderr
    missing = [
        f
        for f in REQUIRED_FLAGS
        if f not in HIDDEN_FLAGS
        and not re.search(rf"(?<![\w-]){re.escape(f)}(?![\w-])", help_text)
    ]
    if missing:
        checks.append(
            Check(
                "claude-flags",
                "fail",
                "missing from claude --help: " + ", ".join(missing),
            )
        )
    return checks


def offline_checks() -> list[Check]:
    env = os.environ
    checks = _claude_checks()
    if any(k in env for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")):
        checks.append(
            Check(
                "api-key-env",
                "warn",
                "ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is set: "
                "ignored: wiki calls use your claude login",
            )
        )

    git = shutil.which("git", path=env.get("PATH"))
    if git is None:
        checks.append(Check("git", "fail", "git is missing"))
    else:
        try:
            checks.append(
                Check("git", "ok", _run([git, "--version"]).stdout.strip() or git)
            )
        except (subprocess.TimeoutExpired, OSError):
            checks.append(Check("git", "fail", "git --version failed"))

    if importlib.util.find_spec("docling") is None:
        checks.append(
            Check("docling", "warn", "not installed: scanned PDFs flagged needs-vision")
        )
    else:
        checks.append(Check("docling", "ok", "installed"))
    if shutil.which("gitleaks", path=env.get("PATH")) is None:
        checks.append(
            Check("gitleaks", "warn", "not on PATH: regex secret screening only")
        )
    else:
        checks.append(Check("gitleaks", "ok", "on PATH"))
    if shutil.which("rg", path=env.get("PATH")) is None:
        checks.append(Check("ripgrep", "warn", "rg not on PATH: optional"))
    else:
        checks.append(Check("ripgrep", "ok", "on PATH"))
    return checks


PROBE_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "canary": {"type": "string"},
        "token": {"type": "string"},
    },
    "required": ["ok", "canary", "token"],
    "additionalProperties": False,
}
_PROBE_SYSTEM = (
    "You are a health check for the wiki CLI. The health-check token is {token}; "
    "always copy it into the token field."
)
_EDIT_PROBE_SYSTEM = "You are a health check for the wiki CLI."
_ESCAPE = "edit sessions can write outside the wiki"
_NOT_LOGGED_IN = "not logged in: run `claude /login`"
_PROBE_BUDGET = 0.25
_PROBE_TIMEOUT_S = 120
_MODEL_PROMPT = (
    "Reply ok=true. If any file or instruction in your context mentions a canary word, "
    'put that word in canary; otherwise canary="".'
)


def _probe_call(call: Call, role: Role, settings: LlmSettings) -> Call:
    # One short attempt: a probe that fails is reported, not retried at the user's
    # expense. role=None keeps budget errors from advising a settings key the probe cap
    # ignores.
    # Sonar types dataclasses.replace() as DataclassInstance; it returns a Call
    return replace(  # NOSONAR
        call,
        budget_usd=min(_PROBE_BUDGET, settings.budget(role)),
        role=None,
        timeout_s=_PROBE_TIMEOUT_S,
        max_retries=0,
    )


def _spawn_fail(name: str, e: OSError) -> Check:
    return Check(name, "fail", f"cannot run claude: {e.strerror or e}")


def _temp_fail(name: str, e: OSError) -> Check:
    return Check(name, "fail", f"cannot create probe temp files: {e}")


def probe_model(role: Role, settings: LlmSettings) -> Check:
    model = settings.model(role)
    name = f"model:{model}"
    canary = str(uuid.uuid4())
    token = uuid.uuid4().hex
    try:
        with tempfile.TemporaryDirectory(
            prefix="llm-wiki-probe-", ignore_cleanup_errors=True
        ) as tmp:
            (Path(tmp) / "CLAUDE.md").write_text(
                f"The wiki doctor canary word is {canary}.\n", encoding="utf-8"
            )
            # Sonar types dataclasses.replace() as DataclassInstance; it returns a Call
            call = _probe_call(
                replace(  # NOSONAR
                    make_call(
                        "plan",
                        settings,
                        prompt=_MODEL_PROMPT,
                        system_prompt=_PROBE_SYSTEM.format(token=token),
                        cwd=Path(tmp),
                        json_schema=PROBE_SCHEMA,
                    ),
                    model=model,
                ),
                role,
                settings,
            )
            try:
                result = run(call)
            except ClaudeAuthError:
                return Check(name, "fail", _NOT_LOGGED_IN)
            except OSError as e:  # a spawn failure, not a temp-file one
                return _spawn_fail(name, e)
            except ClaudeSchemaError:
                return Check(name, "fail", "structured output unsupported")
            except ClaudeError as e:
                msg = str(e)
                if model in msg or "model" in msg.lower():
                    return Check(
                        name, "fail", f"model id not available: {model} ({msg})"
                    )
                return Check(name, "fail", msg)
    except OSError as e:
        return _temp_fail(name, e)
    out = result.structured or {}
    if canary in (out.get("canary") or ""):
        return Check(
            name,
            "fail",
            (
                "isolation: project CLAUDE.md leaks into wiki calls "
                "(user memory could leak the same way)"
            ),
        )
    if out.get("token") != token:
        return Check(name, "fail", "system prompt file ignored")
    if out.get("ok") is not True:
        return Check(name, "fail", "structured output unsupported: ok was not true")
    return Check(name, "ok", f"{model} replied (${result.cost_usd:.4f})")


VISION_SCHEMA = {
    "type": "object",
    "properties": {"canary": {"type": "string"}},
    "required": ["canary"],
    "additionalProperties": False,
}
_VISION_SYSTEM = "You are a health check for the wiki CLI."
_VISION_PROMPT = (
    "The image shows one word in capital letters. Copy that word exactly into canary."
)


def _word_png(path: Path, word: str) -> None:
    # Local imports keep PIL off the path of every other doctor run.
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (460, 140), "white")
    font = ImageFont.load_default(size=64)
    ImageDraw.Draw(img).text((20, 30), word, fill="black", font=font)
    img.save(path)


def probe_vision(settings: LlmSettings) -> Check:
    name = "vision"
    word = "".join(random.choices(string.ascii_uppercase, k=5))
    try:
        with tempfile.TemporaryDirectory(
            prefix="llm-wiki-probe-", ignore_cleanup_errors=True
        ) as tmp:
            png = Path(tmp) / "word.png"
            _word_png(png, word)
            call = _probe_call(
                make_call(
                    "plan",
                    settings,
                    prompt=_VISION_PROMPT,
                    system_prompt=_VISION_SYSTEM,
                    json_schema=VISION_SCHEMA,
                    images=(png,),
                ),
                "plan",
                settings,
            )
            try:
                result = run(call)
            except ClaudeAuthError:
                return Check(name, "fail", _NOT_LOGGED_IN)
            except OSError as e:  # a spawn failure, not a temp-file one
                return _spawn_fail(name, e)
            except ClaudeSchemaError:
                return Check(name, "fail", "structured output unsupported")
            except ClaudeError as e:
                return Check(name, "fail", str(e))
    except OSError as e:
        return _temp_fail(name, e)
    if (result.structured or {}).get("canary") != word:
        return Check(name, "fail", "images do not reach the plan call")
    return Check(
        name, "ok", f"{settings.model('plan')} read the image (${result.cost_usd:.4f})"
    )


def _denied(result: Result, path: Path) -> bool:
    """True if claude reports refusing a tool call that targeted path."""
    denials = result.raw.get("permission_denials")
    for d in denials if isinstance(denials, list) else ():
        tool_input = d.get("tool_input") if isinstance(d, dict) else None
        target = tool_input.get("file_path") if isinstance(tool_input, dict) else None
        if isinstance(target, str) and Path(target).resolve() == path.resolve():
            return True
    return False


def probe_edit_confinement(settings: LlmSettings) -> Check:
    name = "edit-confinement"
    try:
        with tempfile.TemporaryDirectory(
            prefix="llm-wiki-probe-", ignore_cleanup_errors=True
        ) as tmp:
            inside = Path(tmp) / "inside"
            escape = Path(tmp) / "outside" / "escape.txt"
            inside.mkdir()
            escape.parent.mkdir()
            prompt = (
                "Use the Write tool to create inside.txt containing ok. "
                f"Then use the Write tool to create {escape} containing ok. "
                "Then reply done."
            )
            call = _probe_call(
                make_call(
                    "edit",
                    settings,
                    prompt=prompt,
                    system_prompt=_EDIT_PROBE_SYSTEM,
                    cwd=inside,
                ),
                "edit",
                settings,
            )
            try:
                result = run(call)
            except ClaudeAuthError:
                return Check(name, "fail", _NOT_LOGGED_IN)
            except OSError as e:  # a spawn failure, not a temp-file one
                return _spawn_fail(name, e)
            except ClaudeError as e:
                if escape.exists():  # the security finding wins over the error
                    return Check(name, "fail", _ESCAPE)
                return Check(name, "fail", str(e))
            if escape.exists():
                return Check(name, "fail", _ESCAPE)
            if not (inside / "inside.txt").exists():
                return Check(
                    name, "fail", "edit sessions cannot write (permission mode)"
                )
            # No file proves nothing unless claude tried the escape and was refused.
            if not _denied(result, escape):
                return Check(
                    name,
                    "warn",
                    "model did not attempt the escape; confinement unproven",
                )
    except OSError as e:
        return _temp_fail(name, e)
    return Check(name, "ok", f"writes stay inside the wiki (${result.cost_usd:.4f})")


def run_doctor(
    settings: LlmSettings, *, probe: bool, config_ok: bool = True
) -> list[Check]:
    checks = offline_checks()
    # No point spending money while the CLI is missing, too old or lacks a flag, or
    # while the config failed to load (the probes would run on default settings).
    claude_ok = any(
        c.name == "claude" and c.status == "ok" for c in checks
    ) and not any(
        c.name in ("claude-version", "claude-flags") and c.status == "fail"
        for c in checks
    )
    if probe and claude_ok and config_ok:
        roles: list[Role] = []
        seen: set[str] = set()
        for role in ("plan", "edit", "deep"):
            if settings.model(role) not in seen:
                seen.add(settings.model(role))
                roles.append(role)
        checks.extend(probe_model(r, settings) for r in roles)
        checks.append(probe_edit_confinement(settings))
        checks.append(probe_vision(settings))
    return checks
