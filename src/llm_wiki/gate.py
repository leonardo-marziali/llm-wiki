"""What every wiki-changing run shares: the git preflight, the undo of a run,
fresh names, the log, and the accept/reject gate with its merge or rollback.
"""

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import typer

from llm_wiki import gitops
from llm_wiki.errors import WikiError
from llm_wiki.intake import QUARANTINE_DIR

Decision = Literal["accept", "reject"]
OP_PREFIXES = ("wiki/ingest-", "wiki/fix-", "wiki/consolidate-")
# what a run picks up or leaves for the user; anything else must be committed
DIRT_OK = ("inbox/", "raw/", ".wiki/reports/")
LOG = "wiki/log.md"
# terminal escapes: CSI, OSC (with its payload) and the two-character ones
_ESCAPE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]?"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def plain(text: str) -> str:
    """``text`` without terminal escapes; other control characters become spaces."""
    return _CONTROL.sub(" ", _ESCAPE.sub("", text))


def fresh(path: Path) -> Path:
    out, n = path, 2
    while out.exists():
        out = path.with_name(f"{path.name}-{n}")
        n += 1
    return out


def fresh_branch(root: Path, name: str) -> str:
    out, n = name, 2
    while gitops.branch_exists(root, out):
        out = f"{name}-{n}"
        n += 1
    return out


def _under(rel: str, folder: str) -> bool:
    return rel == folder or rel.startswith(folder + "/")


class Undo:
    """What a reject must put back: copies of moved and pre-run files, held under
    .wiki/tmp/ (listed in its held.json), and the quarantine entries the run did
    not make. Only paths the run owns are reset: any other file under inbox/ or
    raw/ is the user's and is never touched.
    """

    def __init__(self, root: Path, hold: Path, run_dir: Path):
        self.root, self.hold = root, hold
        self.skip = [p.relative_to(root).as_posix() for p in (hold, run_dir)]
        self.entries: list[tuple[Path | None, str]] = []  # (copy or None: absent, rel)
        self.owned: set[str] = set()  # inbox/ and raw/ paths this run changes
        qdir = root / QUARANTINE_DIR
        self.quarantined = {p.name for p in qdir.iterdir()} if qdir.is_dir() else set()

    @property
    def index(self) -> Path:
        return self.hold / "held.json"

    def own(self, rel: str) -> None:
        self.owned.add(rel)

    def keep(self, rel: str, dest: str | None = None) -> None:
        """Hold ``rel`` now; a rollback writes it to ``dest`` (default: ``rel``)."""
        src = self.root / rel
        copy = None
        self.hold.mkdir(parents=True, exist_ok=True)
        if src.is_file():
            copy = self.hold / str(len(self.entries))
            shutil.copy2(src, copy)
        self.entries.append((copy, dest or rel))
        self.owned |= {rel, dest or rel}
        held = [
            {"copy": c.name if c else None, "dest": d} for c, d in self.entries
        ]  # for a rollback finished by hand
        self.index.write_text(json.dumps(held, indent=2) + "\n", encoding="utf-8")

    def discard(self) -> None:
        shutil.rmtree(self.hold, ignore_errors=True)

    def _ours(self, rel: str) -> bool:
        if any(_under(rel, s) for s in self.skip):
            return False
        return rel in self.owned or not rel.startswith(DIRT_OK)

    def rollback(self, base: str, branch: str | None) -> None:
        """Every step runs even when an earlier one fails; the first error then
        propagates, and the held copies' index stays for a rollback by hand.
        """
        root = self.root
        try:
            dirty = [p for p in gitops.changed_paths(root) if self._ours(p)]
            # a gitignored file is reset only when the run owns it
            ignored = gitops.changed_paths(root, ignored=True)
            dirty += [p for p in ignored if p in self.owned]
            gitops.restore(root, dirty)  # nothing the run left half-done survives
        finally:
            try:
                if branch is not None:
                    if gitops.current_branch(root) != base:
                        gitops.switch(root, base)
                    gitops.delete_branch(root, branch)
            finally:
                self._put_back()
        self.discard()

    def _put_back(self) -> None:
        """Drop the run's quarantine entries; move the held copies back."""
        root = self.root
        qdir = root / QUARANTINE_DIR
        if qdir.is_dir():
            for p in qdir.iterdir():
                if p.name not in self.quarantined and p.is_file():
                    p.unlink()  # its source goes back to where it came from
        for copy, rel in self.entries:
            dest = root / rel
            if copy is None:
                dest.unlink(missing_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(copy), dest)


