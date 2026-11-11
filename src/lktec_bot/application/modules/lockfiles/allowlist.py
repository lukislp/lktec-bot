# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The one place that decides whether the module may commit a path. Pure: no I/O.

Two kinds of file pass, and nothing else:

- a **lock file**: its file name is one the module knows (``ecosystems.py``);
- a **derived file**: its path is listed, exactly, under ``derived[].paths`` of the policy, and
  that policy is the one of the default branch.

Both the job that generates the files and the job that commits them ask :func:`admit`. The
generating job runs code the bot does not control, so its answer is a convenience; the
committing job's answer is the one that counts, and it refuses the whole handover when a
single path is not admitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from lktec_bot.application.modules.lockfiles.ecosystems import ecosystem_of, within
from lktec_bot.application.modules.lockfiles.options import LockfilesOptions
from lktec_bot.domain.paths import generated_path_problem, repo_path_problem

# A notices file is text of a few hundred kilobytes at most; a lock file may be far larger and
# keeps the handover's own cap.
MAX_DERIVED_FILE_BYTES = 2 * 1024 * 1024
# Files in one handover, lock files and derived files together. Far above what a repository
# hands over (one lock file per project, a few derived files); a manifest beyond it is not a
# refresh any more.
MAX_FILES = 1000


class Kind(StrEnum):
    """What an admitted path is."""

    LOCKFILE = "lock file"
    DERIVED = "derived file"


@dataclass(frozen=True, slots=True)
class Admission:
    """The verdict on one path.

    ``kind`` is ``None`` when the path is refused and ``problem`` says why. ``left_out`` marks
    a lock file that is allowed in principle but excluded by the ``ecosystems`` or ``paths``
    option: it is ignored, not refused.
    """

    path: str
    kind: Kind | None
    problem: str = ""
    left_out: bool = False

    @property
    def refused(self) -> bool:
        return self.kind is None


def admit(path: str, size: int, options: LockfilesOptions) -> Admission:
    """Decide whether ``path``, a file of ``size`` bytes, may be committed under ``options``."""
    problem = repo_path_problem(path)
    if problem is not None:
        return Admission(path, None, problem)
    ecosystem = ecosystem_of(path)
    if ecosystem is not None:
        wrong_ecosystem = bool(options.ecosystems) and ecosystem not in options.ecosystems
        outside = bool(options.paths) and not any(within(path, scope) for scope in options.paths)
        return Admission(path, Kind.LOCKFILE, left_out=wrong_ecosystem or outside)
    if path not in options.derived_paths():
        return Admission(
            path,
            None,
            "neither a known lock file nor a path listed under derived[].paths in the policy "
            "of the default branch",
        )
    # The options model already refused such a declaration; checked again where it matters.
    problem = generated_path_problem(path)
    if problem is not None:
        return Admission(path, None, problem)
    if size > MAX_DERIVED_FILE_BYTES:
        return Admission(
            path,
            None,
            f"the derived file is larger than {MAX_DERIVED_FILE_BYTES // (1024 * 1024)} MiB",
        )
    return Admission(path, Kind.DERIVED)
