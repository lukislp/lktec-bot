# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""``ping``: the no-op verb. It proves the whole gate path (policy, token, revoke) end to end
without changing anything in the target repository."""

from __future__ import annotations

from lktec_bot.application.gate import VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.verbs import register
from lktec_bot.domain.permissions import Grant, Permission, Verb
from lktec_bot.ports.github_api import GitHubApi


@register(Verb.PING, grants=frozenset({Grant(Permission.METADATA, "read")}))
def ping(api: GitHubApi, request: VerbRequest) -> VerbResult:
    # ``api`` is bound to a token narrowed to metadata:read; minting it is the whole point.
    del api
    return VerbResult(VerbOutcome.OK, f"pong from {request.repository}")
