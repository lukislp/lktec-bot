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


def test_enable_automerge_reports_the_decision_enabled(verb_sleeps: list[float]) -> None:
    state = FakeRepository(pull_requests={7: pull_request()})

    result = Harness(state).run(Verb.ENABLE_AUTOMERGE, {"number": 7})

    assert result.outputs == {"decision": "enabled"}
    assert verb_sleeps == []


def test_enable_automerge_retries_while_github_says_the_pull_request_is_mergeable(
    verb_sleeps: list[float],
) -> None:
    state = FakeRepository(
        pull_requests={7: pull_request()}, auto_merge_refusals={7: ["clean", "unstable"]}
    )

    result = Harness(state).run(Verb.ENABLE_AUTOMERGE, {"number": 7})

    assert result.outcome is VerbOutcome.OK
    assert result.message == "auto-merge (squash) enabled on #7"
    assert result.outputs == {"decision": "enabled"}
    assert verb_sleeps == [10.0, 10.0]
    assert state.auto_merge == [(7, "SQUASH")]


def test_enable_automerge_gives_up_after_six_refusals_without_an_error(
    verb_sleeps: list[float],
) -> None:
    state = FakeRepository(
        pull_requests={7: pull_request()}, auto_merge_refusals={7: ["clean"] * 6}
    )

    result = Harness(state).run(Verb.ENABLE_AUTOMERGE, {"number": 7})

    assert result.outcome is VerbOutcome.OK
    assert result.message == (
        "#7 is already mergeable (clean): GitHub does not enable auto-merge on a pull request "
        "that can be merged now; it needs a person to merge it"
    )
    assert result.outputs == {"decision": "ready", "state": "clean"}
    assert result.url == "https://github.com/acme/widgets/pull/7"
    assert verb_sleeps == [10.0] * 5
    assert state.auto_merge == []


def test_enable_automerge_defaults_to_squash() -> None:
    state = FakeRepository(pull_requests={7: pull_request()})

    assert Harness(state).run(Verb.ENABLE_AUTOMERGE, {"number": 7}).ok
    assert state.auto_merge == [(7, "SQUASH")]


def test_enable_automerge_is_idempotent_for_the_same_method() -> None:
    state = FakeRepository(pull_requests={7: pull_request(auto_merge="REBASE")})

    result = Harness(state).run(Verb.ENABLE_AUTOMERGE, {"number": 7, "method": "rebase"})

    assert result.ok
    assert result.message == "auto-merge (rebase) is already enabled on #7"
    assert result.outputs == {"decision": "unchanged"}
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
