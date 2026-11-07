# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""``check``: publish a check run of the App on a commit, creating it or updating it.

An upsert keyed on (commit sha, check name, this App): when a check run of this App with that
name already exists on the commit it is updated, otherwise one is created. A later module can
use it to show "lock file regenerated" or "next version 1.5.0" in the checks list of a pull
request instead of a job summary. Permission: ``checks:write``.

Which check runs are "of this App": the list endpoint reports ``app.id`` and ``app.slug`` for
every run, but the verb only learns the bot's login (``VerbRequest.actor``, ``<slug>[bot]``);
the App id is known to the token provider alone, and handing it to verbs would widen
``Token`` and ``VerbRequest`` for this one verb. So the filter is ``app.slug`` against the
login without its ``[bot]`` suffix. A slug is unique on GitHub, which is all the filter needs,
and a check run of another App with the same name is never read as ours, updated or touched.
Without an actor the verb refuses to run, as ``comment`` does for a marked comment.

With several matches the one with the highest id wins. It is compared with what was asked
(status, conclusion, title, summary, text, and the details URL and external id when given; an
empty value and none are the same, and summary and text are compared without surrounding
whitespace) and updated only when something differs. A run of the App that exists is never
duplicated.

Annotations come from a JSON file. GitHub takes 50 per request, so the first 50 travel with the
create or update call and every further batch of up to 50 is an extra update with the same
title and summary and only ``output.annotations``. GitHub appends annotations and an
annotation cannot be read back cheaply, so a run with an annotations file always updates, and
running it twice puts the annotations on the check run twice.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from lktec_bot.application.gate import VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.verbs import Option, register
from lktec_bot.domain.permissions import Grant, Permission, Verb
from lktec_bot.ports.github_api import CheckRun, GitHubApi

_SHA = re.compile(r"[0-9a-f]{40}")
_EXTERNAL_ID = re.compile(r"[A-Za-z0-9_./:-]{1,64}")
STATUSES = ("queued", "in_progress", "completed")
CONCLUSIONS = (
    "success",
    "failure",
    "neutral",
    "cancelled",
    "skipped",
    "timed_out",
    "action_required",
)
LEVELS = ("notice", "warning", "failure")
MAX_NAME = 100
MAX_TITLE = 255
MAX_BODY = 65535  # GitHub's limit for the summary and for the text
MAX_BATCH = 50  # annotations per request
MAX_ANNOTATIONS = 500
MAX_ANNOTATION_MESSAGE = 1000
_ANNOTATION_KEYS = frozenset(
    {"path", "start_line", "end_line", "annotation_level", "message", "title"}
)


class _Refusal(Exception):
    """A request the verb refuses before any API call; the text becomes the result message."""


def _fail(message: str) -> VerbResult:
    return VerbResult(VerbOutcome.ERROR, message, outputs={"decision": "error"})


def _optional(request: VerbRequest, name: str) -> str | None:
    """An optional argument; an empty string is the same as not given (action inputs)."""
    raw = request.args.get(name)
    return None if raw is None or raw == "" else str(raw)


def _one_line(flag: str, value: str, limit: int) -> str:
    text = value.strip()
    if not text or "\n" in text or "\r" in text:
        raise _Refusal(f"{flag} is required and must be one line")
    if len(text) > limit:
        raise _Refusal(f"{flag} must be at most {limit} characters")
    return text


def _body(flag: str, value: str) -> str:
    text = value.strip()
    if len(text) > MAX_BODY:
        raise _Refusal(f"{flag} must be at most {MAX_BODY} characters")
    return text


