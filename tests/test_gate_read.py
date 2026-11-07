# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""``Gate.read``: the read-only path a module command uses, under the gate's rules."""

from __future__ import annotations

import pytest

from lktec_bot.application.gate import Gate, VerbOutcome
from lktec_bot.application.verbs import handlers
from lktec_bot.domain.policy import POLICY_PATH, Policy
from lktec_bot.ports.github_api import GitHubApi
from lktec_bot.ports.token_provider import TokenError
from tests.fakes import FakeApiFactory, FakeToken, FakeTokenProvider

ENABLED = "version: 1\nmodules:\n  automerge:\n    enabled: true\n    majors: auto\n"
READ = {"pull_requests": "read"}


def reader(api: GitHubApi, policy: Policy) -> str:
    return str(policy.modules["automerge"].options)


def make(
    text: str | None = ENABLED,
    tokens: FakeTokenProvider | None = None,
    caller_token: str | None = None,
) -> tuple[Gate, FakeTokenProvider, FakeApiFactory]:
    provider = tokens or FakeTokenProvider()
    factory = FakeApiFactory({} if text is None else {POLICY_PATH: text})
    return Gate(provider, factory, handlers(), caller_token=caller_token), provider, factory


def test_read_hands_the_reader_a_narrowed_token_and_the_policy() -> None:
    gate, tokens, factory = make()

    result = gate.read("acme", "widgets", "automerge", READ, reader)

    assert result.failure is None
    assert result.value == "{'majors': 'auto'}"
    assert [request[2] for request in tokens.requests] == [{"contents": "read"}, READ]
    assert all(token.revoked for token in tokens.minted)
    assert factory.created[-1].token == "tok-1"
    assert factory.created[-1].closed


def test_read_refuses_an_unknown_module_before_anything_is_minted() -> None:
    gate, tokens, _ = make()

    result = gate.read("acme", "widgets", "mystery", READ, reader)

    assert result.failure is not None
    assert result.failure.outcome is VerbOutcome.DENIED
    assert "module 'mystery' is not known" in result.failure.message
    assert tokens.requests == []


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        (None, "is missing"),
        ("version: 1\nmodules:\n  automerge:\n    typo: 1\n", "is invalid"),
        ("version: 1\nmodules:\n  ping:\n    enabled: true\n", "not declared"),
        ("version: 1\nmodules:\n  automerge:\n    enabled: false\n", "is disabled"),
    ],
)
def test_read_is_denied_by_the_policy(text: str | None, needle: str) -> None:
    gate, tokens, _ = make(text)

    result = gate.read("acme", "widgets", "automerge", READ, reader)

    assert result.failure is not None
    assert result.failure.outcome is VerbOutcome.DENIED
    assert needle in result.failure.message
    assert [request[2] for request in tokens.requests] == [{"contents": "read"}]


def test_read_reports_a_policy_token_error() -> None:
    gate, _, _ = make(tokens=FakeTokenProvider(fail_with=TokenError("not installed")))

    result = gate.read("acme", "widgets", "automerge", READ, reader)

    assert result.failure is not None
    assert result.failure.outcome is VerbOutcome.ERROR
    assert result.failure.message == "could not mint the policy token: not installed"


class FailsOnTheSecondMint(FakeTokenProvider):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def token_for(self, owner: str, repo: str, permissions: dict[str, str]) -> FakeToken:  # type: ignore[override]
        if self.minted:
            raise self.error
        return super().token_for(owner, repo, permissions)


def test_read_reports_a_read_token_error() -> None:
    gate, _, _ = make(tokens=FailsOnTheSecondMint(TokenError("rate limited")))

    result = gate.read("acme", "widgets", "automerge", READ, reader)

    assert result.failure is not None
    assert result.failure.message == "could not mint a token: rate limited"


def test_read_reports_an_unexpected_provider_bug() -> None:
    gate, _, _ = make(tokens=FailsOnTheSecondMint(KeyError("boom")))

    result = gate.read("acme", "widgets", "automerge", READ, reader)

    assert result.failure is not None
    assert result.failure.message.startswith("unexpected error while minting a token (KeyError)")


def test_read_refuses_a_token_broader_than_asked_and_revokes_it() -> None:
    broad = {"metadata": "read", "pull_requests": "read", "contents": "write"}
    gate, tokens, _ = make(tokens=FakeTokenProvider(permissions_by_mint={1: broad}))

    result = gate.read("acme", "widgets", "automerge", READ, reader)

    assert result.failure is not None
    assert result.failure.message.startswith("read token refused: it carries contents:write")
    assert all(token.revoked for token in tokens.minted)


def test_read_turns_a_failing_reader_into_an_error_and_still_revokes() -> None:
    gate, tokens, factory = make()

    def broken(api: GitHubApi, policy: Policy) -> str:
        raise RuntimeError("GET pulls -> 502")

    result = gate.read("acme", "widgets", "automerge", READ, broken)

    assert result.failure is not None
    assert result.failure.message == "automerge could not read: GET pulls -> 502"
    assert all(token.revoked for token in tokens.minted)
    assert factory.created[-1].closed


def test_read_reports_a_revoke_that_failed_as_a_warning() -> None:
    gate, _, _ = make(tokens=FakeTokenProvider(failing_revokes=5))

    result = gate.read("acme", "widgets", "automerge", READ, reader)

    assert result.value is not None
    assert len(result.warnings) == 2
    assert all(warning.startswith("token not revoked") for warning in result.warnings)


def test_read_validates_owner_and_repository_names() -> None:
    gate, _, _ = make()

    with pytest.raises(ValueError, match="owner"):
        gate.read("..", "widgets", "automerge", READ, reader)
    with pytest.raises(ValueError, match="repository name"):
        gate.read("acme", "a b", "automerge", READ, reader)


def test_read_with_a_caller_token_reads_the_policy_without_minting() -> None:
    gate, tokens, factory = make(caller_token="caller")

    result = gate.read("acme", "widgets", "automerge", READ, reader)

    assert result.value is not None
    assert [request[2] for request in tokens.requests] == [READ]
    assert factory.created[0].token == "caller"
