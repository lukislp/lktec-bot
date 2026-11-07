# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""``open-issue``: open an issue, or update the bot's own marked one instead of opening another.

Without a marker every call opens an issue. With a marker the body carries the same invisible
tag as a marked ``comment`` (``<!-- lktec-bot:<id> -->``) and the verb looks for an issue that
carries the tag *and* was opened by the login the token acts as; an issue by anyone else that
happens to contain the tag is never touched. A match is brought up to date (title, body and the
labels it lacks; labels are only ever added, a maintainer may have added some), a closed one is
reopened unless ``--no-reopen`` asks for a fresh issue. Permission: ``issues:write``.
"""

from __future__ import annotations

from lktec_bot.application.gate import VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.verbs import Option, register
from lktec_bot.application.verbs.comment import marker_tag
from lktec_bot.domain.permissions import Grant, Permission, Verb
from lktec_bot.ports.github_api import GitHubApi, Issue

_MAX_TITLE = 256
_MAX_LABEL = 50


def _fail(message: str) -> VerbResult:
    return VerbResult(VerbOutcome.ERROR, message, outputs={"decision": "error"})


def _done(decision: str, issue: Issue, message: str) -> VerbResult:
    return VerbResult(
        VerbOutcome.OK,
        message,
        url=issue.html_url,
        outputs={"decision": decision, "number": str(issue.number)},
    )


def _labels_of(request: VerbRequest) -> list[str] | str:
    """The requested labels, de-duplicated in order, or the reason one is invalid."""
    raw = request.args.get("label")
    labels: list[str] = []
    for entry in raw if isinstance(raw, list | tuple) else ():
        label = str(entry).strip()
        if not label or len(label) > _MAX_LABEL or "\n" in label or "\r" in label:
            return f"--label must be 1 to {_MAX_LABEL} characters on one line, got {entry!r}"
        if label.casefold() not in {seen.casefold() for seen in labels}:
            labels.append(label)
    return labels


@register(
    Verb.OPEN_ISSUE,
    grants=frozenset({Grant(Permission.ISSUES, "write")}),
    options=(
        Option("title", "Issue title (one line).", required=True),
        Option("body", "Issue text (markdown).", kind="text", required=True),
        Option(
            "marker",
            "Identifier of an issue the bot owns: update that one instead of opening another.",
        ),
        Option(
            "label",
            "Label to put on the issue (must exist in the repository); repeatable.",
            kind="multi",
        ),
        Option(
            "reopen",
            "With a marker: reopen the bot's closed issue instead of opening a new one.",
            kind="flag",
            default=True,
        ),
    ),
)
def open_issue(api: GitHubApi, request: VerbRequest) -> VerbResult:
    title = str(request.args.get("title", "")).strip()
    body = str(request.args.get("body", ""))
    marker = request.args.get("marker")
    reopen = bool(request.args.get("reopen", True))
    if not title or len(title) > _MAX_TITLE or "\n" in title or "\r" in title:
        return _fail(f"--title is required and must be one line of up to {_MAX_TITLE} characters")
    if not body.strip():
        return _fail("the issue body is empty")
    labels = _labels_of(request)
    if isinstance(labels, str):
        return _fail(labels)

    if marker is None or marker == "":
        created = api.create_issue(
            request.owner, request.repo, title=title, body=body, labels=labels
        )
        return _done("opened", created, f"opened #{created.number}")

    try:
        tag = marker_tag(str(marker))
    except ValueError as exc:
        return _fail(str(exc))
    if not request.actor:
        return _fail(
            "cannot update a marked issue: the token provider did not report the bot's "
            "login, and an issue that may belong to someone else is never edited"
        )
    tagged = f"{body.rstrip()}\n\n{tag}\n"
    matches = sorted(
        (
            issue
            for issue in api.list_issues(request.owner, request.repo, creator=request.actor)
            if issue.author_login == request.actor and tag in issue.body
        ),
        key=lambda issue: issue.number,
    )
    others = ", ".join(f"#{other.number}" for other in matches[1:])
    note = f"; also carrying marker {marker!r}: {others}" if others else ""

    if not matches:
        return _create_marked(api, request, title, tagged, labels, marker, "")
    issue = matches[0]
    if issue.state != "open" and not reopen:
        return _create_marked(
            api, request, title, tagged, labels, marker, f"; #{issue.number} is closed{note}"
        )

    changes: list[str] = []
    new_title = title if title != issue.title else None
    if new_title is not None:
        changes.append("title")
    new_body = tagged if tagged != issue.body else None
    if new_body is not None:
        changes.append("body")
    have = {label.casefold() for label in issue.labels}
    missing = [label for label in labels if label.casefold() not in have]
    if missing:
        changes.append("labels")
    reopening = issue.state != "open"
    if not changes and not reopening:
        return _done(
            "unchanged",
            issue,
            f"#{issue.number} with marker {marker!r} is already up to date{note}",
        )
    updated = api.update_issue(
        request.owner,
        request.repo,
        issue.number,
        title=new_title,
        body=new_body,
        state="open" if reopening else None,
        labels=[*issue.labels, *missing] if missing else None,
    )
    detail = f" ({', '.join(changes)})" if changes else ""
    if reopening:
        return _done("reopened", updated, f"reopened #{issue.number}{detail}{note}")
    return _done("updated", updated, f"updated #{issue.number}{detail}{note}")


def _create_marked(
    api: GitHubApi,
    request: VerbRequest,
    title: str,
    tagged: str,
    labels: list[str],
    marker: object,
    note: str,
) -> VerbResult:
    created = api.create_issue(request.owner, request.repo, title=title, body=tagged, labels=labels)
    return _done("opened", created, f"opened #{created.number} with marker {marker!r}{note}")
