"""``comment``: post a comment on a pull request or an issue, or update the bot's own earlier
comment when a marker is given.

The marker is an HTML comment (``<!-- lktec-bot:<id> -->``) appended to the body; it does not
render. On the next run with the same marker the verb looks for a comment that carries the
marker *and* was written by the login the token acts as, and edits that one instead of adding
another. A comment by anyone else that happens to contain the marker is never touched.
"""

from __future__ import annotations

import re

from lktec_bot.application.gate import VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.verbs import Option, register
from lktec_bot.application.verbs._common import NUMBER, number_of
from lktec_bot.domain.permissions import Grant, Permission, Target, Verb
from lktec_bot.ports.github_api import GitHubApi

_MARKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def marker_tag(marker: str) -> str:
    """The invisible tag for ``marker``; the marker itself is restricted to a safe alphabet."""
    if not _MARKER.match(marker):
        raise ValueError(
            f"--marker {marker!r} may only contain letters, digits, '-', '_' and '.' "
            "(up to 64 characters)"
        )
    return f"<!-- lktec-bot:{marker} -->"


@register(
    Verb.COMMENT,
    grants={
        Target.PULL_REQUEST: frozenset({Grant(Permission.PULL_REQUESTS, "write")}),
        Target.ISSUE: frozenset({Grant(Permission.ISSUES, "write")}),
    },
    options=(
        NUMBER,
        Option("body", "The comment text.", kind="text", required=True),
        Option(
            "marker",
            "Identifier of a comment the bot owns: update it instead of adding a new one.",
        ),
    ),
)
def comment(api: GitHubApi, request: VerbRequest) -> VerbResult:
    number = number_of(request)
    body = str(request.args.get("body", ""))
    marker = request.args.get("marker")
    if not body.strip():
        return VerbResult(VerbOutcome.ERROR, "the comment body is empty")
    if marker is None or marker == "":
        url = api.create_issue_comment(request.owner, request.repo, number, body)
        return VerbResult(VerbOutcome.OK, f"commented on #{number}", url=url)

    tag = marker_tag(str(marker))
    if not request.actor:
        return VerbResult(
            VerbOutcome.ERROR,
            "cannot update a marked comment: the token provider did not report the bot's "
            "login, and a comment that may belong to someone else is never edited",
        )
    tagged = f"{body.rstrip()}\n\n{tag}\n"
    existing = [
        entry
        for entry in api.list_issue_comments(request.owner, request.repo, number)
        if entry.user_login == request.actor and tag in entry.body
    ]
    if existing:
        if existing[0].body == tagged:
            return VerbResult(
                VerbOutcome.OK,
                f"comment on #{number} with marker {marker!r} is already up to date",
                url=existing[0].html_url,
            )
        url = api.update_issue_comment(request.owner, request.repo, existing[0].id, tagged)
        return VerbResult(
            VerbOutcome.OK, f"updated the comment on #{number} with marker {marker!r}", url=url
        )
    url = api.create_issue_comment(request.owner, request.repo, number, tagged)
    return VerbResult(VerbOutcome.OK, f"commented on #{number} with marker {marker!r}", url=url)