# --- preflight ---


def preflight_git(root: Path, op: str = "ingest") -> str:
    """The base branch, after the branch and clean-tree rules; ``op`` names the
    command in the messages.
    """
    top = gitops.enclosing_repo(root)
    if top is None:
        raise WikiError(f"{root} is not inside a git repository")
    own = top == Path(root).resolve()
    branch = gitops.current_branch(root)
    if branch is None:
        raise WikiError("HEAD is detached; switch to a branch first")
    if branch.startswith(OP_PREFIXES):
        left_by = branch.removeprefix("wiki/").split("-", 1)[0]
        base = "main" if own else "<base branch>"
        raise WikiError(
            f"a previous {left_by} left {branch} checked out; run "
            f"`git switch {base} && git branch -D {branch}` first"
        )
    if own and branch != "main":
        raise WikiError(f"{op} runs on main; the wiki is on {branch}")
    try:
        gitops.git(root, "rev-parse", "--verify", "-q", "HEAD")
    except WikiError:
        raise WikiError("the repository has no commits yet") from None
    staged = gitops.staged_paths(root)
    if staged:
        raise WikiError(
            "the repository has staged changes ("
            + ", ".join(staged[:5])
            + "); commit or unstage them first"
        )
    dirty = [p for p in gitops.changed_paths(root) if not p.startswith(DIRT_OK)]
    if dirty:
        raise WikiError(
            "the wiki has uncommitted changes outside inbox/ and raw/ ("
            + ", ".join(dirty[:5])
            + "); commit or discard them first"
        )
    return branch


# --- the log ---


def append_log(root: Path, lines: list[str]) -> None:
    if not lines:
        return
    path = root / LOG
    text = path.read_text(encoding="utf-8") if path.is_file() else "# Log\n"
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "\n".join(lines) + "\n", encoding="utf-8")


# --- the gate ---


def show_diff(root: Path, base: str, branch: str) -> None:
    # the pager is inherited: no capture
    subprocess.run(
        ["git", "diff", f"{base}...{branch}", "--", "."], cwd=root, check=False
    )


def _prompt_decide(root: Path, base: str, branch: str) -> Callable[[str], Decision]:
    def ask(summary: str) -> Decision:
        while True:
            answer = typer.prompt("accept, reject or diff? [a/r/d]").strip().lower()
            if answer in ("a", "accept"):
                return "accept"
            if answer in ("r", "reject"):
                return "reject"
            if answer in ("d", "diff"):
                show_diff(root, base, branch)

    return ask


def decide(
    root: Path,
    base: str,
    branch: str,
    summary: str,
    lint_errors: int,
    yes: bool,
    decide: Callable[[str], Decision] | None,
    confirm: Callable[[str], bool] | None,
) -> Decision:
    """Accept or reject the finished branch: --yes takes only a clean run."""
    if yes:
        return "reject" if lint_errors else "accept"
    gate = decide or _prompt_decide(root, base, branch)
    # anything but an explicit accept rejects
    accepted = gate(summary) == "accept"
    if accepted and lint_errors:
        n = lint_errors
        ask = confirm or (lambda q: typer.confirm(q, default=False))
        accepted = ask(f"lint has {n} error{'' if n == 1 else 's'}; accept anyway?")
    return "accept" if accepted else "reject"


def accept(root: Path, base: str, branch: str, undo: Undo) -> None:
    """Merge the branch; if that is cut short, say how to finish it by hand."""
    try:
        gitops.switch(root, base)
        gitops.ff_merge(root, branch)
        gitops.delete_branch(root, branch)
    except BaseException:
        typer.echo(
            f"error: the accept stopped before it finished. To finish it, run in "
            f"{root}:\n"
            f"  git switch {base} && git merge --ff-only {branch} && "
            f"git branch -D {branch}\n"
            f"then delete {undo.hold} (copies held only for a reject)",
            err=True,
        )
        raise
    undo.discard()


def rollback(undo: Undo, base: str, branch: str | None) -> None:
    """Reject; if that is cut short, say how to finish it by hand."""
    try:
        undo.rollback(base, branch)
    except BaseException:
        drop = f" && git branch -D {branch}" if branch else ""
        typer.echo(
            f"error: the rollback stopped before it finished. To finish it, run in "
            f"{undo.root}:\n"
            f"  git switch {base}{drop}\n"
            f"then move each file listed in {undo.index} from that folder back "
            "to its dest (a null copy: delete the dest)",
            err=True,
        )
        raise
