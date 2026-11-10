# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""``merge``: merge a pull request GitHub refused to arm for auto-merge because it is already
mergeable ("clean status").

The verb is deliberately narrow and checks everything itself, right before it merges; any doubt
is a refusal (``error`` with ``decision=refused``), never a merge:

- the pull request is open, not a draft, and its head is the ``expected_head_sha`` the caller
  decided on (passed on as ``sha``, so a push between decision and merge fails at GitHub too);
- GitHub's ``mergeable_state`` is ``clean``: required checks green, reviews satisfied, branch
  up to date under a strict rule. ``unknown`` is read again a few times;
- the latest review by the App on that head is an approval.

It uses only ``PUT /pulls/{n}/merge``, the call that respects branch protection and rulesets;
there is no admin bypass. It holds ``workflows:write`` because GitHub refuses an App's merge of
a pull request that changes ``.github/workflows/`` without it; it never writes workflow content.
The ``decision`` output is ``merged`` or ``refused``; ``sha`` is the merge commit.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import cast

from lktec_bot.application.gate import VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.verbs import Option, register
from lktec_bot.application.verbs._common import NUMBER, not_actionable, number_of
from lktec_bot.domain.permissions import Grant, Permission, Verb
from lktec_bot.ports.github_api import GitHubApi, MergeMethod, MergeRefused, PullRequest

METHODS = ("squash", "merge", "rebase")
CLEAN = "CLEAN"
UNKNOWN = "UNKNOWN"
# GitHub computes ``mergeable_state`` lazily; UNKNOWN is read again (this many reads in all).
READS = 3
INTERVAL = 2.0
# Module level so tests replace it and do not wait (``sleep`` is looked up at call time).
sleep: Callable[[float], None] = time.sleep


def _refused(message: str, url: str | None = None) -> VerbResult:
    return VerbResult(
        VerbOutcome.ERROR, f"refusing to merge: {message}", url=url, outputs={"decision": "refused"}
    )


def _settled(api: GitHubApi, request: VerbRequest, number: int) -> PullRequest:
    """The pull request with a computed merge state (or still ``UNKNOWN`` after ``READS``)."""
    pull_request = api.get_pull_request(request.owner, request.repo, number)
    for _ in range(READS - 1):
        if pull_request.merge_state != UNKNOWN:
            break
        sleep(INTERVAL)
        pull_request = api.get_pull_request(request.owner, request.repo, number)
    return pull_request


@register(
    Verb.MERGE,
    grants=frozenset(
        {
            Grant(Permission.CONTENTS, "write"),
            Grant(Permission.PULL_REQUESTS, "write"),
            Grant(Permission.WORKFLOWS, "write"),
        }
    ),
    options=(
        NUMBER,
        Option("method", "How to merge.", kind="choice", choices=METHODS),
        Option(
            "expected_head_sha",
            "The head commit the decision to merge was made on.",
            kind="str",
            required=True,
        ),
    ),
)
def merge(api: GitHubApi, request: VerbRequest) -> VerbResult:
    number = number_of(request)
    raw_method = str(request.args.get("method", "squash")).lower()
    if raw_method not in METHODS:
        return VerbResult(
            VerbOutcome.ERROR, f"--method must be one of {', '.join(METHODS)}, not {raw_method!r}"
        )
    method = cast(MergeMethod, raw_method.upper())
    expected = str(request.args.get("expected_head_sha", ""))
    if not expected:
        return VerbResult(VerbOutcome.ERROR, "--expected-head-sha is required")
    pull_request = _settled(api, request, number)
    url = pull_request.html_url
    problem = not_actionable(pull_request)
    if problem is not None:
        return _refused(problem, url)
    if pull_request.head_sha != expected:
        return _refused(
            f"#{number} head is {pull_request.head_sha[:12]}, not the expected {expected[:12]}", url
        )
    if pull_request.merge_state != CLEAN:
        state = (pull_request.merge_state or "not reported").lower()
        return _refused(f"#{number} is not mergeable right now (state: {state})", url)
    approved = False
    for review in api.list_reviews(request.owner, request.repo, number):
        if request.actor and review.user_login == request.actor and review.commit_id == expected:
            approved = review.state == "APPROVED"  # the latest review of the App on this head
    if not approved:
        return _refused(f"the App has not approved #{number} at {expected[:12]}", url)
    try:
        merged = api.merge_pull_request(request.owner, request.repo, number, method, expected)
    except MergeRefused as exc:
        return _refused(f"GitHub says: {exc.message}", url)
    return VerbResult(
        VerbOutcome.OK,
        f"merged #{number} ({raw_method}) at {expected[:12]}",
        url=url,
        outputs={"decision": "merged", "sha": merged},
    )
