# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Shared setup for the per-verb gate tests: a gate wired to the fakes with the automerge
module enabled, and a pull request to act on."""

from __future__ import annotations

from collections.abc import Mapping

from lktec_bot.application.gate import Gate, VerbRequest, VerbResult
from lktec_bot.application.verbs import handlers
from lktec_bot.domain.permissions import Target, Verb, as_token_request, permissions_for
from lktec_bot.domain.policy import POLICY_PATH
from lktec_bot.ports.github_api import PullRequest
from tests.fakes import FakeApiFactory, FakeRepository, FakeTokenProvider

AUTOMERGE = "version: 1\nmodules:\n  automerge:\n    enabled: true\n"
LOCKFILES = "version: 1\nmodules:\n  lockfiles:\n    enabled: true\n"
HEAD = "0123456789abcdef0123456789abcdef01234567"


def pull_request(
    number: int = 7, *, state: str = "open", draft: bool = False, auto_merge: str | None = None
) -> PullRequest:
    return PullRequest(
        number=number,
        html_url=f"https://github.com/acme/widgets/pull/{number}",
        state=state,
        draft=draft,
        head_sha=HEAD,
        auto_merge_method=auto_merge,  # type: ignore[arg-type]
    )


class Harness:
    """A gate plus the fake state behind it, for one verb."""

    def __init__(
        self,
        state: FakeRepository | None = None,
        actor: str | None = "fake-app[bot]",
        policy: str = AUTOMERGE,
    ):
        self.state = state if state is not None else FakeRepository()
        self.tokens = FakeTokenProvider(actor=actor)
        self.factory = FakeApiFactory({POLICY_PATH: policy}, self.state)
        self.gate = Gate(self.tokens, self.factory, handlers())

    def run(
        self,
        verb: Verb,
        args: Mapping[str, object],
        *,
        target: Target | None = None,
        module: str = "automerge",
    ) -> VerbResult:
        request = VerbRequest("acme", "widgets", module, verb, target=target, args=dict(args))
        result = self.gate.run(request)
        # The verb token is always the second one and always exactly the verb's permission.
        if len(self.tokens.requests) == 2:
            assert self.tokens.requests[1][2] == as_token_request(permissions_for(verb, target))
        assert all(token.revoked for token in self.tokens.minted)
        return result

    @property
    def verb_api_calls(self) -> list[str]:
        """Names of the calls the verb made with its own token (the policy read is excluded)."""
        return [name for name, _ in self.factory.created[1].calls]
