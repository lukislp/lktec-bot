# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""GitHub Actions output: the job summary and secret masking.

Outside Actions the summary goes to stdout and masking is a no-op.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from lktec_bot.text import neutralise_commands, sanitize

SUMMARY_VAR = "GITHUB_STEP_SUMMARY"
OUTPUT_VAR = "GITHUB_OUTPUT"
ACTIONS_VAR = "GITHUB_ACTIONS"


def write_outputs(
    values: Mapping[str, str | None], *, env: Mapping[str, str] | None = None
) -> None:
    """Append ``name=value`` lines to the step's ``GITHUB_OUTPUT`` file, when there is one.

    Values are collapsed to one line (a multi-line value would need the heredoc form and
    could otherwise smuggle in a second output); ``None`` becomes an empty output. Outside
    Actions nothing is written: the outputs are for the calling workflow only.
    """
    environment = env if env is not None else os.environ
    path = environment.get(OUTPUT_VAR)
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        for name, value in values.items():
            handle.write(f"{name}={sanitize(value or '')}\n")


def write_summary(
    markdown: str,
    *,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
) -> None:
    """Append ``markdown`` to the step summary, or print it when there is none.

    No line of the summary may start with ``::``: the summary file is not parsed for workflow
    commands, but the same text goes to stdout outside Actions and the rule is cheap.
    """
    environment = env if env is not None else os.environ
    text = neutralise_commands(markdown)
    text = text if text.endswith("\n") else text + "\n"
    path = environment.get(SUMMARY_VAR)
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(text)
        return
    (out or sys.stdout).write(text)


def mask_secret(
    value: str,
    *,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
) -> None:
    """Ask the Actions runner to mask ``value`` in every log line from now on.

    Emitted right after a token is minted, before anything else can print it. Outside Actions
    (``GITHUB_ACTIONS`` unset) nothing is written: the line would only expose the value.
    """
    environment = env if env is not None else os.environ
    if not environment.get(ACTIONS_VAR) or not value:
        return
    stream = out or sys.stdout
    stream.write(f"::add-mask::{value}\n")
    stream.flush()
