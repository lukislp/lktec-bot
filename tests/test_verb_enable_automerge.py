# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from lktec_bot.application.gate import VerbOutcome
from lktec_bot.domain.permissions import Verb
from tests.fakes import FakeRepository
from tests.verb_helpers import Harness, pull_request


@pytest.mark.parametrize(("method", "graphql"), [("squash", "SQUASH"), ("merge", "MERGE")])
def test_enable_automerge_runs_the_mutation_with_contents_and_pull_requests_write(
    method: str, graphql: str
) -> None:
    state = FakeRepository(pull_requests={7: pull_request()})
    harness = Harness(state)

    result = harness.run(Verb.ENABLE_AUTOMERGE, {"number": 7, "method": method})

    assert result.outcome is VerbOutcome.OK
    assert result.message == f"auto-merge ({method}) enabled on #7"
    assert result.url == "https://github.com/acme/widgets/pull/7"
    # contents:write as well: verified live, pull_requests:write alone is refused by GitHub.
    assert harness.tokens.requests[1][2] == {"contents": "write", "pull_requests": "write"}
    assert state.auto_merge == [(7, graphql)]


def test_enable_automerge_defaults_to_squash() -> None:
    state = FakeRepository(pull_requests={7: pull_request()})

    assert Harness(state).run(Verb.ENABLE_AUTOMERGE, {"number": 7}).ok
    assert state.auto_merge == [(7, "SQUASH")]


def test_enable_automerge_is_idempotent_for_the_same_method() -> None:
    state = FakeRepository(pull_requests={7: pull_request(auto_merge="REBASE")})

    result = Harness(state).run(Verb.ENABLE_AUTOMERGE, {"number": 7, "method": "rebase"})

    assert result.ok
    assert result.message == "auto-merge (rebase) is already enabled on #7"
    assert state.auto_merge == []


def test_enable_automerge_with_another_method_already_set_is_an_error() -> None:
    state = FakeRepository(pull_requests={7: pull_request(auto_merge="MERGE")})

    result = Harness(state).run(Verb.ENABLE_AUTOMERGE, {"number": 7, "method": "squash"})

    assert result.outcome is VerbOutcome.ERROR
    assert result.message == (
        "auto-merge is already enabled on #7 with method merge, not squash; disable it first"
    )
    assert state.auto_merge == []


@pytest.mark.parametrize(
    ("kwargs", "problem"),
    [({"draft": True}, "is a draft"), ({"state": "closed"}, "is closed, not open")],
)
def test_enable_automerge_refuses_drafts_and_closed(
    kwargs: dict[str, object], problem: str
) -> None:
    state = FakeRepository(pull_requests={7: pull_request(**kwargs)})  # type: ignore[arg-type]

    result = Harness(state).run(Verb.ENABLE_AUTOMERGE, {"number": 7, "method": "squash"})

    assert result.outcome is VerbOutcome.ERROR
    assert result.message.startswith("refusing to enable auto-merge: ")
    assert problem in result.message
    assert state.auto_merge == []


def test_enable_automerge_rejects_an_unknown_method_before_any_call() -> None:
    harness = Harness(FakeRepository(pull_requests={7: pull_request()}))

    result = harness.run(Verb.ENABLE_AUTOMERGE, {"number": 7, "method": "fast-forward"})

    assert result.outcome is VerbOutcome.ERROR
    assert "squash, merge, rebase" in result.message
    assert harness.verb_api_calls == []
