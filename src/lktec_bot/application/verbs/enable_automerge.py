# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""``enable-automerge``: ask GitHub to merge the pull request once its required checks and
reviews are green. The bot never merges; branch protection does.

Idempotent: auto-merge that is already enabled with the requested method is reported as
``ok``. Enabled with another method is an error, because silently keeping the other method
would not be what the caller asked for (``gh pr merge --disable-auto`` resets it).

A pull request GitHub considers mergeable right now cannot be armed. The mutation is tried up
to six times, ten seconds apart; when it is still refused the result is ``ok`` with
``decision=ready``: nothing is wrong, a person has to merge it. The ``decision`` output is
``enabled``, ``unchanged`` or ``ready``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import cast

from lktec_bot.application.gate import VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.verbs import Option, register
from lktec_bot.application.verbs._common import NUMBER, not_actionable, number_of
from lktec_bot.domain.permissions import Grant, Permission, Verb
from lktec_bot.ports.github_api import AutoMergeNotNeeded, GitHubApi, MergeMethod

METHODS = ("squash", "merge", "rebase")
# GitHub refuses auto-merge on a pull request that can be merged right now, which includes one
# whose required checks are not registered yet (a Dependabot pull request seconds after it was
# opened). The checks show up a few seconds later, so the mutation is retried before the pull
# request is reported as ready for a person.
ATTEMPTS = 6
INTERVAL = 10.0
# Module level so tests replace it and do not wait (``sleep`` is looked up at call time).
sleep: Callable[[float], None] = time.sleep


@register(
    Verb.ENABLE_AUTOMERGE,
    # contents:write as well as pull_requests:write: GitHub refuses the mutation without it
    # ("Resource not accessible by integration"), verified live against lukislp/lktec-bot#4.
    grants=frozenset(
        {Grant(Permission.CONTENTS, "write"), Grant(Permission.PULL_REQUESTS, "write")}
    ),
    options=(
        NUMBER,
        Option("method", "How to merge once the checks are green.", kind="choice", choices=METHODS),
    ),
)
def enable_automerge(api: GitHubApi, request: VerbRequest) -> VerbResult:
    number = number_of(request)
    raw_method = str(request.args.get("method", "squash")).lower()
    if raw_method not in METHODS:
        return VerbResult(
            VerbOutcome.ERROR, f"--method must be one of {', '.join(METHODS)}, not {raw_method!r}"
        )
    method = cast(MergeMethod, raw_method.upper())
    pull_request = api.get_pull_request(request.owner, request.repo, number)
    problem = not_actionable(pull_request)
    if problem is not None:
        return VerbResult(
            VerbOutcome.ERROR,
            f"refusing to enable auto-merge: {problem}",
            url=pull_request.html_url,
        )
    current = pull_request.auto_merge_method
    if current == method:
        return VerbResult(
            VerbOutcome.OK,
            f"auto-merge ({raw_method}) is already enabled on #{number}",
            url=pull_request.html_url,
            outputs={"decision": "unchanged"},
        )
    if current is not None:
        return VerbResult(
            VerbOutcome.ERROR,
            f"auto-merge is already enabled on #{number} with method {current.lower()}, "
            f"not {raw_method}; disable it first",
            url=pull_request.html_url,
        )
    refused = ""
    for attempt in range(ATTEMPTS):
        try:
            url = api.enable_auto_merge(request.owner, request.repo, number, method)
        except AutoMergeNotNeeded as exc:
            refused = exc.state
            if attempt + 1 < ATTEMPTS:
                sleep(INTERVAL)
            continue
        return VerbResult(
            VerbOutcome.OK,
            f"auto-merge ({raw_method}) enabled on #{number}",
            url=url,
            outputs={"decision": "enabled"},
        )
    # Not an error: the pull request can be merged now, which only a person may do.
    return VerbResult(
        VerbOutcome.OK,
        f"#{number} is already mergeable ({refused}): GitHub does not enable auto-merge on a "
        "pull request that can be merged now; it needs a person to merge it",
        url=pull_request.html_url,
        outputs={"decision": "ready", "state": refused},
    )
