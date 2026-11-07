# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Small helpers shared by the pull request verbs. Not a verb: the leading underscore keeps
``load_verbs`` from importing it as one."""

from __future__ import annotations

from lktec_bot.application.gate import VerbRequest
from lktec_bot.application.verbs import Option
from lktec_bot.ports.github_api import PullRequest

NUMBER = Option("number", "Pull request (or issue) number.", kind="int", required=True)


def number_of(request: VerbRequest) -> int:
    """The ``--number`` argument as an int; a missing or bad value is a usage bug, so raise."""
    raw = request.args.get("number")
    if isinstance(raw, bool) or not isinstance(raw, int | str):
        raise ValueError("the verb needs a numeric --number")
    try:
        number = int(raw)
    except ValueError:
        raise ValueError(f"--number {raw!r} is not a number") from None
    if number <= 0:
        raise ValueError(f"--number must be positive, not {number}")
    return number


def not_actionable(pull_request: PullRequest) -> str | None:
    """Why a review-type verb must not touch this pull request, or ``None`` when it may."""
    if not pull_request.open:
        return f"pull request #{pull_request.number} is {pull_request.state}, not open"
    if pull_request.draft:
        return f"pull request #{pull_request.number} is a draft"
    return None
