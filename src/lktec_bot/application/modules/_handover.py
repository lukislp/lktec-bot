"""The handover directory a module's first job passes to the job that holds the credentials.

Layout: ``manifest.txt`` (one repository path per line) and ``files/<repository path>``. The
first job may have run untrusted code, so everything here is checked: plain relative paths,
characters the bot accepts, no symlinks, no file outside ``files/``, a size cap. A module adds
its own rule per path (``path_problem``) on top of these.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from lktec_bot.domain.paths import repo_path_problem

MANIFEST = "manifest.txt"
FILES_DIR = "files"
MAX_FILE_BYTES = 10 * 1024 * 1024
# Paths end up in a commit message and in ``--add path=file``.
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._/@+ -]+$")


class HandoverError(ValueError):
    """What the first job handed over cannot be trusted or read."""


def read_manifest(
    changed_dir: Path, path_problem: Callable[[str], str | None] | None = None
) -> list[str]:
    """The repository paths the first job reports as changed.

    Every one must be a plain relative path (and pass ``path_problem``, when given); if any is
    not, the whole handover is refused (nothing is committed).
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
            problem = "the path has characters outside letters, digits and . _ / @ + - space"
        if problem is None and path_problem is not None:
            problem = path_problem(path)
        if problem is not None:
            problems.append(f"{path!r}: {problem}")
        paths.append(path)
    if problems:
        raise HandoverError("refusing the handover: " + "; ".join(problems))
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
