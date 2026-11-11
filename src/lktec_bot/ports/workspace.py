# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""What generating derived files needs from the machine it runs on: a source tree, a way to
run a command, and a place to put the result.

These ports are used in the job that holds no secret (``lktec-bot lockfiles derive``). Nothing
here talks to GitHub and nothing here ever sees the App key.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class WorkspaceError(Exception):
    """The source tree could not be read or prepared."""


@dataclass(frozen=True, slots=True)
class PreparedTree:
    """The tree the generators run in.

    ``root`` is a checkout of ``base``, a commit of the trusted branch's history, with the
    dependency files in ``overlaid`` replaced by the pull request's version. ``ignored`` lists
    the other files the pull request changes; they stay as they are at ``base``.
    """

    root: Path
    base: str
    overlaid: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FileStat:
    """A path in the prepared tree: whether it is a regular file (not a link, not a directory)
    and how large it is."""

    regular: bool
    size: int = 0


@dataclass(frozen=True, slots=True)
class CommandResult:
    """How a command ended. ``exit_code`` is ``None`` when it was killed after the timeout;
    ``output`` is the end of what it printed."""

    exit_code: int | None
    timed_out: bool = False
    output: str = ""


class SourceTree(Protocol):
    """The checkout of a pull request and the tree prepared from it."""

    def trusted_file(self, path: str) -> str | None:
        """The text of ``path`` on the trusted (default) branch, ``None`` when it is absent."""
        ...

    def prepare(self, is_dependency_file: Callable[[str], bool]) -> PreparedTree:
        """Build the tree the generators run in. Raises :class:`WorkspaceError`."""
        ...

    def changes(self) -> Mapping[str, str]:
        """A fingerprint of every path in the prepared tree that differs from its commit or is
        new, version-control ignores left out. Two calls bracket a command to see what it
        wrote."""
        ...

    def stat(self, path: str) -> FileStat | None:
        """What ``path`` is in the prepared tree, ``None`` when it does not exist."""
        ...

    def read(self, path: str) -> bytes:
        """The content of ``path`` in the prepared tree."""
        ...

    def head(self, path: str) -> bytes | None:
        """The content of ``path`` in the pull request's checkout, ``None`` when absent."""
        ...


class CommandRunner(Protocol):
    def run(
        self, command: str, cwd: Path, timeout_seconds: int, env: Mapping[str, str]
    ) -> CommandResult:
        """Run ``command`` in a shell in ``cwd`` with exactly ``env``; kill it after the
        timeout."""
        ...


class HandoverSink(Protocol):
    def add(self, files: Mapping[str, bytes]) -> None:
        """Add the files (repository path -> content) to the handover directory."""
        ...
