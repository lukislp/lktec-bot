# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The source tree of a pull request checkout, read and prepared with git, and the handover
directory on disk. Used by ``lktec-bot lockfiles derive`` in the job that holds no secret."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from lktec_bot.ports.workspace import FileStat, PreparedTree, WorkspaceError

MANIFEST = "manifest.txt"
FILES_DIR = "files"
GIT_TIMEOUT_SECONDS = 120

# A ref is handed to git as an argument: no leading dash, no revision syntax.
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*\Z")


class GitWorkspace:
    """Implements :class:`~lktec_bot.ports.workspace.SourceTree` on a git checkout.

    ``repo`` is the checkout of the pull request head (with the refreshed lock files in its
    working tree), ``trusted_ref`` the default branch as the checkout knows it
    (``origin/main``), ``tree_dir`` a path that does not exist yet, where the prepared tree is
    created as a detached worktree.
    """

    def __init__(self, repo: Path, trusted_ref: str, tree_dir: Path) -> None:
        if not _REF.match(trusted_ref) or ".." in trusted_ref:
            raise WorkspaceError(f"{trusted_ref!r} is not a plain branch reference")
        self._repo = repo
        self._ref = trusted_ref
        self._tree = tree_dir

    def trusted_file(self, path: str) -> str | None:
        self._git(self._repo, "rev-parse", "--verify", "--quiet", f"{self._ref}^{{commit}}")
        shown = self._run(self._repo, "show", f"{self._ref}:{path}")
        if shown.returncode != 0:
            return None
        try:
            return shown.stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError(f"{path} on {self._ref} is not UTF-8 text") from None

    def prepare(self, is_dependency_file: Callable[[str], bool]) -> PreparedTree:
        base = self._git(self._repo, "merge-base", self._ref, "HEAD").strip()
        if self._tree.exists():
            raise WorkspaceError(f"{self._tree} already exists")
        self._git(self._repo, "worktree", "add", "--detach", "--quiet", str(self._tree), base)
        # Everything that differs between the base and the working tree: the pull request's own
        # commits and the lock files refreshed in this job. Tracked files only.
        changed = _split(self._git(self._repo, "diff", "--name-only", "-z", "--no-renames", base))
        overlaid: list[str] = []
        ignored: list[str] = []
        for path in sorted(changed):
            source = self._repo / path
            if not is_dependency_file(path) or source.is_symlink():
                ignored.append(path)
                continue
            target = self._tree / path
            if target.is_symlink() or target.is_file():
                target.unlink()
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            overlaid.append(path)
        return PreparedTree(self._tree, base, tuple(overlaid), tuple(ignored))

    def changes(self) -> Mapping[str, str]:
        status = self._git(
            self._tree, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"
        )
        # Each entry is ``XY <path>``; with renames off there is no second path.
        return {entry[3:]: self._fingerprint(entry[3:]) for entry in _split(status)}

    def stat(self, path: str) -> FileStat | None:
        file = self._tree / path
        if file.is_symlink():
            return FileStat(regular=False)
        if not file.exists():
            return None
        return (
            FileStat(regular=True, size=file.stat().st_size) if file.is_file() else FileStat(False)
        )

    def read(self, path: str) -> bytes:
        return (self._tree / path).read_bytes()

    def head(self, path: str) -> bytes | None:
        file = self._repo / path
        if file.is_symlink() or not file.is_file():
            return None
        return file.read_bytes()

    def _fingerprint(self, path: str) -> str:
        file = self._tree / path
        if file.is_symlink():
            return f"link:{os.readlink(file)}"
        if file.is_file():
            return hashlib.sha256(file.read_bytes()).hexdigest()
        return "dir" if file.is_dir() else "absent"

    def _git(self, cwd: Path, *args: str) -> str:
        done = self._run(cwd, *args)
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", errors="replace").strip()[:300]
            raise WorkspaceError(f"git {args[0]} failed: {detail or done.returncode}")
        return done.stdout.decode("utf-8", errors="surrogateescape")

    def _run(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            # core.quotePath off: paths come back as they are, not octal-escaped.
            return subprocess.run(  # noqa: S603 - fixed program, arguments are not shell-parsed
                ["git", "-c", "core.quotePath=false", *args],  # noqa: S607 - git from PATH
                cwd=cwd,
                capture_output=True,
                check=False,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError(f"git {args[0]} could not run: {exc}") from None


def _split(text: str) -> list[str]:
    return [entry for entry in text.split("\0") if entry]


class DirectoryHandover:
    """Implements :class:`~lktec_bot.ports.workspace.HandoverSink`: ``manifest.txt`` plus
    ``files/<repository path>``, the layout the commit job reads. Lines already in the manifest
    (the lock files) are kept."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def add(self, files: Mapping[str, bytes]) -> None:
        manifest = self._directory / MANIFEST
        listed = manifest.read_text(encoding="utf-8").splitlines() if manifest.exists() else []
        for path, content in files.items():
            target = self._directory / FILES_DIR / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            if path not in listed:
                listed.append(path)
        # The manifest last and in one step: the files it names exist before it names them, and
        # a step that dies half-way leaves the previous manifest (the lock files) as it was.
        staged = self._directory / f"{MANIFEST}.tmp"
        staged.write_text("".join(f"{line}\n" for line in listed), encoding="utf-8", newline="\n")
        staged.replace(manifest)
