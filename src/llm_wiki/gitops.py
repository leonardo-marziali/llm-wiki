"""Thin git wrappers. Every failure surfaces as GitError carrying git's stderr."""

import subprocess
from collections.abc import Collection
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


def has_commits(root: Path) -> bool:
    """True when HEAD points at a commit (False on an unborn branch)."""
    try:
        git(root, "rev-parse", "--verify", "-q", "HEAD")
    except GitError:
        return False
    return True


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


def _pgit(top: Path, *args: str) -> str:
    """git with literal pathspecs: a file named ``*.md`` or ``[A]GENTS.md`` (which
    an edit session can create) never matches other files.
    """
    return git(top, "--literal-pathspecs", *args)


def _scope(root: Path) -> tuple[Path, str]:
    """(the repo toplevel, the wiki's path in it: "." when the wiki is the repo)."""
    top = enclosing_repo(root)
    if top is None:
        raise GitError(f"{root} is not inside a git repository")
    return top, Path(root).resolve().relative_to(top).as_posix()


def _repo_paths(prefix: str, rels: list[str]) -> list[str]:
    return [r if prefix == "." else f"{prefix}/{r}" for r in rels]


def _tracked_at(top: Path, rev: str, paths: list[str]) -> list[str]:
    """The ``paths`` that exist in commit ``rev``."""
    if not paths:
        return []
    listed = _pgit(top, "ls-tree", "-r", "-z", "--name-only", rev, "--", *paths)
    found = set(listed.split("\0"))
    return [p for p in paths if p in found]


def wiki_rel(root: Path) -> str:
    return _scope(root)[1]


def current_branch(root: Path) -> str | None:
    """The checked-out branch, or None on a detached HEAD."""
    try:
        return git(root, "symbolic-ref", "--short", "-q", "HEAD").strip() or None
    except GitError:
        return None


def changed_paths(root: Path, ignored: bool = False) -> list[str]:
    """Modified, added, deleted and untracked files of the wiki, relative to the
    wiki root; gitignored files too when ``ignored`` is set.
    """
    top, prefix = _scope(root)
    flags = ["--ignored"] if ignored else []
    # no renames: a staged ``git mv`` lists both its paths, so a rollback
    # restores the source as well as dropping the destination
    out = _pgit(
        top,
        "status",
        "--porcelain=v1",
        "-z",
        "-uall",
        "--no-renames",
        *flags,
        "--",
        prefix,
    )
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


def restore(root: Path, rels: list[str], rev: str = "HEAD") -> None:
    """A file tracked at ``rev`` goes back to it (index too); any other is deleted."""
    if not rels:
        return
    top, prefix = _scope(root)
    paths = _repo_paths(prefix, list(dict.fromkeys(rels)))
    tracked = _tracked_at(top, rev, paths)
    if tracked:
        _pgit(top, "checkout", rev, "--", *tracked)
    untracked = [p for p in paths if p not in tracked]
    if untracked:  # drop it from the index too, in case it was staged
        _pgit(top, "rm", "--cached", "-q", "--ignore-unmatch", "--", *untracked)
        for p in untracked:
            (top / p).unlink(missing_ok=True)


def exists_at(root: Path, rev: str, rel: str) -> bool:
    top, prefix = _scope(root)
    return bool(_tracked_at(top, rev, _repo_paths(prefix, [rel])))


def show_text(root: Path, rev: str, rel: str) -> str | None:
    """The text of ``rel`` in commit ``rev``, or None when it has none."""
    top, prefix = _scope(root)
    try:
        return git(top, "show", f"{rev}:{_repo_paths(prefix, [rel])[0]}")
    except GitError:
        return None


