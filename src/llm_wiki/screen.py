"""Security gate between raw sources and anything an LLM or a wiki page sees.

Secrets are redacted first, then injection detection runs on the redacted text so
excerpts never contain a secret.
"""

import base64
import binascii
import bisect
import contextlib
import itertools
import json
import re
import shutil
import subprocess
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

MARK = "‹REDACTED:{kind}›"
_MARK_PREFIX = "‹REDACTED:"

_PLACEHOLDERS = tuple(
    re.compile(rf"^(?:{p})$", re.IGNORECASE)
    for p in (
        r"<.*>",  # <your-token>
        r"\$[({].*",  # $(cmd), ${VAR}
        r"\{\{.*\}\}",  # {{ template }}
        r"\*+|x{3,}|\.{3,}",  # masked
        r"changeme|null|none|n/?a",
        r"your[_-]?\w*|example\w*",
    )
)
# `$(cmd)` / `${VAR}` (shell substitution) fetch a secret at run time; they are not one
_SHELL_SUBST = ("$(", "${")
_ARN = re.compile(r"arn:aws[\w-]*:secretsmanager:[^\s]*$")


def _looks_secret(value: str) -> bool:
    """Real secrets have digits, symbols or mixed case; plain words do not.

    Plain words such as "required" or "mandatory" are not secrets.
    """
    v = value.strip("\"'`,;")
    if len(v) < 6 or any(p.match(v) for p in _PLACEHOLDERS) or _MARK_PREFIX in v:
        return False
    return not (v.isalpha() and (v.islower() or v.istitle()) and len(v) < 16)


# (kind, pattern, group holding the secret value or 0 for the whole match)
_RULES: list[tuple[str, re.Pattern[str], int]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
            r"(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        0,
    ),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
        ),
        0,
    ),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    ("jfrog_api_key", re.compile(r"\bAKC[a-zA-Z0-9]{20,}\b"), 0),
    (
        "github_token",
        # ASCII on purpose: on str patterns \w also matches non-ASCII letters
        re.compile(  # NOSONAR
            r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"
            r"|\bgithub_pat_[A-Za-z0-9_]{40,}\b"
        ),
        0,
    ),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"), 0),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 0),
    ("bearer", re.compile(r"(?i)\b(bearer\s+)([a-z0-9\-._~+/]{20,}=*)"), 2),
    ("basic_auth", re.compile(r"(?i)\b(basic\s+)([a-z0-9+/]{16,}={0,2})"), 2),
    (
        "url_credentials",
        re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]{3,})(@)"),
        2,
    ),
    (
        "aws_secret",
        re.compile(
            r"(?i)(aws_secret_access_key\s*+[\"']?\s*+[:=]\s*+[\"']?)([^\s\"']{20,})"
        ),
        2,
    ),
    (
        "token",
        re.compile(
            r"(?i)((?<![A-Za-z0-9])(?:api[_-]?key|x-api-key|apikey|client[_-]?secret|secret[_-]?key|access[_-]?token|refresh[_-]?token|"
            r"auth[_-]?token|token|secret)\b\s*[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9._\-/+=]{16,})"
        ),
        2,
    ),
    (
        "password",
        re.compile(
            r"(?i)((?<![a-z0-9])(?:password|passwd|pwd|pass)\b\s*+[\"']?\s*+[:=]\s*+[\"']?)"
            r"([^\s\"'<>,;|]{4,})"
        ),
        2,
    ),
    ("conn_string_password", re.compile(r"(?i)(password|pwd)(=)([^;\s]{4,})"), 3),
]


def _is_basic_credential(value: str) -> bool:
    """A Basic auth value is base64 of `user:password`; prose glued after the word
    "Basic" (PDF text that lost its spaces) does not decode to one."""
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return ":" in decoded and decoded.isprintable()


