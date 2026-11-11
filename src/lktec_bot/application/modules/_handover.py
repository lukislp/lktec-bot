# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The handover directory a module's first job passes to the job that holds the credentials.

Layout: ``manifest.txt`` (one repository path per line) and ``files/<repository path>``. The
first job may have run untrusted code, so everything here is checked: plain relative paths,
characters the bot accepts, no symlinks, no file outside ``files/``, a size cap, and the
caller's cap on the number of files. Which of the paths a module may commit is the module's own
rule, applied on top of these (the lock file module's allow-list, the sync module's ``paths``
option).
"""

from __future__ import annotations

import re
from pathlib import Path

from lktec_bot.domain.paths import SAFE_PATH, SAFE_PATH_HINT, repo_path_problem

MANIFEST = "manifest.txt"
FILES_DIR = "files"
MAX_FILE_BYTES = 10 * 1024 * 1024
# Paths end up in a commit message and in ``--add path=file``.
_SAFE_PATH: re.Pattern[str] = SAFE_PATH


class HandoverError(ValueError):
    """What the first job handed over cannot be trusted or read."""


def read_manifest(changed_dir: Path, max_files: int | None = None) -> list[str]:
    """The repository paths the first job reports as changed.

    Every one must be a plain relative path, and there may be at most ``max_files`` of them
    (when given); otherwise the whole handover is refused (nothing is committed).
    """
    try:
        text = (changed_dir / MANIFEST).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HandoverError(f"cannot read {MANIFEST} in {changed_dir}: {exc}") from None
    paths: list[str] = []
    problems: list[str] = []
    for raw in text.splitlines():
        path = raw.strip()
        if not path or path in paths:
            continue
        problem = repo_path_problem(path)
        if problem is None and not _SAFE_PATH.match(path):
            problem = SAFE_PATH_HINT
        if problem is not None:
            problems.append(f"{path!r}: {problem}")
        paths.append(path)
    if problems:
        raise HandoverError("refusing the handover: " + "; ".join(problems))
    if max_files is not None and len(paths) > max_files:
        raise HandoverError(
            f"refusing the handover: {MANIFEST} lists {len(paths)} files, more than {max_files}"
        )
    return paths


def local_file(changed_dir: Path, path: str) -> Path:
    """The handed-over copy of ``path``: a regular file inside ``<changed_dir>/files``."""
    root = (changed_dir / FILES_DIR).resolve()
    candidate = changed_dir / FILES_DIR / path
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise HandoverError(
            f"{MANIFEST} lists {path!r} but {FILES_DIR}/{path} is missing"
        ) from None
    if candidate.is_symlink() or not resolved.is_relative_to(root) or not resolved.is_file():
        raise HandoverError(f"{FILES_DIR}/{path} is not a plain file inside {FILES_DIR}/")
    if resolved.stat().st_size > MAX_FILE_BYTES:
        raise HandoverError(
            f"{FILES_DIR}/{path} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB"
        )
    return resolved
