# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lktec_bot.application.gate import VerbOutcome
from lktec_bot.domain.permissions import Verb
from lktec_bot.ports.github_api import BranchUpdate
from tests.fakes import FakeRepository
from tests.verb_helpers import HEAD, Harness, pull_request


def test_update_branch_sends_the_current_head_with_both_write_permissions() -> None:
    state = FakeRepository(pull_requests={7: pull_request()})
    harness = Harness(state)

    result = harness.run(Verb.UPDATE_BRANCH, {"number": 7})

    assert result.outcome is VerbOutcome.OK
    assert result.message == "update of #7 accepted; GitHub is merging the base into the branch"
    assert result.url == "https://github.com/acme/widgets/pull/7"
    assert harness.tokens.requests[1][2] == {"contents": "write", "pull_requests": "write"}
    assert state.branch_updates == [(7, HEAD)]


def test_update_branch_already_up_to_date_is_ok_with_a_message() -> None:
    state = FakeRepository(pull_requests={7: pull_request()}, branch_update=BranchUpdate.UP_TO_DATE)

    result = Harness(state).run(Verb.UPDATE_BRANCH, {"number": 7})

    assert result.ok
    assert result.message == "#7 is already up to date with its base"


def test_update_branch_refuses_drafts_and_closed_pull_requests() -> None:
    state = FakeRepository(
        pull_requests={1: pull_request(1, draft=True), 2: pull_request(2, state="closed")}
    )

    draft = Harness(state).run(Verb.UPDATE_BRANCH, {"number": 1})
    closed = Harness(state).run(Verb.UPDATE_BRANCH, {"number": 2})

    assert draft.outcome is closed.outcome is VerbOutcome.ERROR
    assert draft.message == "refusing to update: pull request #1 is a draft"
    assert closed.message == "refusing to update: pull request #2 is closed, not open"
    assert state.branch_updates == []
