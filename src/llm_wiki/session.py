"""A confined edit session: one ``edit`` call in the wiki root, then the
post-check (strays reverted, open-questions guarded, the changed pages linted)
and at most one repair.

Ingest, ``wiki lint --fix`` and ``wiki consolidate`` share it; each plugs its
own checks in through the ``pages_to_lint`` and ``extra`` hooks.
"""

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

from llm_wiki import claude
from llm_wiki.config import LlmSettings, WikiConfig
from llm_wiki.gitops import changed_paths, restore
from llm_wiki.lint import Finding, lint
from llm_wiki.pages import SYSTEM_FILES
from llm_wiki.plan import system_prompt

OPEN_QUESTIONS = "wiki/open-questions.md"
QUARANTINE = ".wiki/quarantine/"
_FINALIZE_CODES = {"not-in-index"}  # finalize rebuilds the indexes after all edits
_APPEND_ONLY = (
    "may only grow: append entries, never edit or remove existing ones; "
    "the earlier text was put back"
)
_RESOLVED_ONLY = (
    "may only grow, apart from removing the resolved questions the plan lists; "
    "the earlier text was put back"
)


class SessionFailed(Exception):
    """Lint errors are left after the repair; the message is the reason."""


def is_page(rel: str) -> bool:
    parts = rel.split("/")
    return (
        parts[0] == "wiki"
        and rel.endswith(".md")
        and parts[-1] not in SYSTEM_FILES
        and "_archive" not in parts[1:-1]
    )


