"""Thin git wrappers. Every failure surfaces as GitError carrying git's stderr."""

import subprocess
from pathlib import Path

from llm_wiki.errors import GitError


def git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            check=False,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH") from None
    if proc.returncode != 0:
        msg = (
            proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
        )
        raise GitError(f"git {' '.join(args)}: {msg}")
    return proc.stdout


def init_repo(root: Path) -> None:
    git(root, "init", "-b", "main")


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD").strip()


def _existing_dir(path: Path) -> Path:
    cwd = Path(path).resolve()
    while not cwd.is_dir():  # path may not exist yet
        cwd = cwd.parent
    return cwd


def enclosing_repo(path: Path) -> Path | None:
    """Toplevel of the git work tree containing ``path`` (which may not exist yet), else
    None.
    """
    try:
        top = git(_existing_dir(path), "rev-parse", "--show-toplevel").strip()
    except GitError:
        return None
    return Path(top).resolve() if top else None


def inside_git_dir(path: Path) -> bool:
    """True when ``path`` (which may not exist yet) lies inside a repository's ``.git``
    directory.
    """
    try:
        return (
            git(_existing_dir(path), "rev-parse", "--is-inside-git-dir").strip()
            == "true"
        )
    except GitError:
        return False


def has_identity(root: Path) -> bool:
    """True when git can build author and committer identities (config or GIT_* env)."""
    cwd = _existing_dir(root)
    try:
        git(cwd, "var", "GIT_AUTHOR_IDENT")
        git(cwd, "var", "GIT_COMMITTER_IDENT")
    except GitError:
        return False
    return True


# --- ingest helpers: every path is relative to the wiki root, scoped to the wiki ---


def _scope(root: Path) -> tuple[Path, str]:
    """(the repo toplevel, the wiki's path in it: "." when the wiki is the repo)."""
    top = enclosing_repo(root)
    if top is None:
        raise GitError(f"{root} is not inside a git repository")
    return top, Path(root).resolve().relative_to(top).as_posix()


def _repo_paths(prefix: str, rels: list[str]) -> list[str]:
    return [r if prefix == "." else f"{prefix}/{r}" for r in rels]


def wiki_rel(root: Path) -> str:
    return _scope(root)[1]


def current_branch(root: Path) -> str | None:
    """The checked-out branch, or None on a detached HEAD."""
    try:
        return git(root, "symbolic-ref", "--short", "-q", "HEAD").strip() or None
    except GitError:
        return None


def changed_paths(root: Path) -> list[str]:
    """Modified, added, deleted and untracked files of the wiki (gitignored ones
    excluded), relative to the wiki root.
    """
    top, prefix = _scope(root)
    out = git(top, "status", "--porcelain=v1", "-z", "-uall", "--", prefix)
    fields = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if not entry:
            continue
        if entry[0] in "RC":  # a rename or copy: the original path follows
            i += 1
        path = entry[3:]
        paths.append(path if prefix == "." else path.removeprefix(prefix + "/"))
    return sorted(paths)


def restore(root: Path, rels: list[str]) -> None:
    """A file tracked at HEAD goes back to it; any other file is deleted."""
    if not rels:
        return
    top, prefix = _scope(root)
    paths = dict(zip(_repo_paths(prefix, list(rels)), rels, strict=True))
    listed = git(top, "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", *paths)
    tracked = [p for p in listed.split("\0") if p in paths]
    if tracked:
        git(top, "checkout", "HEAD", "--", *tracked)
    untracked = [p for p in paths if p not in tracked]
    if untracked:  # drop it from the index too, in case it was staged
        git(top, "rm", "--cached", "-q", "--ignore-unmatch", "--", *untracked)
        for p in untracked:
            (top / p).unlink(missing_ok=True)


def stage_commit(
    root: Path, rels: list[str], message: str, *, amend: bool = False
) -> str:
    """Stage exactly ``rels`` (a missing file stages its deletion) and commit.

    Nothing staged and no ``amend``: no commit. Returns the HEAD sha.
    """
    top, prefix = _scope(root)
    root = Path(root)
    rels = list(dict.fromkeys(rels))
    present = [r for r in rels if (root / r).exists()]
    gone = [r for r in rels if r not in present]
    if present:
        git(top, "add", "--", *_repo_paths(prefix, present))
    if gone:
        git(
            top,
            "rm",
            "--cached",
            "-q",
            "--ignore-unmatch",
            "--",
            *_repo_paths(prefix, gone),
        )
    staged = git(top, "diff", "--cached", "--name-only", "-z", "--", prefix)
    if staged or amend:
        git(top, "commit", "-q", "-m", message, *(["--amend"] if amend else []))
    return git(top, "rev-parse", "HEAD").strip()


def switch(root: Path, branch: str, create: bool = False) -> None:
    git(root, "switch", "-q", *(["-c"] if create else []), branch)


def delete_branch(root: Path, branch: str) -> None:
    git(root, "branch", "-q", "-D", branch)


def ff_merge(root: Path, branch: str) -> None:
    git(root, "merge", "-q", "--ff-only", branch)
