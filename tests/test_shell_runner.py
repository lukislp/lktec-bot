# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The shell adapter behind ``lockfiles derive``: exit codes, the environment, the timeout.

It starts real ``bash`` processes and kills process groups, so it runs where the workflows
run (Linux, macOS), not on Windows.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from lktec_bot.adapters import shell_runner
from lktec_bot.adapters.shell_runner import ShellRunner

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs bash and process groups")

ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def test_a_command_runs_in_the_directory_it_is_given(tmp_path: Path) -> None:
    result = ShellRunner().run("pwd > where.txt && echo done", tmp_path, 30, ENV)

    assert (result.exit_code, result.timed_out) == (0, False)
    assert result.output == "done\n"
    assert Path((tmp_path / "where.txt").read_text().strip()).resolve() == tmp_path.resolve()


def test_the_exit_code_and_both_streams_come_back(tmp_path: Path) -> None:
    result = ShellRunner().run("echo out; echo err >&2; exit 7", tmp_path, 30, ENV)

    assert result.exit_code == 7
    assert result.output == "out\nerr\n"


def test_a_failing_step_of_a_multi_line_command_stops_it(tmp_path: Path) -> None:
    result = ShellRunner().run("false\necho not reached\n", tmp_path, 30, ENV)

    assert result.exit_code == 1
    assert "not reached" not in result.output


def test_a_failing_pipe_fails_the_command(tmp_path: Path) -> None:
    assert ShellRunner().run("false | cat", tmp_path, 30, ENV).exit_code == 1


def test_an_unset_variable_fails_the_command(tmp_path: Path) -> None:
    assert ShellRunner().run('echo "$NOT_SET_ANYWHERE"', tmp_path, 30, ENV).exit_code != 0


def test_the_command_sees_exactly_the_environment_it_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LKTEC_BOT_PRIVATE_KEY", "secret")

    result = ShellRunner().run(
        'echo "key=${LKTEC_BOT_PRIVATE_KEY:-none} mine=$MINE"', tmp_path, 30, {**ENV, "MINE": "1"}
    )

    assert result.output == "key=none mine=1\n"


def test_the_command_has_no_standard_input(tmp_path: Path) -> None:
    result = ShellRunner().run("cat; echo eof", tmp_path, 30, ENV)

    assert (result.exit_code, result.output) == (0, "eof\n")


def test_only_the_end_of_a_long_output_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell_runner, "OUTPUT_TAIL_BYTES", 6)

    result = ShellRunner().run("echo 0123456789", tmp_path, 30, ENV)

    assert result.output == "56789\n"


def test_a_command_that_runs_too_long_is_killed_with_what_it_started(tmp_path: Path) -> None:
    started = time.monotonic()

    result = ShellRunner().run(
        "echo before; (sleep 30; touch late.txt) & sleep 30; touch late.txt", tmp_path, 1, ENV
    )

    assert time.monotonic() - started < 15
    assert (result.exit_code, result.timed_out) == (None, True)
    assert result.output == "before\n"
    time.sleep(0.2)
    assert not (tmp_path / "late.txt").exists()


def test_a_shell_that_cannot_start_is_a_failed_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell_runner, "SHELL", ("/nonexistent/shell", "-c"))

    result = ShellRunner().run("true", tmp_path, 30, ENV)

    assert result.exit_code == 127
    assert "the shell could not start" in result.output