def _annotation(index: int, entry: object) -> dict[str, Any]:
    def bad(field: str, why: str) -> _Refusal:
        return _Refusal(f"--annotations-file: annotation {index}, field {field!r}: {why}")

    if not isinstance(entry, dict):
        raise _Refusal(f"--annotations-file: annotation {index} is not an object")
    for key in sorted(entry):
        if key not in _ANNOTATION_KEYS:
            raise bad(str(key), "unknown field")
    path = entry.get("path")
    if not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/"):
        raise bad("path", "must be a repository-relative path without '..'")
    lines: dict[str, int] = {}
    for field in ("start_line", "end_line"):
        value = entry.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise bad(field, "must be an integer of at least 1")
        lines[field] = value
    if lines["start_line"] > lines["end_line"]:
        raise bad("end_line", "must not be smaller than start_line")
    level = entry.get("annotation_level")
    if level not in LEVELS:
        raise bad("annotation_level", f"must be one of {', '.join(LEVELS)}")
    message = entry.get("message")
    if not isinstance(message, str) or not message.strip():
        raise bad("message", "must be a non-empty string")
    if len(message.strip()) > MAX_ANNOTATION_MESSAGE:
        raise bad("message", f"must be at most {MAX_ANNOTATION_MESSAGE} characters")
    clean: dict[str, Any] = {
        "path": path,
        "start_line": lines["start_line"],
        "end_line": lines["end_line"],
        "annotation_level": level,
        "message": message.strip(),
    }
    if "title" in entry:
        title = entry["title"]
        if not isinstance(title, str) or "\n" in title or "\r" in title:
            raise bad("title", "must be a one line string")
        if len(title.strip()) > MAX_TITLE:
            raise bad("title", f"must be at most {MAX_TITLE} characters")
        clean["title"] = title.strip()
    return clean