def _is_false_positive(kind: str, m: re.Match[str], value: str) -> bool:
    if _MARK_PREFIX in value:
        return True  # already redacted
    if kind == "basic_auth":
        return not _is_basic_credential(value)
    if kind in ("password", "token") and not _looks_secret(value):
        return True
    if kind == "conn_string_password" and value.startswith(_SHELL_SUBST):
        return True
    # `arn:aws:secretsmanager:...:secret:<name>` names a secret, it is not one
    return kind == "token" and bool(
        _ARN.search(m.string, max(0, m.start() - 80), m.start() + 1)
    )


def redact(text: str) -> tuple[str, Counter[str]]:
    counts: Counter[str] = Counter()
    for kind, rx, group in _RULES:

        def sub(m: re.Match[str], kind: str = kind, group: int = group) -> str:
            mark = MARK.format(kind=kind)
            if group == 0:
                counts[kind] += 1
                return mark
            if _is_false_positive(kind, m, m.group(group)):
                return m.group(0)
            counts[kind] += 1
            start, end = m.start(group) - m.start(0), m.end(group) - m.start(0)
            whole = m.group(0)
            return whole[:start] + mark + whole[end:]

        text = rx.sub(sub, text)
    return text, counts


@dataclass
class Hit:
    kind: str
    line: int
    excerpt: str  # <= 80 chars


INJECTION_RULES: dict[str, list[str]] = {
    "addressed_to_ai": [
        (
            r"(?i)\b(?:hey|dear|attention|note to|instructions? for)\s+(?:the\s+)?"
            r"(?:ai|assistant|llm|language model|agent|claude|chatgpt|copilot)\b"
        ),
        r"(?i)\b(?:ai|assistant|agent|llm|claude)s?\s*[:,]\s*(?:please\s+)?(?:ignore|run|execute|delete|write|send|disregard|forget)\b",
    ],
    "override": [
        r"(?i)\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions|prompts|rules|messages)\b",
        r"(?i)\bnew\s+system\s+prompt\b",
        r"(?i)\byou\s+are\s+now\s+(?:a|an|in)\b",
    ],
    "agent_config": [
        r"(?i)\b(?:write|modify|edit|append|overwrite|update)\b[^\n]{0,60}(?:\.claude/|CLAUDE\.md|AGENTS\.md|mcp\.json|settings\.json|SKILL\.md)",
    ],
    "base64_blob": [r"[A-Za-z0-9+/]{400,}={0,2}"],
}
_COMPILED = {k: [re.compile(p) for p in ps] for k, ps in INJECTION_RULES.items()}
_B64 = re.compile(INJECTION_RULES["base64_blob"][0])
_HIDDEN = re.compile(
    "[\u200b-\u200f\u061c\u2060\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f\ufeff]"
)
# zero-width spaces and joiners: editors and wiki exports leave single ones inside
# words, so only a run of them (a hidden payload) is a finding
_ZERO_WIDTH = re.compile("[\u200b-\u200d\u2060\ufeff]")
_ZERO_WIDTH_RUN = re.compile("[\u200b-\u200d\u2060\ufeff]{4,}")
# direction marks and overrides, and tag characters: a finding even alone
_ALWAYS_HIDDEN = re.compile(
    "[\u200e\u200f\u061c\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]"
)
_EXCERPT_MAX = 80


def _clip(s: str) -> str:
    s = _HIDDEN.sub(lambda m: f" U+{ord(m.group(0)):04X} ", s)
    s = " ".join(s.split())
    return s if len(s) <= _EXCERPT_MAX else s[: _EXCERPT_MAX - 1] + "…"


def _hidden_hits(text: str, line_of: Callable[[int], int]) -> list[Hit]:
    """One hit per line holding direction or tag characters, or a run of zero-width
    ones, listing their code points."""
    hidden: dict[int, list[str]] = {}
    found = itertools.chain(
        _ALWAYS_HIDDEN.finditer(text), _ZERO_WIDTH_RUN.finditer(text)
    )
    for m in sorted(found, key=lambda m: m.start()):
        codes = hidden.setdefault(line_of(m.start()), [])
        for ch in m.group(0):
            code = f"U+{ord(ch):04X}"
            if code not in codes:
                codes.append(code)
    return [Hit("hidden_chars", n, _clip(" ".join(c))) for n, c in hidden.items()]


