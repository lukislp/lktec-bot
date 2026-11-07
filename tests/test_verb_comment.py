# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from lktec_bot.application.gate import VerbOutcome
from lktec_bot.application.verbs.comment import marker_tag
from lktec_bot.domain.permissions import Target, Verb
from lktec_bot.ports.github_api import IssueComment
from tests.fakes import BOT, FakeRepository
from tests.verb_helpers import Harness

TAG = "<!-- lktec-bot:e2e -->"


def existing(body: str, *, login: str = BOT, comment_id: int = 42) -> IssueComment:
    return IssueComment(
        id=comment_id,
        user_login=login,
        user_type="Bot" if login.endswith("[bot]") else "User",
        body=body,
        html_url=f"https://github.com/acme/widgets/issues/7#issuecomment-{comment_id}",
    )


def test_comment_on_a_pull_request_mints_pull_requests_write_only() -> None:
    harness = Harness()

    result = harness.run(Verb.COMMENT, {"number": 7, "body": "hello"}, target=Target.PULL_REQUEST)

    assert result.outcome is VerbOutcome.OK
    assert result.message == "commented on #7"
    assert result.url == "https://github.com/acme/widgets/issues/7#issuecomment-100"
    assert harness.tokens.requests[1][2] == {"pull_requests": "write"}
    assert harness.state.created_comments == [(7, "hello")]


def test_comment_on_an_issue_mints_issues_write_only() -> None:
    harness = Harness()

    result = harness.run(Verb.COMMENT, {"number": "3", "body": "hi"}, target=Target.ISSUE)

    assert result.ok
    assert harness.tokens.requests[1][2] == {"issues": "write"}
    assert harness.state.created_comments == [(3, "hi")]


def test_marker_creates_a_tagged_comment_when_none_exists() -> None:
    harness = Harness()

    result = harness.run(
        Verb.COMMENT,
        {"number": 7, "body": "run 1\n", "marker": "e2e"},
        target=Target.PULL_REQUEST,
    )

    assert result.ok
    assert result.message == "commented on #7 with marker 'e2e'"
    assert harness.state.created_comments == [(7, f"run 1\n\n{TAG}\n")]
    assert harness.verb_api_calls == ["list_issue_comments", "create_issue_comment"]


def test_marker_updates_the_bots_own_comment_instead_of_adding_one() -> None:
    state = FakeRepository(comments={7: [existing(f"run 1\n\n{TAG}\n")]})
    harness = Harness(state)

    result = harness.run(
        Verb.COMMENT,
        {"number": 7, "body": "run 2", "marker": "e2e"},
        target=Target.PULL_REQUEST,
    )

    assert result.ok
    assert result.message == "updated the comment on #7 with marker 'e2e'"
    assert result.url == "https://github.com/acme/widgets/issues/comments/42"
    assert state.updated_comments == [(42, f"run 2\n\n{TAG}\n")]
    assert state.created_comments == []


def test_marker_leaves_an_unchanged_comment_alone() -> None:
    state = FakeRepository(comments={7: [existing(f"same\n\n{TAG}\n")]})
    harness = Harness(state)

    result = harness.run(
        Verb.COMMENT, {"number": 7, "body": "same", "marker": "e2e"}, target=Target.PULL_REQUEST
    )

    assert result.ok
    assert "already up to date" in result.message
    assert result.url == "https://github.com/acme/widgets/issues/7#issuecomment-42"
    assert state.updated_comments == []
    assert state.created_comments == []


def test_marker_never_edits_someone_elses_comment_carrying_the_tag() -> None:
    hijack = existing(f"look at me\n\n{TAG}\n", login="mallory", comment_id=9)
    other_marker = existing("<!-- lktec-bot:other -->", comment_id=10)
    state = FakeRepository(comments={7: [hijack, other_marker]})
    harness = Harness(state)

    result = harness.run(
        Verb.COMMENT, {"number": 7, "body": "x", "marker": "e2e"}, target=Target.PULL_REQUEST
    )

    assert result.ok
    assert state.updated_comments == []
    assert state.created_comments == [(7, f"x\n\n{TAG}\n")]


def test_marker_without_a_known_actor_fails_closed() -> None:
    harness = Harness(actor=None)

    result = harness.run(
        Verb.COMMENT, {"number": 7, "body": "x", "marker": "e2e"}, target=Target.PULL_REQUEST
    )

    assert result.outcome is VerbOutcome.ERROR
    assert "did not report the bot's login" in result.message
    assert harness.state.created_comments == []


def test_empty_body_is_an_error_before_any_call() -> None:
    harness = Harness()

    result = harness.run(Verb.COMMENT, {"number": 7, "body": "  \n"}, target=Target.PULL_REQUEST)

    assert result.outcome is VerbOutcome.ERROR
    assert result.message == "the comment body is empty"
    assert harness.verb_api_calls == []


@pytest.mark.parametrize("marker", ["", None])
def test_blank_marker_means_no_marker(marker: str | None) -> None:
    harness = Harness()

    result = harness.run(
        Verb.COMMENT, {"number": 7, "body": "x", "marker": marker}, target=Target.PULL_REQUEST
    )

    assert result.ok
    assert harness.state.created_comments == [(7, "x")]


@pytest.mark.parametrize("marker", ["-->", "a b", "x" * 65, ".hidden", "<script>"])
def test_bad_marker_is_an_error(marker: str) -> None:
    harness = Harness()

    result = harness.run(
        Verb.COMMENT,
        {"number": 7, "body": "x", "marker": marker},
        target=Target.PULL_REQUEST,
    )

    assert result.outcome is VerbOutcome.ERROR
    assert "--marker" in result.message
    assert harness.state.created_comments == []


def test_marker_tag_is_an_html_comment() -> None:
    assert marker_tag("e2e") == TAG
    assert marker_tag("release.v1_2-rc") == "<!-- lktec-bot:release.v1_2-rc -->"


@pytest.mark.parametrize("number", [0, -1, "abc", None, True, 1.5])
def test_bad_number_is_an_error(number: object) -> None:
    harness = Harness()

    result = harness.run(Verb.COMMENT, {"number": number, "body": "x"}, target=Target.PULL_REQUEST)

    assert result.outcome is VerbOutcome.ERROR
    assert "--number" in result.message
