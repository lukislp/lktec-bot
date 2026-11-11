# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Repository paths the bot may write: pure rules, no I/O.

A path handed to a verb that changes files comes from a module or a command line, and for the
lock file module from an artifact produced by a job that ran a pull request's code. It must
therefore be a plain, normalised, relative path inside the repository, and never one the App
cannot write anyway: the App has no ``workflows`` permission, so GitHub rejects a change under
``.github/workflows/`` and the bot refuses it up front with a clear reason.

A path a policy declares as the output of a generator (the ``derived`` option of the lock file
module) is held to a stricter rule on top, :func:`generated_path_problem`: one exact file, never
a pattern, and nothing under ``.github/`` at all, where the workflows, the policy file and
Dependabot's own configuration live.
"""

from __future__ import annotations

import re

WORKFLOWS_DIR = ".github/workflows"
GITHUB_DIR = ".github"
# The policy file, lower-cased; spelled out here because the domain's policy module must not be
# needed to judge a path (``tests/test_paths.py`` pins it to ``POLICY_PATH``).
_POLICY_FILE = ".github/lktec-bot.yml"

# Paths end up in a commit message and in ``--add path=file``.
SAFE_PATH = re.compile(r"^[A-Za-z0-9._/@+ -]+\Z")
SAFE_PATH_HINT = "the path has characters outside letters, digits and . _ / @ + - space"

_DRIVE = re.compile(r"^[A-Za-z]:")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PATTERN = re.compile(r"[*?\[\]{}]")


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


def generated_path_problem(path: str) -> str | None:
    """Why ``path`` may not be declared as a file a generator produces, or ``None``.

    On top of :func:`repo_path_problem`: it must name one file exactly (no ``*``, ``?``, ``[]``
    or ``{}``), use only the characters the handover accepts, and lie outside ``.github/``
    (compared case-insensitively), which holds the workflows, the policy file and Dependabot's
    configuration. The policy file is refused by name as well, so the rule survives a move of
    either.
    """
    if _PATTERN.search(path):
        return "the path is a pattern; name every generated file exactly"
    problem = repo_path_problem(path)
    if problem is not None:
        return problem
    if not SAFE_PATH.match(path):
        return SAFE_PATH_HINT
    lowered = path.lower()
    if lowered == _POLICY_FILE:
        return "the path is the bot's own policy file; a generator never writes it"
    if lowered == GITHUB_DIR or lowered.startswith(f"{GITHUB_DIR}/"):
        return (
            f"the path is under {GITHUB_DIR}/; generated files there (workflows, the policy "
            "file, Dependabot's configuration) are never committed"
        )
    return None
