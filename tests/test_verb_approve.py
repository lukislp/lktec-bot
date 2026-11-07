# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lktec_bot.application.gate import VerbOutcome
from lktec_bot.domain.permissions import Verb
from lktec_bot.ports.github_api import Review
from tests.fakes import BOT, FakeRepository
from tests.verb_helpers import HEAD, Harness, pull_request


def review(state: str = "APPROVED", *, login: str = BOT, commit: str = HEAD) -> Review:
    return Review(
        user_login=login,
        state=state,
        commit_id=commit,
        html_url="https://github.com/acme/widgets/pull/7#pullrequestreview-5",
    )


def test_approve_submits_an_approving_review_with_pull_requests_write() -> None:
    state = FakeRepository(pull_requests={7: pull_request()})
    harness = Harness(state)

    result = harness.run(Verb.APPROVE, {"number": 7, "body": "lgtm"})

    assert result.outcome is VerbOutcome.OK
    assert result.message == f"approved #7 at {HEAD[:12]}"
    assert result.url == "https://github.com/acme/widgets/pull/7#pullrequestreview-1"
    assert harness.tokens.requests[1][2] == {"pull_requests": "write"}
    assert state.reviews_created == [(7, "APPROVE", "lgtm")]
    assert harness.verb_api_calls == ["get_pull_request", "list_reviews", "create_review"]


def test_approve_without_a_body_sends_an_empty_body() -> None:
    state = FakeRepository(pull_requests={7: pull_request()})

    assert Harness(state).run(Verb.APPROVE, {"number": 7}).ok
    assert state.reviews_created == [(7, "APPROVE", "")]


def test_approve_is_idempotent_per_head_commit() -> None:
    state = FakeRepository(pull_requests={7: pull_request()}, reviews={7: [review()]})
    harness = Harness(state)

    result = harness.run(Verb.APPROVE, {"number": 7})

    assert result.ok
    assert result.message == f"#7 is already approved at {HEAD[:12]}"
    assert result.url == "https://github.com/acme/widgets/pull/7#pullrequestreview-5"
    assert state.reviews_created == []


def test_approve_reviews_again_after_a_new_head_or_by_others() -> None:
    stale = review(commit="f" * 40)
    someone_else = review(login="alice")
    dismissed = review("DISMISSED")
    state = FakeRepository(
        pull_requests={7: pull_request()}, reviews={7: [stale, someone_else, dismissed]}
    )

    assert Harness(state).run(Verb.APPROVE, {"number": 7}).ok
    assert state.reviews_created == [(7, "APPROVE", "")]


def test_approve_without_a_known_actor_skips_the_idempotency_check() -> None:
    state = FakeRepository(pull_requests={7: pull_request()}, reviews={7: [review()]})
    harness = Harness(state, actor=None)

    assert harness.run(Verb.APPROVE, {"number": 7}).ok
    assert state.reviews_created == [(7, "APPROVE", "")]
    assert "list_reviews" not in harness.verb_api_calls


def test_approve_refuses_a_draft() -> None:
    state = FakeRepository(pull_requests={7: pull_request(draft=True)})
    harness = Harness(state)

    result = harness.run(Verb.APPROVE, {"number": 7})

    assert result.outcome is VerbOutcome.ERROR
    assert result.message == "refusing to approve: pull request #7 is a draft"
    assert result.url == "https://github.com/acme/widgets/pull/7"
    assert state.reviews_created == []


def test_approve_refuses_a_closed_pull_request() -> None:
    state = FakeRepository(pull_requests={7: pull_request(state="closed")})

    result = Harness(state).run(Verb.APPROVE, {"number": 7})

    assert result.outcome is VerbOutcome.ERROR
    assert result.message == "refusing to approve: pull request #7 is closed, not open"
    assert state.reviews_created == []


def test_approve_reports_an_unknown_pull_request_as_an_error() -> None:
    result = Harness().run(Verb.APPROVE, {"number": 404})

    assert result.outcome is VerbOutcome.ERROR
    assert result.message == "approve failed: GET pulls/404 -> 404: Not Found"


def test_approve_is_denied_for_a_module_that_may_not_run_it() -> None:
    harness = Harness(FakeRepository(pull_requests={7: pull_request()}))

    result = harness.run(Verb.APPROVE, {"number": 7}, module="ping")

    assert result.outcome is VerbOutcome.DENIED
    assert len(harness.tokens.minted) == 1