def _annotations(path: str | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise _Refusal(f"--annotations-file: cannot read {path}: {exc}") from None
    except ValueError as exc:
        raise _Refusal(f"--annotations-file: {path} is not valid JSON: {exc}") from None
    if not isinstance(raw, list):
        raise _Refusal("--annotations-file: the file must hold a JSON array")
    if len(raw) > MAX_ANNOTATIONS:
        raise _Refusal(f"--annotations-file: {len(raw)} annotations, at most {MAX_ANNOTATIONS}")
    return [_annotation(index, entry) for index, entry in enumerate(raw)]


def _same(left: str | None, right: str | None) -> bool:
    """Equal, with none and the empty string being the same."""
    return (left or "") == (right or "")


def _differs(
    run: CheckRun,
    *,
    status: str,
    conclusion: str | None,
    title: str,
    summary: str,
    text: str | None,
    details_url: str | None,
    external_id: str | None,
) -> bool:
    return not (
        run.status == status
        and _same(run.conclusion, conclusion)
        and _same(run.title, title)
        and _same((run.summary or "").strip(), summary)
        and _same((run.text or "").strip(), text)
        # The URL and the id are only compared when asked for: GitHub fills in a details URL of
        # its own, and a run without a wish for either must not be rewritten for that.
        and (details_url is None or run.details_url == details_url)
        and (external_id is None or run.external_id == external_id)
    )


@register(
    Verb.CHECK,
    grants=frozenset({Grant(Permission.CHECKS, "write")}),
    options=(
        Option(
            "sha", "Full id (40 hex characters) of the commit the check run is on.", required=True
        ),
        Option("name", "Name of the check run (one line, up to 100 characters).", required=True),
        Option("title", "Title of the result (one line, up to 255 characters).", required=True),
        Option("summary", "Summary of the result (markdown).", kind="text", required=True),
        Option("text", "Details below the summary (markdown).", kind="text"),
        Option(
            "status",
            "Status of the check run; completed (default) needs --conclusion.",
            kind="choice",
            choices=STATUSES,
        ),
        Option(
            "conclusion",
            "Result of a completed check run.",
            kind="choice",
            choices=CONCLUSIONS,
        ),
        Option("details_url", "https://URL with more details; required for action_required."),
        Option("external_id", "Your own reference for the run (up to 64 characters)."),
        Option(
            "annotations_file",
            "JSON file with annotations: path, start_line, end_line, annotation_level, message "
            "and optionally title.",
        ),
    ),
)
def check(api: GitHubApi, request: VerbRequest) -> VerbResult:
    try:
        sha = str(request.args.get("sha", ""))
        if not _SHA.fullmatch(sha):
            raise _Refusal(f"--sha {sha!r} is not a full commit id (40 lowercase hex characters)")
        name = _one_line("--name", str(request.args.get("name", "")), MAX_NAME)
        title = _one_line("--title", str(request.args.get("title", "")), MAX_TITLE)
        summary = _body("--summary", str(request.args.get("summary") or ""))
        if not summary:
            raise _Refusal("--summary is required and must not be empty")
        text = _optional(request, "text")
        text = None if text is None or not text.strip() else _body("--text", text)
        status = _optional(request, "status") or "completed"
        if status not in STATUSES:
            raise _Refusal(f"--status {status!r} must be one of {', '.join(STATUSES)}")
        conclusion = _optional(request, "conclusion")
        if conclusion is not None and conclusion not in CONCLUSIONS:
            raise _Refusal(f"--conclusion {conclusion!r} must be one of {', '.join(CONCLUSIONS)}")
        if status == "completed" and conclusion is None:
            raise _Refusal("--conclusion is required when the status is completed")
        if status != "completed" and conclusion is not None:
            raise _Refusal(f"--conclusion must not be given when the status is {status}")
        details_url = _optional(request, "details_url")
        if details_url is not None and (
            not details_url.startswith("https://") or any(c.isspace() for c in details_url)
        ):
            raise _Refusal("--details-url must be an https:// URL without whitespace")
        if conclusion == "action_required" and details_url is None:
            raise _Refusal("--details-url is required when the conclusion is action_required")
        external_id = _optional(request, "external_id")
        if external_id is not None and not _EXTERNAL_ID.fullmatch(external_id):
            raise _Refusal(
                "--external-id may only contain letters, digits, '-', '_', '.', '/' and ':' "
                "(up to 64 characters)"
            )
        annotations = _annotations(_optional(request, "annotations_file"))
    except _Refusal as refusal:
        return _fail(str(refusal))

    if not request.actor:
        return _fail(
            "cannot tell which check runs belong to the bot: the token provider did not report "
            "the bot's login, and a check run of another App is never touched"
        )
    slug = request.actor.removesuffix("[bot]")
    short = sha[:7]
    label = f"check run {name!r} on {short}"

    ours = [
        run
        for run in api.list_check_runs(request.owner, request.repo, sha, name)
        if run.app_slug == slug
    ]
    existing = max(ours, key=lambda run: run.id, default=None)
    first, rest = annotations[:MAX_BATCH], annotations[MAX_BATCH:]

    if existing is None:
        run = api.create_check_run(
            request.owner,
            request.repo,
            name=name,
            sha=sha,
            status=status,
            conclusion=conclusion,
            details_url=details_url,
            external_id=external_id,
            title=title,
            summary=summary,
            text=text,
            annotations=first,
        )
        decision, message = "created", f"created {label} ({conclusion or status})"
    elif not annotations and not _differs(
        existing,
        status=status,
        conclusion=conclusion,
        title=title,
        summary=summary,
        text=text,
        details_url=details_url,
        external_id=external_id,
    ):
        return VerbResult(
            VerbOutcome.OK,
            f"{label} is already up to date",
            url=existing.html_url,
            outputs={"decision": "unchanged", "id": str(existing.id)},
        )
    else:
        run = api.update_check_run(
            request.owner,
            request.repo,
            existing.id,
            status=status,
            conclusion=conclusion,
            details_url=details_url,
            external_id=external_id,
            title=title,
            summary=summary,
            text=text or "",  # an empty text clears one the run had before
            annotations=first or None,
        )
        decision, message = "updated", f"updated {label} ({conclusion or status})"

    _send_batches(api, request, run.id, title, summary, rest)
    return VerbResult(
        VerbOutcome.OK,
        message,
        url=run.html_url,
        outputs={"decision": decision, "id": str(run.id)},
    )


def _send_batches(
    api: GitHubApi,
    request: VerbRequest,
    check_run_id: int,
    title: str,
    summary: str,
    rest: Sequence[Mapping[str, Any]],
) -> None:
    """The annotations after the first 50, 50 per update; GitHub needs the title and summary."""
    for start in range(0, len(rest), MAX_BATCH):
        api.update_check_run(
            request.owner,
            request.repo,
            check_run_id,
            title=title,
            summary=summary,
            annotations=rest[start : start + MAX_BATCH],
        )
