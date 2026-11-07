# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from lktec_bot.application.gate import VerbOutcome, VerbResult
from lktec_bot.application.verbs.comment import marker_tag
from lktec_bot.domain.permissions import Verb
from lktec_bot.ports.github_api import Issue
from tests.fakes import BOT, FakeRepository
from tests.verb_helpers import LOCKFILES, Harness

TAG = marker_tag("deploy")
URL = "https://github.com/acme/widgets/issues"


def issue(
    number: int = 5,
    *,
    title: str = "Deploy bump failed",
    body: str = f"image tag not found\n\n{TAG}\n",
    state: str = "open",
    labels: tuple[str, ...] = ("ci",),
    author: str = BOT,
) -> Issue:
    return Issue(number, title, body, state, f"{URL}/{number}", labels, author)


def run(harness: Harness, **args: object) -> VerbResult:
    arguments: dict[str, object] = {"title": "Deploy bump failed", "body": "image tag not found"}
    arguments.update(args)
    return harness.run(Verb.OPEN_ISSUE, arguments, module="lockfiles")


def harness_with(*issues: Issue, **state: object) -> Harness:
    repository = FakeRepository(issues={entry.number: entry for entry in issues}, **state)  # type: ignore[arg-type]
    return Harness(repository, policy=LOCKFILES)


def test_without_a_marker_an_issue_is_opened_with_the_plain_body() -> None:
    harness = Harness(policy=LOCKFILES)

    result = run(harness, label=["ci", "ci", " bug "])

    assert result.outcome is VerbOutcome.OK
    assert result.message == "opened #1"
    assert result.url == f"{URL}/1"
    assert result.outputs == {"decision": "opened", "number": "1"}
    assert harness.tokens.requests[1][2] == {"issues": "write"}
    assert harness.state.issue_writes == [
        (
            "create",
            1,
            {"title": "Deploy bump failed", "body": "image tag not found", "labels": ["ci", "bug"]},
        )
    ]
    assert harness.verb_api_calls == ["create_issue"]


def test_without_a_marker_every_call_opens_another_issue() -> None:
    harness = harness_with(issue())

    result = run(harness)

    assert result.outputs["number"] == "6"
    assert harness.verb_api_calls == ["create_issue"]


@pytest.mark.parametrize("marker", ["", None])
def test_a_blank_marker_means_no_marker(marker: str | None) -> None:
    harness = Harness(policy=LOCKFILES)

    result = run(harness, marker=marker)

    assert result.outputs["decision"] == "opened"
    assert harness.verb_api_calls == ["create_issue"]
    assert harness.state.issues[1].body == "image tag not found"


def test_a_marker_opens_a_tagged_issue_when_none_exists() -> None:
    harness = harness_with(issue(7, body="no tag here"), issue(8, body=TAG, author="mallory"))

    result = run(harness, marker="deploy", label=["ci"])

    assert result.ok
    assert result.message == "opened #9 with marker 'deploy'"
    assert result.outputs == {"decision": "opened", "number": "9"}
    assert harness.state.issues[9].body == f"image tag not found\n\n{TAG}\n"
    assert harness.state.issues[9].labels == ("ci",)
    assert harness.verb_api_calls == ["list_issues", "create_issue"]
    assert harness.factory.created[1].calls[0] == ("list_issues", ("acme", "widgets", BOT, "all"))


def test_an_identical_open_issue_is_left_alone() -> None:
    harness = harness_with(issue())

    result = run(harness, marker="deploy", label=["CI"])

    assert result.ok
    assert result.message == "#5 with marker 'deploy' is already up to date"
    assert result.url == f"{URL}/5"
    assert result.outputs == {"decision": "unchanged", "number": "5"}
    assert harness.state.issue_writes == []
    assert harness.verb_api_calls == ["list_issues"]


@pytest.mark.parametrize(
    ("args", "changed", "expected"),
    [
        ({"title": "New title"}, "title", {"title": "New title"}),
        (
            {"body": "different text"},
            "body",
            {"body": f"different text\n\n{TAG}\n"},
        ),
        ({"label": ["ci", "e2e-check"]}, "labels", {"labels": ["ci", "maintainer", "e2e-check"]}),
        (
            {"title": "T", "body": "B", "label": ["extra"]},
            "title, body, labels",
            {"title": "T", "body": f"B\n\n{TAG}\n", "labels": ["ci", "maintainer", "extra"]},
        ),
    ],
)
def test_an_open_issue_gets_only_the_fields_that_differ(
    args: dict[str, object], changed: str, expected: dict[str, object]
) -> None:
    harness = harness_with(issue(labels=("ci", "maintainer")))

    result = run(harness, marker="deploy", **({"label": ["ci"]} | args))

    assert result.ok
    assert result.message == f"updated #5 ({changed})"
    assert result.outputs == {"decision": "updated", "number": "5"}
    assert harness.state.issue_writes == [("update", 5, expected)]
    assert harness.verb_api_calls == ["list_issues", "update_issue"]


def test_a_label_the_maintainer_added_is_kept() -> None:
    harness = harness_with(issue(labels=("ci", "triaged")))

    result = run(harness, marker="deploy", label=["ci"])

    assert result.outputs["decision"] == "unchanged"
    assert harness.state.issues[5].labels == ("ci", "triaged")


