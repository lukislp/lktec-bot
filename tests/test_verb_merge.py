# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from lktec_bot.application.gate import VerbOutcome, VerbResult
from lktec_bot.domain.permissions import Verb
from lktec_bot.ports.github_api import PullRequest, Review
from tests.fakes import BOT, FakeRepository
from tests.verb_helpers import HEAD, Harness, pull_request

URL = "https://github.com/acme/widgets/pull/7"


def mergeable(merge_state: str = "CLEAN", **changes: Any) -> PullRequest:
    return replace(pull_request(), merge_state=merge_state, **changes)


def approval(state: str = "APPROVED", head: str = HEAD, user: str = BOT) -> Review:
    return Review(user, state, head, f"{URL}#pullrequestreview-1")


def state_with(
    pr: PullRequest | None = None, reviews: list[Review] | None = None
) -> FakeRepository:
    return FakeRepository(
        pull_requests={7: pr or mergeable()},
        reviews={7: [approval()] if reviews is None else reviews},
    )


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waited: list[float] = []
    monkeypatch.setattr("lktec_bot.application.verbs.merge.sleep", waited.append)
    return waited


ARGS: dict[str, object] = {"number": 7, "method": "squash", "expected_head_sha": HEAD}


def run(state: FakeRepository, **args: object) -> VerbResult:
    return Harness(state).run(Verb.MERGE, {**ARGS, **args})


def assert_refused(result: VerbResult, message: str, state: FakeRepository) -> None:
    assert result.outcome is VerbOutcome.ERROR
    assert result.message == f"refusing to merge: {message}"
    assert result.outputs == {"decision": "refused"}
    assert state.merges == []


def test_merge_sends_sha_and_method_with_exactly_the_three_grants() -> None:
    state = state_with()
    harness = Harness(state)

    result = harness.run(Verb.MERGE, {**ARGS, "method": "rebase"})

    assert result.outcome is VerbOutcome.OK
    assert result.message == f"merged #7 (rebase) at {HEAD[:12]}"
    assert result.url == URL
    assert result.outputs == {"decision": "merged", "sha": "m" * 40}
    assert state.merges == [(7, "REBASE", HEAD)]
    assert harness.tokens.requests[1][2] == {
        "contents": "write",
        "pull_requests": "write",
        "workflows": "write",
    }


def test_merge_defaults_to_squash() -> None:
    state = state_with()
    result = Harness(state).run(Verb.MERGE, {"number": 7, "expected_head_sha": HEAD})

    assert result.ok
    assert state.merges == [(7, "SQUASH", HEAD)]


def test_merge_rejects_an_unknown_method_and_a_missing_head() -> None:
    state = state_with()

    bad = run(state, method="fast-forward")
    missing = run(state, expected_head_sha="")

    assert bad.outcome is missing.outcome is VerbOutcome.ERROR
    assert "--method must be one of squash, merge, rebase" in bad.message
    assert missing.message == "--expected-head-sha is required"
    assert state.merges == []


def test_merge_refuses_a_closed_pull_request_and_a_draft() -> None:
    closed = state_with(mergeable(state="closed"))
    draft = state_with(mergeable(draft=True))

    assert_refused(run(closed), "pull request #7 is closed, not open", closed)
    assert_refused(run(draft), "pull request #7 is a draft", draft)


def test_merge_refuses_when_the_head_moved() -> None:
    state = state_with(mergeable(head_sha="f" * 40))

    assert_refused(run(state), f"#7 head is {'f' * 12}, not the expected {HEAD[:12]}", state)


@pytest.mark.parametrize("merge_state", ["UNSTABLE", "BLOCKED", "BEHIND", "DIRTY", "HAS_HOOKS"])
def test_merge_refuses_every_state_but_clean(merge_state: str) -> None:
    state = state_with(mergeable(merge_state=merge_state))

    assert_refused(
        run(state), f"#7 is not mergeable right now (state: {merge_state.lower()})", state
    )


def test_merge_refuses_a_state_github_did_not_report() -> None:
    state = state_with(mergeable(merge_state=""))

    assert_refused(run(state), "#7 is not mergeable right now (state: not reported)", state)


def test_merge_reads_unknown_again_until_github_has_computed_the_state(
    sleeps: list[float],
) -> None:
    state = state_with(mergeable(merge_state="UNKNOWN"))
    state.merge_state_script[7] = ["UNKNOWN", "CLEAN"]

    result = run(state)

    assert result.ok
    assert len(sleeps) == 1
    assert state.merges == [(7, "SQUASH", HEAD)]


def test_merge_gives_up_on_a_state_that_stays_unknown_after_three_reads(
    sleeps: list[float],
) -> None:
    state = state_with(mergeable(merge_state="UNKNOWN"))
    harness = Harness(state)

    result = harness.run(Verb.MERGE, ARGS)

    assert_refused(result, "#7 is not mergeable right now (state: unknown)", state)
    assert len(sleeps) == 2
    assert harness.verb_api_calls.count("get_pull_request") == 3


@pytest.mark.parametrize(
    "reviews",
    [
        [],
        [approval(user="someone")],
        [approval(head="e" * 40)],
        [approval("CHANGES_REQUESTED")],
        [approval(), approval("DISMISSED")],
    ],
    ids=["none", "other-user", "other-head", "changes-requested", "latest-not-approval"],
)
def test_merge_refuses_without_an_approval_of_the_app_on_the_head(reviews: list[Review]) -> None:
    state = state_with(reviews=reviews)

    assert_refused(run(state), f"the App has not approved #7 at {HEAD[:12]}", state)


def test_merge_takes_the_latest_review_of_the_app() -> None:
    state = state_with(reviews=[approval("CHANGES_REQUESTED"), approval()])

    assert run(state).ok


def test_merge_without_a_known_actor_cannot_verify_the_approval() -> None:
    state = state_with()
    result = Harness(state, actor=None).run(Verb.MERGE, ARGS)

    assert_refused(result, f"the App has not approved #7 at {HEAD[:12]}", state)


@pytest.mark.parametrize("status", ["405", "409"])
def test_merge_turns_a_github_refusal_into_a_refusal_with_its_message(status: str) -> None:
    state = state_with()
    state.merge_refusals[7] = f"PUT pulls/7/merge -> {status}: Head branch was modified"

    result = run(state)

    assert result.outcome is VerbOutcome.ERROR
    assert result.message == (
        f"refusing to merge: GitHub says: PUT pulls/7/merge -> {status}: Head branch was modified"
    )
    assert result.outputs == {"decision": "refused"}
