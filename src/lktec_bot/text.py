# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Pure text helpers for everything the bot prints or writes to a job summary.

Messages reach the output from places a repository owner does not control: GitHub error
bodies, policy files, module names on the command line. Two things must never happen with
them: a message must not span lines (a second line could look like a result of its own), and
it must not form a GitHub Actions workflow command (``::set-env ...``, ``::add-mask::``), which
the runner interprets when it appears on stdout.
"""

from __future__ import annotations

import re

_LINE_BREAKS = re.compile(r"[\r\n]+")
_COMMAND_PREFIX = re.compile(r"(?m)^::")
# Characters that give markdown structure when they appear in a message. ``-``, ``.`` and
# ``(`` are left alone: they only matter at a line start, where a label always precedes them.
_MARKDOWN = re.compile(r"([\\`*_\[\]<>|~#])")


def sanitize(text: str) -> str:
    """Collapse ``text`` to one line and make sure it cannot be a workflow command.

    Line breaks become single spaces, and every ``::`` becomes ``: :``. The runner only acts on
    a ``::`` at the start of a line, but breaking the token wherever it appears keeps the rule
    independent of how the line is later prefixed or wrapped.
    """
    return _LINE_BREAKS.sub(" ", text).replace("::", ": :")


def neutralise_commands(text: str) -> str:
    """For multi-line output: break a ``::`` at the start of any line, keep the rest as is."""
    return _COMMAND_PREFIX.sub(": :", text)


def escape_markdown(text: str) -> str:
    """Escape markdown structure in ``text`` after collapsing it to one line."""
    return _MARKDOWN.sub(r"\\\1", sanitize(text))
