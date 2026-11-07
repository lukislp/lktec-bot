# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""``open-pr``: open a pull request from a branch, or bring the open one up to date.

An upsert keyed on ``head`` -> ``base``: when an open pull request for that pair exists, its
title, body and draft state are changed to what was asked (only the fields that differ; a body
that is not given is left alone), otherwise one is opened. The head branch must live in the
target repository. Permission: ``pull_requests:write``. GitHub's REST API cannot toggle the
draft state, so that one change goes through GraphQL.
"""

from __future__ import annotations

from lktec_bot.application.gate import VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.verbs import Option, register
from lktec_bot.domain.permissions import Grant, Permission, Verb
from lktec_bot.ports.github_api import GitHubApi


def _fail(message: str) -> VerbResult:
    return VerbResult(VerbOutcome.ERROR, message, outputs={"decision": "error"})


@register(
    Verb.OPEN_PR,
    grants=frozenset({Grant(Permission.PULL_REQUESTS, "write")}),
    options=(
        Option("head", "Branch with the changes (in the target repository).", required=True),
        Option("base", "Branch the changes should be merged into.", required=True),
        Option("title", "Pull request title.", required=True),
        Option("body", "Pull request description (markdown).", kind="text"),
        Option("draft", "Open or keep the pull request as a draft.", kind="flag"),
    ),
)
def open_pr(api: GitHubApi, request: VerbRequest) -> VerbResult:
    head = str(request.args.get("head", ""))
    base = str(request.args.get("base", ""))
    title = str(request.args.get("title", "")).strip()
    raw_body = request.args.get("body")
    body = None if raw_body is None else str(raw_body)
    draft = bool(request.args.get("draft", False))
    if not head or not base:
        return _fail("--head and --base are required")
    if head.startswith("refs/") or base.startswith("refs/"):
        return _fail("--head and --base must be branch names, not full refs")
    if ":" in head:
        return _fail("--head must be a branch of the target repository, without an owner prefix")
    if head == base:
        return _fail("--head and --base must be different branches")
    if not title or "\n" in title or "\r" in title:
        return _fail("--title is required and must be one line")

    existing = api.find_open_pull_request(request.owner, request.repo, head, base)
    if existing is None:
        created = api.create_pull_request(
            request.owner, request.repo, head, base, title, body or "", draft=draft
        )
        return VerbResult(
            VerbOutcome.OK,
            f"opened #{created.number} from {head} into {base}",
            url=created.html_url,
            outputs={"decision": "opened", "number": str(created.number)},
        )

    new_title = title if title != existing.title else None
    new_body = body if body is not None and body != existing.body else None
    changes: list[str] = []
    if new_title is not None or new_body is not None:
        api.update_pull_request(
            request.owner, request.repo, existing.number, title=new_title, body=new_body
        )
        if new_title is not None:
            changes.append("title")
        if new_body is not None:
            changes.append("body")
    if draft != existing.draft:
        api.set_pull_request_draft(request.owner, request.repo, existing.number, draft)
        changes.append("draft" if draft else "ready for review")
    outputs = {"number": str(existing.number)}
    if not changes:
        return VerbResult(
            VerbOutcome.OK,
            f"#{existing.number} from {head} into {base} is already up to date",
            url=existing.html_url,
            outputs={**outputs, "decision": "unchanged"},
        )
    return VerbResult(
        VerbOutcome.OK,
        f"updated #{existing.number} ({', '.join(changes)})",
        url=existing.html_url,
        outputs={**outputs, "decision": "updated"},
    )
