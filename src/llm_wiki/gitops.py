"""Thin git wrappers. Every failure surfaces as GitError carrying git's stderr."""

import subprocess
from pathlib import Path

from llm_wiki.errors import GitError


def git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
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
    """Toplevel of the git work tree containing ``path`` (which may not exist yet), else None."""
    try:
        top = git(_existing_dir(path), "rev-parse", "--show-toplevel").strip()
    except GitError:
        return None
    return Path(top).resolve() if top else None


def inside_git_dir(path: Path) -> bool:
    """True when ``path`` (which may not exist yet) lies inside a repository's ``.git`` directory."""
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
