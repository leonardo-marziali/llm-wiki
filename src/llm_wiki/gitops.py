"""Thin git wrappers. Every failure surfaces as GitError carrying git's stderr."""

import subprocess
from pathlib import Path

from llm_wiki.errors import GitError


def git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
        )
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH") from None
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
        raise GitError(f"git {' '.join(args)}: {msg}")
    return proc.stdout


def init_repo(root: Path) -> None:
    git(root, "init", "-b", "main")


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD").strip()


def has_identity(root: Path) -> bool:
    """True when git can build an author identity (config or GIT_AUTHOR_* env)."""
    cwd = Path(root).resolve()
    while not cwd.is_dir():  # root may not exist yet
        cwd = cwd.parent
    try:
        git(cwd, "var", "GIT_AUTHOR_IDENT")
    except GitError:
        return False
    return True
