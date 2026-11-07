# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""``release``: publish a GitHub release for a tag, creating the tag at a given commit.

``POST /releases`` with ``target_commitish`` makes GitHub create a lightweight tag ``--tag`` at
``--sha`` when the tag does not exist, so the only permission is ``contents:write`` and no
deploy key is needed. The tag ruleset of a repository forbids deleting and moving tags, not
creating them.

Idempotent and never moves a tag:

- the release exists and its tag points at ``--sha``: ``unchanged``;
- the tag exists at another commit: an error, nothing is written;
- otherwise the release is created (with the tag, or for the tag that already points at
  ``--sha``): ``created``.
"""

from __future__ import annotations

import re

from lktec_bot.application.gate import VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.verbs import Option, register
from lktec_bot.domain.permissions import Grant, Permission, Verb
from lktec_bot.ports.github_api import GitHubApi

_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
# A tag name git accepts, restricted to what a release tag looks like in practice.
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+@-]{0,199}\Z")


def _fail(message: str, tag: str = "") -> VerbResult:
    return VerbResult(VerbOutcome.ERROR, message, outputs={"decision": "error", "tag": tag})


@register(
    Verb.RELEASE,
    grants=frozenset({Grant(Permission.CONTENTS, "write")}),
    options=(
        Option(
            "tag", "Tag of the release, e.g. v1.2.3; created at --sha when missing.", required=True
        ),
        Option("sha", "Full id of the commit the tag points at.", required=True),
        Option("name", "Title of the release.", required=True),
        Option("body", "Release notes (markdown).", kind="text"),
        Option("latest", "Mark the release as the latest one.", kind="flag", default=True),
        Option("prerelease", "Mark the release as a prerelease.", kind="flag"),
    ),
)
def release(api: GitHubApi, request: VerbRequest) -> VerbResult:
    tag = str(request.args.get("tag", ""))
    sha = str(request.args.get("sha", ""))
    name = str(request.args.get("name", "")).strip()
    body = str(request.args.get("body", ""))
    latest = bool(request.args.get("latest", True))
    prerelease = bool(request.args.get("prerelease", False))
    if not _TAG.fullmatch(tag) or ".." in tag or tag.endswith(("/", ".lock", ".")):
        return _fail(f"--tag {tag!r} is not a usable tag name")
    if not _OID.fullmatch(sha):
        return _fail(f"--sha {sha!r} is not a full commit id", tag)
    if not name:
        return _fail("--name is required", tag)
    if prerelease and latest:
        # GitHub refuses a prerelease as the latest release; say so before calling it.
        latest = False

    existing = api.get_release_by_tag(request.owner, request.repo, tag)
    tagged = api.get_tag_commit(request.owner, request.repo, tag)
    if tagged is not None and tagged != sha:
        return _fail(
            f"tag {tag} already points at {tagged}, not at {sha}; a tag is never moved", tag
        )
    if existing is not None:
        if tagged is None:
            return _fail(f"a release for {tag} exists but the tag does not", tag)
        return VerbResult(
            VerbOutcome.OK,
            f"release {tag} already exists at {sha}",
            url=existing.html_url,
            outputs={"decision": "unchanged", "tag": tag},
        )
    url = api.create_release(
        request.owner,
        request.repo,
        tag,
        sha,
        name,
        body,
        latest=latest,
        prerelease=prerelease,
    )
    return VerbResult(
        VerbOutcome.OK,
        f"created release {tag} at {sha}",
        url=url,
        outputs={"decision": "created", "tag": tag},
    )
