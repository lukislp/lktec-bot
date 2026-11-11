# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Runs a generator command from the policy in a shell, with a timeout.

Only ``lktec-bot lockfiles derive`` uses this, in the job that holds no secret: the command is
text from the default branch's policy file, and what it prints or writes is untrusted.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import IO

from lktec_bot.ports.workspace import CommandResult

SHELL = ("bash", "-euo", "pipefail", "-c")
# How much of the command's output is kept for the log.
OUTPUT_TAIL_BYTES = 4000


class ShellRunner:
    """Implements :class:`~lktec_bot.ports.workspace.CommandRunner` with ``bash``."""

    def run(
        self, command: str, cwd: Path, timeout_seconds: int, env: Mapping[str, str]
    ) -> CommandResult:
        with tempfile.TemporaryFile() as log:
            try:
                process = subprocess.Popen(  # noqa: S603 - the command is the policy's, see above
                    [*SHELL, command],
                    cwd=cwd,
                    env=dict(env),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    # A group of its own, so the timeout reaches what the command started.
                    start_new_session=True,
                )
            except OSError as exc:
                return CommandResult(127, output=f"the shell could not start: {exc}")
            timed_out = False
            try:
                exit_code: int | None = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = None
                _kill(process)
                process.wait()
            return CommandResult(exit_code, timed_out, _tail(log))


def _kill(process: subprocess.Popen[bytes]) -> None:
    if sys.platform != "win32":
        # It may have ended between the timeout and the kill.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    else:  # pragma: no cover - the workflows run on Linux
        process.kill()


def _tail(log: IO[bytes]) -> str:
    size = log.seek(0, os.SEEK_END)
    log.seek(max(0, size - OUTPUT_TAIL_BYTES))
    return log.read().decode("utf-8", errors="replace")
