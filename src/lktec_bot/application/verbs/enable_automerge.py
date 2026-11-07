# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""``enable-automerge``: ask GitHub to merge the pull request once its required checks and
reviews are green. The bot never merges; branch protection does.

Idempotent: auto-merge that is already enabled with the requested method is reported as
``ok``. Enabled with another method is an error, because silently keeping the other method
would not be what the caller asked for (``gh pr merge --disable-auto`` resets it).
"""

from __future__ import annotations

from typing import cast

from lktec_bot.application.gate import VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.verbs import Option, register
from lktec_bot.application.verbs._common import NUMBER, not_actionable, number_of
from lktec_bot.domain.permissions import Grant, Permission, Verb
from lktec_bot.ports.github_api import GitHubApi, MergeMethod

METHODS = ("squash", "merge", "rebase")


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
        )
    if current is not None:
        return VerbResult(
            VerbOutcome.ERROR,
            f"auto-merge is already enabled on #{number} with method {current.lower()}, "
            f"not {raw_method}; disable it first",
            url=pull_request.html_url,
        )
    url = api.enable_auto_merge(request.owner, request.repo, number, method)
    return VerbResult(VerbOutcome.OK, f"auto-merge ({raw_method}) enabled on #{number}", url=url)