def test_a_closed_issue_is_reopened_even_when_nothing_else_differs() -> None:
    harness = harness_with(issue(state="closed"))

    result = run(harness, marker="deploy", label=["ci"])

    assert result.ok
    assert result.message == "reopened #5"
    assert result.outputs == {"decision": "reopened", "number": "5"}
    assert harness.state.issue_writes == [("update", 5, {"state": "open"})]
    assert harness.state.issues[5].state == "open"


def test_a_closed_issue_is_reopened_and_updated_in_one_write() -> None:
    harness = harness_with(issue(state="closed"))

    result = run(harness, marker="deploy", title="New title", label=["ci", "more"])

    assert result.message == "reopened #5 (title, labels)"
    assert harness.state.issue_writes == [
        ("update", 5, {"title": "New title", "state": "open", "labels": ["ci", "more"]})
    ]


def test_no_reopen_opens_a_new_issue_next_to_the_closed_one() -> None:
    harness = harness_with(issue(state="closed"))

    result = run(harness, marker="deploy", reopen=False)

    assert result.ok
    assert result.message == "opened #6 with marker 'deploy'; #5 is closed"
    assert result.outputs == {"decision": "opened", "number": "6"}
    assert harness.state.issues[5].state == "closed"
    assert harness.state.issues[6].body == f"image tag not found\n\n{TAG}\n"


def test_no_reopen_still_updates_an_open_issue() -> None:
    harness = harness_with(issue())

    result = run(harness, marker="deploy", reopen=False, title="New title", label=["ci"])

    assert result.outputs["decision"] == "updated"


def test_several_matches_update_the_oldest_and_mention_the_others() -> None:
    harness = harness_with(issue(9), issue(3, body=f"old\n\n{TAG}\n"), issue(12))

    result = run(harness, marker="deploy", title="New title", label=["ci"])

    assert result.message == "updated #3 (title, body); also carrying marker 'deploy': #9, #12"
    assert result.outputs["number"] == "3"
    assert [write[1] for write in harness.state.issue_writes] == [3]


def test_several_matches_with_nothing_to_change_say_so() -> None:
    harness = harness_with(issue(9), issue(3), issue(12, state="closed"))

    result = run(harness, marker="deploy", label=["ci"])

    assert result.message == (
        "#3 with marker 'deploy' is already up to date; also carrying marker 'deploy': #9, #12"
    )


def test_an_issue_by_someone_else_carrying_the_tag_is_never_touched() -> None:
    hijack = issue(4, author="mallory", title="Hijack")
    harness = harness_with(hijack, list_ignores_creator=True)

    result = run(harness, marker="deploy")

    assert result.message == "opened #5 with marker 'deploy'"
    assert harness.state.issues[4] == hijack
    assert [write[0] for write in harness.state.issue_writes] == ["create"]


def test_a_marker_without_a_known_actor_fails_closed() -> None:
    harness = Harness(actor=None, policy=LOCKFILES)

    result = run(harness, marker="deploy")

    assert result.outcome is VerbOutcome.ERROR
    assert "did not report the bot's login" in result.message
    assert result.outputs == {"decision": "error"}
    assert harness.verb_api_calls == []


def test_without_a_marker_the_actor_is_not_needed() -> None:
    harness = Harness(actor=None, policy=LOCKFILES)

    assert run(harness).outputs["decision"] == "opened"


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({"title": ""}, "--title"),
        ({"title": "  "}, "--title"),
        ({"title": "two\nlines"}, "--title"),
        ({"title": "a\rb"}, "--title"),
        ({"title": "x" * 257}, "--title"),
        ({"body": ""}, "body is empty"),
        ({"body": " \n"}, "body is empty"),
        ({"label": [""]}, "--label"),
        ({"label": ["x" * 51]}, "--label"),
        ({"label": ["a\nb"]}, "--label"),
        ({"label": ["a\rb"]}, "--label"),
        ({"marker": "-->"}, "--marker"),
        ({"marker": "a b"}, "--marker"),
        ({"marker": "x" * 65}, "--marker"),
    ],
)
def test_invalid_input_is_an_error_before_any_call(args: dict[str, object], fragment: str) -> None:
    harness = Harness(policy=LOCKFILES)

    result = run(harness, **args)

    assert result.outcome is VerbOutcome.ERROR
    assert fragment in result.message
    assert result.outputs == {"decision": "error"}
    assert harness.verb_api_calls == []


def test_the_longest_valid_title_and_label_are_accepted() -> None:
    harness = Harness(policy=LOCKFILES)

    result = run(harness, title="x" * 256, label=["y" * 50])

    assert result.ok


def test_an_unknown_label_is_reported_with_the_apis_message() -> None:
    harness = harness_with(known_labels={"ci"})

    result = run(harness, label=["nope"])

    assert result.outcome is VerbOutcome.ERROR
    assert "422" in result.message
    assert "label 'nope' does not exist" in result.message
    assert harness.state.issues == {}


def test_an_unknown_label_on_update_is_reported_too() -> None:
    harness = harness_with(issue(), known_labels={"ci"})

    result = run(harness, marker="deploy", label=["ci", "nope"])

    assert result.outcome is VerbOutcome.ERROR
    assert "422" in result.message
    assert harness.state.issues[5].labels == ("ci",)