def _line_finder(text: str) -> Callable[[int], int]:
    lines = text.split("\n")
    starts = list(itertools.accumulate((len(ln) + 1 for ln in lines[:-1]), initial=0))
    return lambda offset: bisect.bisect_right(starts, offset)


def find_injection(text: str) -> list[Hit]:
    hidden = _hidden_hits(text, _line_finder(text))
    lines = text.split("\n")
    # a zero-width character must not split a phrase out of the rules' reach;
    # removing them keeps every newline, so line numbers do not move
    text = _ZERO_WIDTH.sub("", text)
    hits: list[Hit] = []
    line_of = _line_finder(text)

    # every excerpt is built from lines with all base64 blobs masked
    masked = [
        _B64.sub(lambda m: f"<base64 {len(m.group(0))} chars>", ln) for ln in lines
    ]
    for kind, rxs in _COMPILED.items():
        for rx in rxs:
            for m in rx.finditer(text):
                n = line_of(m.start())
                hits.append(Hit(kind, n, _clip(masked[n - 1])))
    hits += hidden
    hits.sort(key=lambda h: (h.line, h.kind))
    return hits


@dataclass
class ScreenResult:
    text: str
    redactions: Counter[str] = field(default_factory=Counter)
    injection: list[Hit] = field(default_factory=list)
    gitleaks: list[str] = field(default_factory=list)


def _run_gitleaks(text: str, binary: str, tmp_dir: Path) -> list[str]:
    src = tmp_dir / f"gitleaks-{uuid.uuid4().hex}.txt"
    report = tmp_dir / f"gitleaks-{uuid.uuid4().hex}.json"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        src.write_text(text, encoding="utf-8")
        proc = subprocess.run(
            [
                binary,
                "detect",
                "--no-git",
                "--source",
                str(src),
                "--report-format",
                "json",
                "--report-path",
                str(report),
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if proc.returncode not in (0, 1):  # 1 = leaks found
            return ["gitleaks-error"]
        findings = json.loads(report.read_text(encoding="utf-8"))
        if not isinstance(findings, list):
            return ["gitleaks-error"]
    except (OSError, subprocess.SubprocessError, ValueError):
        return ["gitleaks-error"]
    finally:
        for f in (src, report):
            with contextlib.suppress(OSError):
                f.unlink(missing_ok=True)
    return sorted(
        {str(f["RuleID"]) for f in findings if isinstance(f, dict) and "RuleID" in f}
    )


def screen_text(
    text: str, gitleaks_path: Path | None = None, tmp_dir: Path | None = None
) -> ScreenResult:
    """Gitleaks runs only when a binary exists AND tmp_dir is given.

    tmp_dir must lie inside the wiki root.
    """
    binary = str(gitleaks_path) if gitleaks_path else shutil.which("gitleaks")
    redacted, counts = redact(text)
    # gitleaks sees the redacted text: what it still finds survived the regex set
    leaks = _run_gitleaks(redacted, binary, tmp_dir) if binary and tmp_dir else []
    return ScreenResult(redacted, counts, find_injection(redacted), leaks)


def quarantine(root: Path, raw_path: Path, reason: dict) -> Path:
    qdir = root / ".wiki" / "quarantine"
    qdir.mkdir(parents=True, exist_ok=True)
    dest = qdir / raw_path.name
    n = 2
    while dest.exists() or (qdir / f"{dest.name}.reason.json").exists():
        dest = qdir / f"{raw_path.stem}-{n}{raw_path.suffix}"
        n += 1
    payload = json.dumps(
        reason, indent=2, ensure_ascii=False
    )  # fail before moving anything
    reason_path = qdir / f"{dest.name}.reason.json"
    reason_path.write_text(payload, encoding="utf-8")
    try:
        shutil.move(str(raw_path), dest)
    except OSError:
        reason_path.unlink(missing_ok=True)
        raise
    return dest