def stage_commit(
    root: Path,
    rels: list[str],
    message: str,
    *,
    amend: str | None = None,
    allow_empty: bool = False,
    force: Collection[str] = (),
) -> str:
    """Commit exactly ``rels`` (a missing file commits its deletion); returns HEAD.

    Only these paths are committed (``--only``), whatever else the index holds.
    The ``force`` paths are tool-generated (pages, conversions, the manifest,
    the log and indexes) or accepted raw sources, named one by one: they are
    added with ``-f``, so a host repo's .gitignore cannot break an ingest (R23).
    Any other path is added without it. ``amend`` is the sha of the commit to
    amend: it must still be HEAD. Without ``amend`` or ``allow_empty``, nothing
    to commit makes no commit.
    """
    top, prefix = _scope(root)
    head = git(top, "rev-parse", "HEAD").strip()
    if amend is not None and head != amend:
        raise GitError(f"refusing to amend {head[:12]}: expected {amend[:12]}")
    root = Path(root)
    rels = list(dict.fromkeys(rels))
    here = [r for r in rels if (root / r).exists()]
    forced = _repo_paths(prefix, [r for r in here if r in force])
    plain = _repo_paths(prefix, [r for r in here if r not in force])
    present = forced + plain
    gone = _repo_paths(prefix, [r for r in rels if not (root / r).exists()])
    if forced:
        _pgit(top, "add", "-f", "--", *forced)
    if plain:
        _pgit(top, "add", "--", *plain)
    if gone:
        _pgit(top, "rm", "--cached", "-q", "--ignore-unmatch", "--", *gone)
    # a path neither added nor in HEAD is unknown to git: there is nothing to commit
    paths = present + _tracked_at(top, "HEAD", gone)
    staged = paths and _pgit(top, "diff", "--cached", "--name-only", "-z", "--", *paths)
    if not (staged or amend or allow_empty):
        return head
    extra = ["--allow-empty"] + (["--amend"] if amend else [])
    _pgit(top, "commit", "-q", "--only", *extra, "-m", message, "--", *paths)
    return git(top, "rev-parse", "HEAD").strip()


def move(root: Path, src: str, dest: str) -> None:
    """``git mv`` wiki path ``src`` to ``dest``, whose folder must exist."""
    top, prefix = _scope(root)
    _pgit(top, "mv", "--", *_repo_paths(prefix, [src, dest]))


def is_ignored(root: Path, rel: str) -> bool:
    """True when git ignores the (untracked) wiki path ``rel``."""
    top, prefix = _scope(root)
    # check-ignore takes paths, not pathspecs: a name is matched literally
    proc = subprocess.run(
        ["git", "check-ignore", "-q", "--", *_repo_paths(prefix, [rel])],
        cwd=top,
        capture_output=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise GitError(f"git check-ignore {rel}: {proc.stderr.decode().strip()}")
    return proc.returncode == 0


def switch(root: Path, branch: str, create: bool = False) -> None:
    git(root, "switch", "-q", *(["-c"] if create else []), branch)


def delete_branch(root: Path, branch: str) -> None:
    """Delete ``branch``; a branch that does not exist is no error."""
    if branch_exists(root, branch):
        git(root, "branch", "-q", "-D", branch)


def unstage(root: Path, rels: list[str]) -> None:
    """Put the index entries of ``rels`` back to HEAD; the files stay as they are."""
    if not rels:
        return
    top, prefix = _scope(root)
    _pgit(top, "reset", "-q", "--", *_repo_paths(prefix, list(dict.fromkeys(rels))))


def ff_merge(root: Path, branch: str) -> None:
    git(root, "merge", "-q", "--ff-only", branch)


def branch_exists(root: Path, branch: str) -> bool:
    try:
        git(root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
    except GitError:
        return False
    return True


def staged_paths(root: Path) -> list[str]:
    """Paths staged anywhere in the repository, relative to its toplevel."""
    top, _ = _scope(root)
    out = git(top, "diff", "--cached", "--name-only", "-z")
    return [p for p in out.split("\0") if p]


def changed_since(root: Path, rev: str) -> list[str]:
    """The wiki's files that differ between commit ``rev`` and HEAD."""
    top, prefix = _scope(root)
    out = _pgit(
        top, "diff", "--name-only", "-z", "--no-renames", rev, "HEAD", "--", prefix
    )
    paths = [p for p in out.split("\0") if p]
    return sorted(p if prefix == "." else p.removeprefix(prefix + "/") for p in paths)


def reset_soft(root: Path, rev: str) -> None:
    git(root, "reset", "-q", "--soft", rev)
