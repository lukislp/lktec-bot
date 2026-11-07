# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The verb table: every verb maps to the GitHub App permissions it needs, and nothing more.

The installation token minted for a verb is narrowed to exactly the grants listed here. The
table is deliberately complete for every planned verb, even though only ``ping`` ships today,
so the permission footprint of the whole project is visible in one place and reviewed as a
unit. The ``workflows`` permission does not appear and must never be added: it would let a
verb change the CI definitions that gate the bot's own actions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

Access = Literal["read", "write"]


class Permission(StrEnum):
    """GitHub App permission names as used in the installation access token request."""

    METADATA = "metadata"
    CONTENTS = "contents"
    PULL_REQUESTS = "pull_requests"
    ISSUES = "issues"
    CHECKS = "checks"
    DEPLOYMENTS = "deployments"


@dataclass(frozen=True, slots=True)
class Grant:
    """One permission at one access level."""

    permission: Permission
    access: Access

    def __str__(self) -> str:
        return f"{self.permission.value}:{self.access}"


class Verb(StrEnum):
    """Every verb the core knows about. Only ``ping`` has a handler in this release."""

    PING = "ping"
    COMMENT = "comment"
    APPROVE = "approve"
    ENABLE_AUTOMERGE = "enable-automerge"
    UPDATE_BRANCH = "update-branch"
    COMMIT = "commit"
    OPEN_PR = "open-pr"
    RELEASE = "release"
    CHECK = "check"
    OPEN_ISSUE = "open-issue"
    DEPLOYMENT = "deployment"


class Target(StrEnum):
    """What a verb acts on, for the verbs whose permission depends on it."""

    PULL_REQUEST = "pull_request"
    ISSUE = "issue"


def _grants(*pairs: tuple[Permission, Access]) -> frozenset[Grant]:
    return frozenset(Grant(permission, access) for permission, access in pairs)


# Verbs whose permission does not depend on the target.
_VERB_PERMISSIONS: Mapping[Verb, frozenset[Grant]] = {
    # The installation token always carries metadata:read; requesting it explicitly keeps the
    # request body non-empty, which is what makes GitHub narrow the token at all.
    Verb.PING: _grants((Permission.METADATA, "read")),
    Verb.APPROVE: _grants((Permission.PULL_REQUESTS, "write")),
    # Verified live (lukislp/lktec-bot#4): with pull_requests:write alone GitHub answers the
    # enablePullRequestAutoMerge mutation with "Resource not accessible by integration";
    # contents:write is required as well, as for the merge the mutation schedules.
    Verb.ENABLE_AUTOMERGE: _grants(
        (Permission.CONTENTS, "write"),
        (Permission.PULL_REQUESTS, "write"),
    ),
    Verb.UPDATE_BRANCH: _grants(
        (Permission.CONTENTS, "write"),
        (Permission.PULL_REQUESTS, "write"),
    ),
    Verb.COMMIT: _grants((Permission.CONTENTS, "write")),
    Verb.OPEN_PR: _grants((Permission.PULL_REQUESTS, "write")),
    Verb.RELEASE: _grants((Permission.CONTENTS, "write")),
    Verb.CHECK: _grants((Permission.CHECKS, "write")),
    Verb.OPEN_ISSUE: _grants((Permission.ISSUES, "write")),
    Verb.DEPLOYMENT: _grants((Permission.DEPLOYMENTS, "write")),
}

# ``comment`` is the one verb whose permission depends on what it comments on.
_COMMENT_PERMISSIONS: Mapping[Target, frozenset[Grant]] = {
    Target.PULL_REQUEST: _grants((Permission.PULL_REQUESTS, "write")),
    Target.ISSUE: _grants((Permission.ISSUES, "write")),
}


def permissions_for(verb: Verb, target: Target | None = None) -> frozenset[Grant]:
    """Return the exact grants a verb needs.

    ``target`` is required for :attr:`Verb.COMMENT` and ignored for every other verb.
    """
    if verb is Verb.COMMENT:
        if target is None:
            raise ValueError("the comment verb needs a target (pull_request or issue)")
        return _COMMENT_PERMISSIONS[target]
    return _VERB_PERMISSIONS[verb]


def as_token_request(grants: frozenset[Grant]) -> dict[str, str]:
    """Render grants as the ``permissions`` object of an installation token request."""
    return {grant.permission.value: grant.access for grant in sorted(grants, key=str)}
