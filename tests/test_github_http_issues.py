# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import httpx
import pytest
import respx

from lktec_bot.adapters.github_http import GitHubHttpError, HttpGitHubApi
from lktec_bot.ports.github_api import Issue

BASE = "https://api.github.test"
ISSUES = "/repos/acme/widgets/issues"


@pytest.fixture
def api() -> HttpGitHubApi:
    return HttpGitHubApi("ghs_token", base_url=BASE, sleep=lambda _: None, max_attempts=3)


def payload(number: int, **extra: object) -> dict[str, object]:
    return {
        "number": number,
        "title": f"t{number}",
        "body": f"b{number}",
        "state": "open",
        "html_url": f"https://github.test/i/{number}",
        "labels": [{"name": "e2e-check"}, {"name": "bug"}],
        "user": {"login": "my-bot[bot]"},
        **extra,
    }


@respx.mock(base_url=BASE)
def test_list_issues_asks_for_the_creators_issues_and_drops_pull_requests(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(ISSUES).respond(
        200, json=[payload(1), payload(2, pull_request={"url": "x"}), payload(3, labels=None)]
    )

    issues = api.list_issues("acme", "widgets", creator="my-bot[bot]")

    assert issues == [
        Issue(
            1, "t1", "b1", "open", "https://github.test/i/1", ("e2e-check", "bug"), "my-bot[bot]"
        ),
        Issue(3, "t3", "b3", "open", "https://github.test/i/3", (), "my-bot[bot]"),
    ]
    params = route.calls[0].request.url.params
    assert params["creator"] == "my-bot[bot]"
    assert params["state"] == "all"
    assert params["per_page"] == "100"


@respx.mock(base_url=BASE)
def test_list_issues_follows_the_pages(api: HttpGitHubApi, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(ISSUES)
    route.side_effect = [
        httpx.Response(200, json=[payload(i) for i in range(1, 101)]),
        httpx.Response(200, json=[payload(101, state="closed", body=None, user=None), "junk"]),
    ]

    issues = api.list_issues("acme", "widgets", creator="x", state="closed")

    assert len(issues) == 101
    assert issues[100] == Issue(
        101,
        "t101",
        "",
        "closed",
        "https://github.test/i/101",
        (
            "e2e-check",
            "bug",
        ),
        "",
    )
    assert route.calls[1].request.url.params["page"] == "2"
    assert route.calls[1].request.url.params["state"] == "closed"


@respx.mock(base_url=BASE)
def test_create_issue_posts_title_body_and_labels(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(ISSUES).respond(201, json=payload(7))

    issue = api.create_issue("acme", "widgets", title="t", body="b", labels=["e2e-check"])

    assert issue.number == 7
    assert issue.html_url == "https://github.test/i/7"
    assert json.loads(route.calls[0].request.content) == {
        "title": "t",
        "body": "b",
        "labels": ["e2e-check"],
    }


@respx.mock(base_url=BASE)
def test_create_issue_with_an_unknown_label_reports_githubs_message(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(ISSUES).respond(
        422, json={"message": "Validation Failed", "errors": [{"code": "invalid"}]}
    )

    with pytest.raises(GitHubHttpError, match=r"422: .*Validation Failed") as raised:
        api.create_issue("acme", "widgets", title="t", body="b", labels=["nope"])

    assert raised.value.status == 422
    assert route.call_count == 1  # a POST is never repeated


@respx.mock(base_url=BASE)
def test_update_issue_sends_only_the_given_fields(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.patch(f"{ISSUES}/7").respond(200, json=payload(7, title="new"))

    issue = api.update_issue("acme", "widgets", 7, title="new")

    assert issue.title == "new"
    assert json.loads(route.calls[0].request.content) == {"title": "new"}


@respx.mock(base_url=BASE)
def test_update_issue_can_change_everything_at_once(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.patch(f"{ISSUES}/7").respond(200, json=payload(7))

    api.update_issue("acme", "widgets", 7, title="t", body="b", state="open", labels=["a", "b"])

    assert json.loads(route.calls[0].request.content) == {
        "title": "t",
        "body": "b",
        "state": "open",
        "labels": ["a", "b"],
    }


@respx.mock(base_url=BASE)
def test_update_issue_with_an_unknown_label_is_an_error(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.patch(f"{ISSUES}/7").respond(422, json={"message": "Validation Failed"})

    with pytest.raises(GitHubHttpError, match="422"):
        api.update_issue("acme", "widgets", 7, labels=["nope"])
