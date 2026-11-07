# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Repository paths the bot may write: pure rules, no I/O.

A path handed to a verb that changes files comes from a module or a command line, and for the
lock file module from an artifact produced by a job that ran a pull request's code. It must
therefore be a plain, normalised, relative path inside the repository, and never one the App
cannot write anyway: the App has no ``workflows`` permission, so GitHub rejects a change under
``.github/workflows/`` and the bot refuses it up front with a clear reason.
"""

from __future__ import annotations

import re

WORKFLOWS_DIR = ".github/workflows"

_DRIVE = re.compile(r"^[A-Za-z]:")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def within(path: str, restriction: str) -> bool:
    """Whether ``path`` is ``restriction`` or lies under it (a directory prefix)."""
    return path == restriction or path.startswith(f"{restriction}/")


def repo_path_problem(path: str) -> str | None:
    """Why ``path`` is not an acceptable repository path, or ``None`` when it is.

    Refused: an empty path, control characters, a backslash, an absolute path (leading ``/``
    or a drive letter), an empty, ``.`` or ``..`` segment (so also a trailing slash), a ``.git``
    segment, and everything at or under ``.github/workflows`` (compared case-insensitively).
    """
    if not path:
        return "the path is empty"
    if _CONTROL.search(path):
        return "the path contains a control character"
    if "\\" in path:
        return "the path contains a backslash; use '/' as the separator"
    if path.startswith("/") or _DRIVE.match(path):
        return "the path is absolute; it must be relative to the repository root"
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return "the path has an empty, '.' or '..' segment"
    if any(segment.lower() == ".git" for segment in segments):
        return "the path points into .git"
    lowered = path.lower()
    if lowered == WORKFLOWS_DIR or lowered.startswith(f"{WORKFLOWS_DIR}/"):
        return (
            f"the path is under {WORKFLOWS_DIR}/; the App has no 'workflows' permission, "
            "so GitHub would refuse the change"
        )
    return None
