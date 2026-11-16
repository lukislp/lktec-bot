# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Which dependency bot opened a pull request, and how its pull requests are read. Pure.

* **Dependabot**: the author is exactly ``dependabot[bot]``. Always on.
* **Renovate**: the author is exactly ``renovate[bot]`` (the Mend Renovate App; a login ending in
  ``[bot]`` belongs to one GitHub App and cannot be taken by a person), the head branch starts
  with ``renovate/`` and lives in the repository itself, not in a fork. Only with the policy
  option ``renovate: true``; without it a Renovate pull request is anyone's pull request, as
  before.

Everything after the recognition (exclude list, majors, workflow files, merge-ready) is the
same for both; only the reading of the title and body differs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from lktec_bot.application.modules.automerge.classify import (
    DEPENDABOT_LOGIN,
    Classification,
    classify,
)
from lktec_bot.application.modules.automerge.options import AutomergeOptions
from lktec_bot.application.modules.automerge.renovate import (
    RENOVATE_BRANCH_PREFIX,
    RENOVATE_LOGIN,
    classify_renovate,
)
from lktec_bot.ports.github_api import PullRequest


class UpdateSource(StrEnum):
    """A dependency bot whose pull requests the module arms."""

    DEPENDABOT = "dependabot"
    RENOVATE = "renovate"


@dataclass(frozen=True, slots=True)
class Recognition:
    """The bot that opened a pull request; ``source`` is ``None`` and ``reason`` says why when
    it is none the module arms."""

    source: UpdateSource | None
    reason: str = ""


def recognise(pull_request: PullRequest, repository: str, options: AutomergeOptions) -> Recognition:
    """Which bot opened ``pull_request`` in ``repository`` (``owner/name``), under ``options``."""
    author = pull_request.author_login
    if author == DEPENDABOT_LOGIN:
        return Recognition(UpdateSource.DEPENDABOT)
    if not options.renovate:
        return Recognition(None, f"not opened by {DEPENDABOT_LOGIN}")
    if author != RENOVATE_LOGIN:
        return Recognition(None, f"not opened by {DEPENDABOT_LOGIN} or {RENOVATE_LOGIN}")
    if not pull_request.head_ref.startswith(RENOVATE_BRANCH_PREFIX):
        return Recognition(
            None, f"{RENOVATE_LOGIN} pull request not on a {RENOVATE_BRANCH_PREFIX} branch"
        )
    if pull_request.head_repo.casefold() != repository.casefold():
        return Recognition(None, f"{RENOVATE_LOGIN} pull request from another repository")
    return Recognition(UpdateSource.RENOVATE)


def classify_update(source: UpdateSource, title: str, body: str) -> Classification:
    """Read a pull request of ``source`` the way that bot writes it."""
    if source is UpdateSource.RENOVATE:
        return classify_renovate(title, body)
    return classify(title, body)
