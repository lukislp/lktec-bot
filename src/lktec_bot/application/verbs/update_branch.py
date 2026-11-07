# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""``update-branch``: merge the base branch into the pull request branch (the "Update branch"
button). GitHub queues the merge commit and answers 202; a branch that already contains the
base is reported as ``ok`` with a message, not as an error.
"""

from __future__ import annotations

from lktec_bot.application.gate import VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.verbs import register
from lktec_bot.application.verbs._common import NUMBER, not_actionable, number_of
from lktec_bot.domain.permissions import Grant, Permission, Verb
from lktec_bot.ports.github_api import BranchUpdate, GitHubApi


@register(
    Verb.UPDATE_BRANCH,
    grants=frozenset(
        {Grant(Permission.CONTENTS, "write"), Grant(Permission.PULL_REQUESTS, "write")}
    ),
    options=(NUMBER,),
)
def update_branch(api: GitHubApi, request: VerbRequest) -> VerbResult:
    number = number_of(request)
    pull_request = api.get_pull_request(request.owner, request.repo, number)
    problem = not_actionable(pull_request)
    if problem is not None:
        return VerbResult(
            VerbOutcome.ERROR, f"refusing to update: {problem}", url=pull_request.html_url
        )
    outcome = api.update_branch(request.owner, request.repo, number, pull_request.head_sha)
    if outcome is BranchUpdate.UP_TO_DATE:
        message = f"#{number} is already up to date with its base"
    else:
        message = f"update of #{number} accepted; GitHub is merging the base into the branch"
    return VerbResult(VerbOutcome.OK, message, url=pull_request.html_url)
