# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""``approve``: submit an approving review on an open, non-draft pull request.

The verb is idempotent per head commit: when the bot already approved the pull request's
current head, nothing is submitted and the earlier review's URL is returned. A new head (a
pushed commit) gets a new review, which is the point of reviewing.
"""

from __future__ import annotations

from lktec_bot.application.gate import VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.verbs import Option, register
from lktec_bot.application.verbs._common import NUMBER, not_actionable, number_of
from lktec_bot.domain.permissions import Grant, Permission, Verb
from lktec_bot.ports.github_api import GitHubApi


@register(
    Verb.APPROVE,
    grants=frozenset({Grant(Permission.PULL_REQUESTS, "write")}),
    options=(NUMBER, Option("body", "Optional review text.", kind="text")),
)
def approve(api: GitHubApi, request: VerbRequest) -> VerbResult:
    number = number_of(request)
    body = str(request.args.get("body", ""))
    pull_request = api.get_pull_request(request.owner, request.repo, number)
    problem = not_actionable(pull_request)
    if problem is not None:
        return VerbResult(
            VerbOutcome.ERROR, f"refusing to approve: {problem}", url=pull_request.html_url
        )
    if request.actor:
        for review in api.list_reviews(request.owner, request.repo, number):
            if (
                review.user_login == request.actor
                and review.state == "APPROVED"
                and review.commit_id == pull_request.head_sha
            ):
                return VerbResult(
                    VerbOutcome.OK,
                    f"#{number} is already approved at {pull_request.head_sha[:12]}",
                    url=review.html_url,
                )
    url = api.create_review(request.owner, request.repo, number, "APPROVE", body)
    return VerbResult(
        VerbOutcome.OK, f"approved #{number} at {pull_request.head_sha[:12]}", url=url
    )