def _content(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _under(rel: str, folder: str | None) -> bool:
    return folder is not None and (rel == folder or rel.startswith(folder + "/"))


class EditSession:
    """Edits confined to ``allowed``; everything else is put back."""

    prompt_file = "edit.md"  # the package prompt appended to AGENTS.md

    def __init__(
        self,
        root: Path,
        cfg: WikiConfig,
        settings: LlmSettings,
        today: date,
        run_dir: Path,
        name: str,
    ):
        self.root = Path(root)
        self.cfg, self.settings, self.today = cfg, settings, today
        self.run_dir = Path(run_dir)
        self.name = name  # prefixes the run-dir files
        self.allowed: set[str] = {OPEN_QUESTIONS}
        # paths the edit may never read (a trailing / denies a folder)
        self.deny: set[str] = {QUARANTINE}
        self.reverted: list[str] = []
        self.cost_usd = 0.0
        self.oq_removable: set[str] = set()  # lines an edit may remove (rule 6)
        self.oq_before: bytes | None = None  # open-questions.md before an edit
        run_dir_abs = self.run_dir.resolve()
        root_abs = self.root.resolve()
        # the run's own files are never strays, gitignored or not
        self.run_rel = (
            run_dir_abs.relative_to(root_abs).as_posix()
            if run_dir_abs.is_relative_to(root_abs)
            else None
        )
        # what was already uncommitted (e.g. inbox files of later sources), with
        # its bytes so a stray edit to it can be undone; None: it was missing.
        # Gitignored files count too: a stray may hide there (m6)
        self.baseline = {
            p: _content(self.root / p)
            for p in changed_paths(self.root, ignored=True)
            if not _under(p, self.run_rel)
        }

    def apply(
        self,
        prompt: Callable[[list[Finding] | None], str],
        suffix: str,
        *,
        pages_to_lint: Callable[[list[str]], list[str]] | None = None,
        extra: Callable[[], list[Finding]] | None = None,
    ) -> None:
        """The edit, the post-check, then one repair of any errors."""
        self.edit(prompt(None), suffix, "edit")
        errors_found = self.check(pages_to_lint, extra)
        if errors_found:
            self.edit(prompt(errors_found), suffix, "repair")
            errors_found = self.check(pages_to_lint, extra)
        if errors_found:
            raise SessionFailed(
                "lint errors after the repair: "
                + "; ".join(f"{f.path} {f.code}: {f.message}" for f in errors_found)
            )

    def edit(self, prompt: str, suffix: str, kind: str) -> None:
        self.oq_before = _content(self.root / OPEN_QUESTIONS)
        call = claude.make_call(
            "edit",
            self.settings,
            prompt=prompt,
            system_prompt=system_prompt(self.root, self.prompt_file),
            cwd=self.root,
            deny_read=claude.deny_rules(self.deny),
        )
        result = claude.run(call)
        self.cost_usd += result.cost_usd
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / f"{self.name}{suffix}.{kind}.json").write_text(
            json.dumps(result.raw, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def changed(self) -> list[str]:
        """Paths changed since the session began, beyond what was uncommitted then."""
        now = {
            p
            for p in changed_paths(self.root, ignored=True)
            if p not in self.baseline and not _under(p, self.run_rel)
        }
        now |= {
            p
            for p, data in self.baseline.items()
            if _content(self.root / p) != data  # changed, deleted or recreated
        }
        return sorted(now)

    def revert(self, paths: list[str]) -> None:
        """Committed paths go back to HEAD, the others to their bytes."""
        restore(self.root, [p for p in paths if p not in self.baseline])
        for p in paths:
            if p not in self.baseline:
                continue
            path, data = self.root / p, self.baseline[p]
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

    def undo(self) -> None:
        """Put back HEAD and the files uncommitted when the session began."""
        self.revert(self.changed())

    def check(
        self,
        pages_to_lint: Callable[[list[str]], list[str]] | None = None,
        extra: Callable[[], list[Finding]] | None = None,
    ) -> list[Finding]:
        """Revert stray changes, then the errors on the pages to lint.

        A changed .gitignore goes back first, so it cannot hide a stray.
        ``pages_to_lint`` maps the changed allowed pages to the pages linted
        (default: those); ``extra`` adds findings ahead of the lint's.
        """
        ignores = [p for p in self.changed() if Path(p).name == ".gitignore"]
        self.revert(ignores)
        changed = sorted({*self.changed(), *ignores})
        strays = [p for p in changed if p not in self.allowed]
        self.revert([p for p in strays if p not in ignores])
        self.reverted = sorted({*self.reverted, *strays})
        wanted = sorted(p for p in self.allowed if is_page(p))
        findings = [
            Finding("error", "missing-page", p, "planned page was not written")
            for p in wanted
            if not (self.root / p).is_file()
        ]
        findings += self._open_questions_check()
        pages = [p for p in changed if p in self.allowed and is_page(p)]
        if pages_to_lint is not None:
            pages = pages_to_lint(pages)
        if extra is not None:
            findings += extra()
        pages = [p for p in pages if (self.root / p).is_file()]
        if pages:
            report = lint(
                self.root,
                self.cfg,
                only=[Path(p) for p in pages],
                today=self.today,
            )
            findings += [
                f
                for f in report.findings
                if f.severity == "error"
                and f.path in pages
                and f.code not in _FINALIZE_CODES
            ]
        return findings

    def _open_questions_check(self) -> list[Finding]:
        """open-questions.md may only grow, but for the removable lines: anything
        else is put back (R19, rule 6).
        """
        before = self.oq_before
        path = self.root / OPEN_QUESTIONS
        now = _content(path)
        if before is None or (now is not None and now.startswith(before)):
            return []
        if now is not None and self.oq_removable and self._resolved_only(before, now):
            return []
        path.write_bytes(before)
        msg = _RESOLVED_ONLY if self.oq_removable else _APPEND_ONLY
        return [Finding("error", "open-questions-append-only", OPEN_QUESTIONS, msg)]

    def _resolved_only(self, before: bytes, now: bytes) -> bool:
        """``now`` is ``before`` with some removable lines deleted, plus an append."""
        removable = {line.rstrip("\r\n") for line in self.oq_removable}
        old = before.decode("utf-8", errors="surrogateescape")
        new = now.decode("utf-8", errors="surrogateescape")
        # every offset into ``new`` that the old lines kept so far can reach
        reach = {0}
        for line in old.splitlines(keepends=True):
            kept = {p + len(line) for p in reach if new.startswith(line, p)}
            if line.rstrip("\r\n") in removable:
                kept |= reach
            reach = kept
            if not reach:
                return False
        return True
