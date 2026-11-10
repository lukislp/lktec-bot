# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from lktec_bot.adapters.github_http import GitHubHttpError, HttpGitHubApi
from lktec_bot.ports.github_api import (
    AutoMergeNotNeeded,
    BranchUpdate,
    IssueComment,
    MergeRefused,
    PullRequest,
    RateLimit,
    Review,
)

BASE = "https://api.github.test"
CONTENTS = "/repos/acme/widgets/contents/.github/lktec-bot.yml"


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def api(sleeps: list[float]) -> HttpGitHubApi:
    return HttpGitHubApi("ghs_token", base_url=BASE, sleep=sleeps.append, max_attempts=3)


def file_response(text: str = "version: 1\n") -> httpx.Response:
    content = base64.b64encode(text.encode()).decode()
    return httpx.Response(200, json={"encoding": "base64", "content": content})


@respx.mock(base_url=BASE)
def test_get_file_content_decodes_base64(api: HttpGitHubApi, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(CONTENTS).mock(return_value=file_response())

    text = api.get_file_content("acme", "widgets", ".github/lktec-bot.yml", ref="main")

    assert text == "version: 1\n"
    req = route.calls[0].request
    assert req.url.params["ref"] == "main"
    assert req.headers["authorization"] == "Bearer ghs_token"
    assert req.headers["user-agent"].startswith("lktec-bot/")
    assert req.headers["x-github-api-version"] == "2022-11-28"


@respx.mock(base_url=BASE)
def test_get_file_content_without_ref_reads_the_default_branch(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(CONTENTS).mock(return_value=file_response())

    api.get_file_content("acme", "widgets", ".github/lktec-bot.yml")

    assert "ref" not in route.calls[0].request.url.params


@respx.mock(base_url=BASE)
def test_get_file_content_returns_none_for_404(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(CONTENTS).respond(404)

    assert api.get_file_content("acme", "widgets", ".github/lktec-bot.yml") is None


@respx.mock(base_url=BASE)
def test_get_file_content_rejects_directories(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("/repos/acme/widgets/contents/.github").respond(200, json=[{"name": "x"}])

    with pytest.raises(GitHubHttpError, match="not a file"):
        api.get_file_content("acme", "widgets", ".github")


# -- retries --------------------------------------------------------------------------------


@respx.mock(base_url=BASE)
def test_idempotent_request_retries_5xx_then_succeeds(
    api: HttpGitHubApi, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.get(CONTENTS)
    route.side_effect = [httpx.Response(502, text="bad gateway"), file_response()]

    assert api.get_file_content("acme", "widgets", ".github/lktec-bot.yml") == "version: 1\n"
    assert route.call_count == 2
    assert sleeps == [1.0]


@respx.mock(base_url=BASE)
def test_post_is_not_retried_on_5xx(
    api: HttpGitHubApi, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.post("/repos/acme/widgets/issues/7/comments").respond(502, text="bad")

    with pytest.raises(GitHubHttpError, match="502") as excinfo:
        api.create_issue_comment("acme", "widgets", 7, "hello")

    assert excinfo.value.status == 502
    assert route.call_count == 1
    assert sleeps == []


@respx.mock(base_url=BASE)
def test_post_retries_a_secondary_rate_limit_with_retry_after(
    api: HttpGitHubApi, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.post("/repos/acme/widgets/pulls/3/reviews")
    route.side_effect = [
        httpx.Response(
            403,
            headers={"retry-after": "7"},
            json={"message": "You have exceeded a secondary rate limit."},
        ),
        httpx.Response(200, json={"html_url": "https://github.test/r/1"}),
    ]

    url = api.create_review("acme", "widgets", 3, "APPROVE", "lgtm")

    assert url == "https://github.test/r/1"
    assert sleeps == [7.0]
    assert json.loads(route.calls[0].request.content) == {"event": "APPROVE", "body": "lgtm"}


@respx.mock(base_url=BASE)
def test_rate_limit_without_retry_after_uses_exhausted_header_or_body(
    api: HttpGitHubApi, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.post("/repos/acme/widgets/issues/1/comments")
    route.side_effect = [
        httpx.Response(429, headers={"x-ratelimit-remaining": "0"}, text="slow down"),
        httpx.Response(403, text="You have exceeded a secondary rate limit, try later"),
        httpx.Response(201, json={"html_url": "u"}),
    ]

    assert api.create_issue_comment("acme", "widgets", 1, "x") == "u"
    assert sleeps == [1.0, 2.0]


@respx.mock(base_url=BASE)
def test_gives_up_after_max_attempts(
    api: HttpGitHubApi, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.put("/repos/acme/widgets/pulls/5/update-branch").respond(503, text="down")

    with pytest.raises(GitHubHttpError, match="503") as excinfo:
        api.update_branch("acme", "widgets", 5, "abc123")

    assert excinfo.value.status == 503
    assert route.call_count == 3
    assert len(sleeps) == 2


@respx.mock(base_url=BASE)
def test_non_retryable_errors_raise_immediately(
    api: HttpGitHubApi, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.post("/repos/acme/widgets/issues/1/comments").respond(
        403, json={"message": "Resource not accessible by integration"}
    )

    with pytest.raises(GitHubHttpError, match="not accessible"):
        api.create_issue_comment("acme", "widgets", 1, "x")

    assert route.call_count == 1
    assert sleeps == []


@respx.mock(base_url=BASE)
def test_other_client_errors_are_not_retried(
    api: HttpGitHubApi, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.put("/repos/acme/widgets/pulls/5/update-branch").respond(
        422, json={"message": "Validation Failed"}
    )

    with pytest.raises(GitHubHttpError, match="422") as excinfo:
        api.update_branch("acme", "widgets", 5, "abc123")

    assert excinfo.value.status == 422
    assert route.call_count == 1
    assert sleeps == []


@respx.mock(base_url=BASE)
def test_retry_after_beyond_the_limit_fails_instead_of_capping(
    respx_mock: respx.MockRouter,
) -> None:
    sleeps: list[float] = []
    api = HttpGitHubApi(
        "t", base_url=BASE, sleep=sleeps.append, max_attempts=3, max_backoff_seconds=30.0
    )
    route = respx_mock.get(CONTENTS).respond(429, headers={"retry-after": "120"}, text="wait")

    with pytest.raises(
        GitHubHttpError, match="Retry-After 120 s exceeds the 30 s limit"
    ) as excinfo:
        api.get_file_content("acme", "widgets", ".github/lktec-bot.yml")

    assert excinfo.value.status == 429
    assert route.call_count == 1
    assert sleeps == []


@respx.mock(base_url=BASE)
def test_exponential_backoff_is_capped(respx_mock: respx.MockRouter) -> None:
    sleeps: list[float] = []
    api = HttpGitHubApi(
        "t", base_url=BASE, sleep=sleeps.append, max_attempts=4, max_backoff_seconds=1.5
    )
    route = respx_mock.get(CONTENTS)
    route.side_effect = [httpx.Response(500), httpx.Response(500), file_response()]

    assert api.get_file_content("acme", "widgets", ".github/lktec-bot.yml") == "version: 1\n"
    assert sleeps == [1.0, 1.5]


@respx.mock(base_url=BASE)
def test_transport_error_is_retried_once_for_idempotent_methods(
    api: HttpGitHubApi, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.get(CONTENTS)
    route.side_effect = [httpx.ConnectError("connection reset"), file_response()]

    assert api.get_file_content("acme", "widgets", ".github/lktec-bot.yml") == "version: 1\n"
    assert route.call_count == 2
    assert sleeps == [1.0]


@respx.mock(base_url=BASE)
def test_second_transport_error_is_a_github_http_error(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(CONTENTS)
    route.side_effect = [httpx.ConnectError("reset"), httpx.ReadTimeout("timed out")]

    with pytest.raises(GitHubHttpError, match=r"GET .* failed: ReadTimeout: timed out") as excinfo:
        api.get_file_content("acme", "widgets", ".github/lktec-bot.yml")

    assert excinfo.value.status is None
    assert isinstance(excinfo.value.__cause__, httpx.ReadTimeout)
    assert route.call_count == 2


@respx.mock(base_url=BASE)
def test_transport_error_is_not_retried_for_post(
    api: HttpGitHubApi, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.post("/repos/acme/widgets/issues/1/comments")
    route.side_effect = httpx.ConnectError("reset")

    with pytest.raises(GitHubHttpError, match="ConnectError"):
        api.create_issue_comment("acme", "widgets", 1, "x")

    assert route.call_count == 1
    assert sleeps == []


@respx.mock(base_url=BASE)
def test_transport_error_is_not_retried_on_the_last_attempt(respx_mock: respx.MockRouter) -> None:
    sleeps: list[float] = []
    api = HttpGitHubApi("t", base_url=BASE, sleep=sleeps.append, max_attempts=1)
    route = respx_mock.get(CONTENTS)
    route.side_effect = httpx.ConnectError("reset")

    with pytest.raises(GitHubHttpError, match="ConnectError"):
        api.get_file_content("acme", "widgets", ".github/lktec-bot.yml")

    assert route.call_count == 1
    assert sleeps == []


# -- the other calls -----------------------------------------------------------------------


@respx.mock(base_url=BASE)
def test_update_branch_accepted(api: HttpGitHubApi, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.put("/repos/acme/widgets/pulls/5/update-branch").respond(
        202, json={"message": "Updating pull request branch.", "url": "u"}
    )

    assert api.update_branch("acme", "widgets", 5, "abc123") is BranchUpdate.ACCEPTED
    assert json.loads(route.calls[0].request.content) == {"expected_head_sha": "abc123"}


@respx.mock(base_url=BASE)
def test_update_branch_already_up_to_date_is_not_an_error(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.put("/repos/acme/widgets/pulls/5/update-branch").respond(
        422, json={"message": "There are no new commits on the base branch."}
    )

    assert api.update_branch("acme", "widgets", 5, "abc123") is BranchUpdate.UP_TO_DATE


@respx.mock(base_url=BASE)
def test_update_branch_other_422_is_an_error(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.put("/repos/acme/widgets/pulls/5/update-branch").respond(
        422, json={"message": "expected_head_sha does not match the current head"}
    )

    with pytest.raises(GitHubHttpError, match="does not match") as excinfo:
        api.update_branch("acme", "widgets", 5, "abc123")
    assert excinfo.value.status == 422


@respx.mock(base_url=BASE)
def test_get_pull_request_maps_the_fields(api: HttpGitHubApi, respx_mock: respx.MockRouter) -> None:
    respx_mock.get("/repos/acme/widgets/pulls/5").respond(
        200,
        json={
            "number": 5,
            "html_url": "https://github.test/acme/widgets/pull/5",
            "state": "open",
            "draft": False,
            "head": {"sha": "abc123"},
            "auto_merge": {"merge_method": "squash", "enabled_by": {"login": "x"}},
        },
    )

    pull_request = api.get_pull_request("acme", "widgets", 5)

    assert pull_request == PullRequest(
        number=5,
        html_url="https://github.test/acme/widgets/pull/5",
        state="open",
        draft=False,
        head_sha="abc123",
        auto_merge_method="SQUASH",
    )
    assert pull_request.open


@pytest.mark.parametrize(
    ("auto_merge", "expected"),
    [
        (None, None),
        ({"merge_method": "merge"}, "MERGE"),
        ({"merge_method": "rebase"}, "REBASE"),
        ({"merge_method": "fast-forward"}, None),
    ],
)
@respx.mock(base_url=BASE)
def test_get_pull_request_auto_merge_variants(
    api: HttpGitHubApi, respx_mock: respx.MockRouter, auto_merge: object, expected: str | None
) -> None:
    respx_mock.get("/repos/acme/widgets/pulls/5").respond(
        200, json={"state": "closed", "draft": True, "head": {"sha": "s"}, "auto_merge": auto_merge}
    )

    pull_request = api.get_pull_request("acme", "widgets", 5)

    assert pull_request.auto_merge_method == expected
    assert pull_request.number == 5
    assert pull_request.draft is True
    assert not pull_request.open


@respx.mock(base_url=BASE)
def test_get_pull_request_without_a_head_is_an_error(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("/repos/acme/widgets/pulls/5").respond(200, json={"number": 5})

    with pytest.raises(GitHubHttpError, match="no head"):
        api.get_pull_request("acme", "widgets", 5)


@respx.mock(base_url=BASE)
def test_list_reviews_maps_the_fields(api: HttpGitHubApi, respx_mock: respx.MockRouter) -> None:
    respx_mock.get("/repos/acme/widgets/pulls/5/reviews").respond(
        200,
        json=[
            {
                "user": {"login": "my-bot[bot]"},
                "state": "APPROVED",
                "commit_id": "abc",
                "html_url": "r1",
            },
            {"user": None, "state": "COMMENTED", "commit_id": "abc", "html_url": "r2"},
        ],
    )

    assert api.list_reviews("acme", "widgets", 5) == [
        Review("my-bot[bot]", "APPROVED", "abc", "r1"),
        Review("", "COMMENTED", "abc", "r2"),
    ]


@respx.mock(base_url=BASE)
def test_list_issue_comments_follows_the_pages(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    def entry(index: int) -> dict[str, object]:
        return {
            "id": index,
            "user": {"login": "my-bot[bot]", "type": "Bot"},
            "body": f"c{index}",
            "html_url": f"u{index}",
        }

    route = respx_mock.get("/repos/acme/widgets/issues/5/comments")
    route.side_effect = [
        httpx.Response(200, json=[entry(i) for i in range(100)]),
        httpx.Response(200, json=[entry(100), {"id": 101, "user": None, "body": None}, "junk"]),
    ]

    comments = api.list_issue_comments("acme", "widgets", 5)

    assert len(comments) == 102
    assert comments[0] == IssueComment(0, "my-bot[bot]", "Bot", "c0", "u0")
    assert comments[101] == IssueComment(101, "", "", "", "")
    assert route.calls[0].request.url.params["per_page"] == "100"
    assert route.calls[0].request.url.params["page"] == "1"


@respx.mock(base_url=BASE)
def test_pagination_stops_on_an_endpoint_that_ignores_the_page(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    # The same full page on every request: the walk must not go on until the rate limit ends it.
    same = [{"id": i, "user": None, "body": f"c{i}", "html_url": ""} for i in range(100)]
    route = respx_mock.get("/repos/acme/widgets/issues/5/comments").respond(200, json=same)

    with pytest.raises(GitHubHttpError, match="does not paginate"):
        api.list_issue_comments("acme", "widgets", 5)

    assert route.call_count == 2


@respx.mock(base_url=BASE)
def test_pagination_refuses_to_walk_past_the_page_ceiling(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    def page(number: int) -> httpx.Response:
        start = number * 100
        return httpx.Response(
            200,
            json=[
                {"id": i, "user": None, "body": "", "html_url": ""}
                for i in range(start, start + 100)
            ],
        )

    route = respx_mock.get("/repos/acme/widgets/issues/5/comments")
    route.side_effect = lambda request: page(int(request.url.params["page"]))

    with pytest.raises(GitHubHttpError, match="more than 100 pages"):
        api.list_issue_comments("acme", "widgets", 5)

    assert route.call_count == 100
    assert route.calls[1].request.url.params["page"] == "2"


@respx.mock(base_url=BASE)
def test_list_that_is_not_a_list_is_an_error(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("/repos/acme/widgets/issues/5/comments").respond(200, json={"message": "x"})

    with pytest.raises(GitHubHttpError, match="did not answer with a list"):
        api.list_issue_comments("acme", "widgets", 5)


@respx.mock(base_url=BASE)
def test_update_issue_comment_patches_the_body(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.patch("/repos/acme/widgets/issues/comments/42").respond(
        200, json={"html_url": "https://github.test/c/42"}
    )

    assert api.update_issue_comment("acme", "widgets", 42, "new") == "https://github.test/c/42"
    assert json.loads(route.calls[0].request.content) == {"body": "new"}


@respx.mock(base_url=BASE)
def test_patch_is_not_retried_on_5xx(
    api: HttpGitHubApi, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.patch("/repos/acme/widgets/issues/comments/42").respond(502)

    with pytest.raises(GitHubHttpError, match="502"):
        api.update_issue_comment("acme", "widgets", 42, "new")
    assert route.call_count == 1
    assert sleeps == []


@respx.mock(base_url=BASE)
def test_enable_auto_merge_resolves_the_node_id_then_mutates(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("/graphql")
    route.side_effect = [
        httpx.Response(200, json={"data": {"repository": {"pullRequest": {"id": "PR_node"}}}}),
        httpx.Response(
            200,
            json={
                "data": {
                    "enablePullRequestAutoMerge": {
                        "pullRequest": {"url": "https://github.test/acme/widgets/pull/9"}
                    }
                }
            },
        ),
    ]

    url = api.enable_auto_merge("acme", "widgets", 9, "SQUASH")

    assert url == "https://github.test/acme/widgets/pull/9"
    lookup = json.loads(route.calls[0].request.content)
    assert "pullRequest(number: $number) { id }" in lookup["query"]
    assert lookup["variables"] == {"owner": "acme", "name": "widgets", "number": 9}
    mutation = json.loads(route.calls[1].request.content)
    assert "enablePullRequestAutoMerge" in mutation["query"]
    assert mutation["variables"] == {"pullRequestId": "PR_node", "mergeMethod": "SQUASH"}


@pytest.mark.parametrize(
    ("message", "state"),
    [
        ("Pull request Pull request is in clean status", "clean"),
        ("Pull request Pull request is in unstable status", "unstable"),
        ("Pull request is in HAS_HOOKS status", "has_hooks"),
    ],
)
@respx.mock(base_url=BASE)
def test_enable_auto_merge_maps_the_mergeable_now_refusal(
    api: HttpGitHubApi, respx_mock: respx.MockRouter, message: str, state: str
) -> None:
    route = respx_mock.post("/graphql")
    route.side_effect = [
        httpx.Response(200, json={"data": {"repository": {"pullRequest": {"id": "PR_node"}}}}),
        httpx.Response(200, json={"data": None, "errors": [{"message": message}]}),
    ]

    with pytest.raises(AutoMergeNotNeeded) as excinfo:
        api.enable_auto_merge("acme", "widgets", 9, "SQUASH")

    assert excinfo.value.state == state


@respx.mock(base_url=BASE)
def test_enable_auto_merge_keeps_other_graphql_errors_generic(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("/graphql")
    route.side_effect = [
        httpx.Response(200, json={"data": {"repository": {"pullRequest": {"id": "PR_node"}}}}),
        httpx.Response(
            200, json={"data": None, "errors": [{"message": "Pull request is in dirty status"}]}
        ),
    ]

    with pytest.raises(GitHubHttpError, match="in dirty status") as excinfo:
        api.enable_auto_merge("acme", "widgets", 9, "SQUASH")

    assert not isinstance(excinfo.value, AutoMergeNotNeeded)


@respx.mock(base_url=BASE)
def test_enable_auto_merge_reports_a_missing_pull_request(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("/graphql").respond(200, json={"data": {"repository": {"pullRequest": None}}})

    with pytest.raises(GitHubHttpError, match=r"no repository\.pullRequest \(missing or null\)"):
        api.enable_auto_merge("acme", "widgets", 9, "SQUASH")


@respx.mock(base_url=BASE)
def test_enable_auto_merge_reports_a_null_mutation_result(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("/graphql")
    route.side_effect = [
        httpx.Response(200, json={"data": {"repository": {"pullRequest": {"id": "PR_node"}}}}),
        httpx.Response(200, json={"data": {"enablePullRequestAutoMerge": None}}),
    ]

    with pytest.raises(GitHubHttpError, match="no enablePullRequestAutoMerge"):
        api.enable_auto_merge("acme", "widgets", 9, "SQUASH")


@respx.mock(base_url=BASE)
def test_graphql_errors_raise(api: HttpGitHubApi, respx_mock: respx.MockRouter) -> None:
    respx_mock.post("/graphql").respond(
        200, json={"data": None, "errors": [{"message": "Pull request is not mergeable"}, "odd"]}
    )

    with pytest.raises(GitHubHttpError, match="not mergeable; odd"):
        api.graphql("mutation { x }")


@respx.mock(base_url=BASE)
def test_graphql_errors_that_are_not_a_list_are_still_reported(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("/graphql").respond(200, json={"errors": "something broke"})

    with pytest.raises(GitHubHttpError, match="GraphQL errors: something broke"):
        api.graphql("query { x }")


@respx.mock(base_url=BASE)
def test_graphql_without_data_raises(api: HttpGitHubApi, respx_mock: respx.MockRouter) -> None:
    respx_mock.post("/graphql").respond(200, json={})

    with pytest.raises(GitHubHttpError, match="without data"):
        api.graphql("query { x }")


@respx.mock(base_url=BASE)
def test_graphql_tolerates_payloads_that_are_not_objects(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("/graphql")
    route.side_effect = [httpx.Response(200, json=[1, 2]), httpx.Response(200, text="<html>")]

    with pytest.raises(GitHubHttpError, match="GraphQL response is list, not an object"):
        api.graphql("query { x }")
    with pytest.raises(GitHubHttpError, match="not JSON"):
        api.graphql("query { x }")


def test_default_client_when_none_is_given_and_close() -> None:
    api = HttpGitHubApi("t")

    assert isinstance(api._client, httpx.Client)
    assert api.base_url == "https://api.github.com"
    api.close()
    api.close()
    assert api._client.is_closed


@respx.mock(base_url=BASE)
def test_get_pull_request_reads_author_title_body_and_merge_state(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("/repos/acme/widgets/pulls/7").respond(
        200,
        json={
            "number": 7,
            "html_url": "https://github.com/acme/widgets/pull/7",
            "state": "open",
            "draft": False,
            "title": "bump ruff from 0.1.0 to 0.1.1",
            "body": None,
            "mergeable_state": "behind",
            "user": {"login": "dependabot[bot]"},
            "head": {"sha": "abc"},
        },
    )

    pull_request = api.get_pull_request("acme", "widgets", 7)

    assert pull_request.title == "bump ruff from 0.1.0 to 0.1.1"
    assert pull_request.body == ""
    assert pull_request.author_login == "dependabot[bot]"
    assert pull_request.merge_state == "BEHIND"


@respx.mock(base_url=BASE)
def test_list_pull_requests_asks_for_open_ones_oldest_first(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    entry = {
        "number": 3,
        "state": "open",
        "head": {"sha": "abc"},
        "user": {"login": "someone"},
        "auto_merge": {"merge_method": "squash"},
    }
    route = respx_mock.get("/repos/acme/widgets/pulls").respond(200, json=[entry])

    pull_requests = api.list_pull_requests("acme", "widgets")

    params = route.calls[0].request.url.params
    assert (params["state"], params["sort"], params["direction"]) == ("open", "created", "asc")
    assert [pr.number for pr in pull_requests] == [3]
    assert pull_requests[0].auto_merge_method == "SQUASH"
    assert pull_requests[0].merge_state == ""


@respx.mock(base_url=BASE)
def test_list_pull_requests_rejects_an_entry_without_a_head(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("/repos/acme/widgets/pulls").respond(200, json=[{"number": 3}])

    with pytest.raises(GitHubHttpError, match="a pull request in the list has no head"):
        api.list_pull_requests("acme", "widgets")


@respx.mock(base_url=BASE)
def test_list_pull_request_files_includes_the_old_name_of_a_rename(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("/repos/acme/widgets/pulls/7/files").respond(
        200,
        json=[
            {"filename": "a.txt"},
            {"filename": "new.yml", "previous_filename": ".github/workflows/old.yml"},
        ],
    )

    paths = api.list_pull_request_files("acme", "widgets", 7)

    assert paths == ["a.txt", "new.yml", ".github/workflows/old.yml"]


@respx.mock(base_url=BASE)
def test_list_pull_request_commits_reads_the_signature_verification(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("/repos/acme/widgets/pulls/7/commits").respond(
        200,
        json=[
            {"sha": "a1", "commit": {"verification": {"verified": True, "reason": "valid"}}},
            {"sha": "b2", "commit": {"verification": {"verified": False, "reason": "unsigned"}}},
            {"sha": "c3", "commit": {}},
            {"sha": "d4", "commit": {"verification": None}},
            {"sha": "e5"},
        ],
    )

    commits = api.list_pull_request_commits("acme", "widgets", 7)

    assert [(c.sha, c.verified, c.reason) for c in commits] == [
        ("a1", True, "valid"),
        ("b2", False, "unsigned"),
        ("c3", False, "unknown"),
        ("d4", False, "unknown"),
        ("e5", False, "unknown"),
    ]


@respx.mock(base_url=BASE)
def test_get_pull_request_reads_the_head_branch_and_repository(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    payload = {
        "number": 7,
        "state": "open",
        "head": {"sha": "abc", "ref": "dependabot/uv/x", "repo": {"full_name": "acme/widgets"}},
    }
    respx_mock.get("/repos/acme/widgets/pulls/7").respond(200, json=payload)
    respx_mock.get("/repos/acme/widgets/pulls/8").respond(
        200, json={**payload, "number": 8, "head": {"sha": "abc", "repo": None}}
    )

    kept = api.get_pull_request("acme", "widgets", 7)
    gone = api.get_pull_request("acme", "widgets", 8)

    assert (kept.head_ref, kept.head_repo) == ("dependabot/uv/x", "acme/widgets")
    assert (gone.head_ref, gone.head_repo) == ("", "")


@respx.mock(base_url=BASE)
def test_get_branch_head_reads_the_ref_with_slashes_in_the_name(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get("/repos/acme/widgets/git/ref/heads/dependabot/uv/x%20y").respond(
        200, json={"object": {"sha": "abc123"}}
    )

    assert api.get_branch_head("acme", "widgets", "dependabot/uv/x y") == "abc123"
    assert route.called


@respx.mock(base_url=BASE)
def test_get_branch_head_is_none_for_a_missing_branch(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("/repos/acme/widgets/git/ref/heads/gone").respond(404)

    assert api.get_branch_head("acme", "widgets", "gone") is None


@pytest.mark.parametrize("payload", [{}, {"object": {}}, {"object": {"sha": ""}}, []])
@respx.mock(base_url=BASE)
def test_get_branch_head_rejects_an_answer_without_a_commit(
    api: HttpGitHubApi, respx_mock: respx.MockRouter, payload: object
) -> None:
    respx_mock.get("/repos/acme/widgets/git/ref/heads/odd").respond(200, json=payload)

    with pytest.raises(GitHubHttpError, match="no commit"):
        api.get_branch_head("acme", "widgets", "odd")


COMMIT_RESPONSE = {
    "data": {
        "createCommitOnBranch": {
            "commit": {"oid": "c0ffee", "url": "https://github.com/acme/widgets/commit/c0ffee"}
        }
    }
}


@respx.mock(base_url=BASE)
def test_create_commit_on_branch_sends_the_input_with_base64_contents(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("/graphql").respond(200, json=COMMIT_RESPONSE)

    created = api.create_commit_on_branch(
        "acme",
        "widgets",
        "dependabot/uv/x",
        "chore: refresh",
        "details",
        "a" * 40,
        {"uv.lock": b"v1\n\xc3\xa4", "sub/p.lock": b""},
        ["old.lock"],
    )

    assert (created.oid, created.url) == ("c0ffee", "https://github.com/acme/widgets/commit/c0ffee")
    sent = json.loads(route.calls[0].request.content)
    assert "createCommitOnBranch(input: $input)" in sent["query"]
    assert sent["variables"]["input"] == {
        "branch": {"repositoryNameWithOwner": "acme/widgets", "branchName": "dependabot/uv/x"},
        "message": {"headline": "chore: refresh", "body": "details"},
        "expectedHeadOid": "a" * 40,
        "fileChanges": {
            "additions": [
                {"path": "uv.lock", "contents": base64.b64encode(b"v1\n\xc3\xa4").decode()},
                {"path": "sub/p.lock", "contents": ""},
            ],
            "deletions": [{"path": "old.lock"}],
        },
    }


@respx.mock(base_url=BASE)
def test_create_commit_on_branch_omits_empty_parts(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("/graphql").respond(200, json=COMMIT_RESPONSE)

    api.create_commit_on_branch("acme", "widgets", "b", "h", "", "a" * 40, {}, ["x"])

    sent = json.loads(route.calls[0].request.content)["variables"]["input"]
    assert sent["message"] == {"headline": "h"}
    assert sent["fileChanges"] == {"deletions": [{"path": "x"}]}


@respx.mock(base_url=BASE)
def test_create_commit_on_branch_fails_when_the_branch_moved(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("/graphql").respond(
        200, json={"errors": [{"message": "Expected branch to point to abc but it did not"}]}
    )

    with pytest.raises(GitHubHttpError, match="Expected branch to point to"):
        api.create_commit_on_branch("acme", "widgets", "b", "h", "", "a" * 40, {"x": b"1"}, [])


@respx.mock(base_url=BASE)
def test_create_commit_on_branch_is_not_retried_on_5xx(
    api: HttpGitHubApi, respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    route = respx_mock.post("/graphql").respond(502)

    with pytest.raises(GitHubHttpError):
        api.create_commit_on_branch("acme", "widgets", "b", "h", "", "a" * 40, {"x": b"1"}, [])

    assert route.call_count == 1
    assert sleeps == []


@respx.mock(base_url=BASE)
def test_create_commit_on_branch_reports_a_null_commit(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("/graphql").respond(200, json={"data": {"createCommitOnBranch": None}})

    with pytest.raises(GitHubHttpError, match="createCommitOnBranch"):
        api.create_commit_on_branch("acme", "widgets", "b", "h", "", "a" * 40, {"x": b"1"}, [])


# -- primary rate limit ----------------------------------------------------------------------

NOW = 1_700_000_000.0
PRIMARY_BODY = {"message": "API rate limit exceeded for installation ID 178980993."}


def primary_api(
    sleeps: list[float], logs: list[str], max_rate_limit_wait_seconds: float = 600.0
) -> HttpGitHubApi:
    return HttpGitHubApi(
        "t",
        base_url=BASE,
        sleep=sleeps.append,
        clock=lambda: NOW,
        log=logs.append,
        max_attempts=3,
        max_rate_limit_wait_seconds=max_rate_limit_wait_seconds,
    )


def test_primary_limit_within_the_window_waits_once_and_retries_once(
    respx_mock: respx.MockRouter,
) -> None:
    sleeps: list[float] = []
    logs: list[str] = []
    api = primary_api(sleeps, logs)
    route = respx_mock.get(f"{BASE}/repos/acme/widgets/git/matching-refs/tags/v").mock(
        side_effect=[
            httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + 90)},
                json=PRIMARY_BODY,
            ),
            httpx.Response(200, json=[{"ref": "refs/tags/v1.0.0"}]),
        ]
    )

    assert api.list_tags("acme", "widgets", "v") == ["v1.0.0"]

    assert route.call_count == 2
    assert sleeps == [92.0]
    assert logs == [
        "primary rate limit exhausted for this installation; waiting 92 s until it resets"
    ]


def test_primary_limit_is_recognised_by_the_body_alone(respx_mock: respx.MockRouter) -> None:
    sleeps: list[float] = []
    api = primary_api(sleeps, [])
    respx_mock.get(f"{BASE}/x").mock(
        side_effect=[
            httpx.Response(
                429, headers={"x-ratelimit-reset": str(int(NOW) - 5)}, json=PRIMARY_BODY
            ),
            httpx.Response(200, json={}),
        ]
    )

    api._request("GET", "/x")

    assert sleeps == [0.0]  # a reset in the past never sleeps a negative time


def test_primary_limit_beyond_the_window_fails_at_once_with_the_reset_time(
    respx_mock: respx.MockRouter,
) -> None:
    sleeps: list[float] = []
    logs: list[str] = []
    api = primary_api(sleeps, logs, max_rate_limit_wait_seconds=600.0)
    reset = int(NOW) + 3000
    route = respx_mock.get(f"{BASE}/x").mock(
        return_value=httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
            json=PRIMARY_BODY,
        )
    )

    with pytest.raises(GitHubHttpError) as caught:
        api._request("GET", "/x")

    message = str(caught.value)
    assert "primary rate limit is exhausted" in message
    assert "resets at 2023-11-14T23:03:20Z" in message
    assert "retry the run after that" in message
    assert caught.value.status == 403
    assert route.call_count == 1
    assert sleeps == []
    assert logs == []


def test_a_second_primary_answer_after_the_wait_is_not_waited_for_again(
    respx_mock: respx.MockRouter,
) -> None:
    sleeps: list[float] = []
    api = primary_api(sleeps, [])
    route = respx_mock.get(f"{BASE}/x").mock(
        return_value=httpx.Response(
            403, headers={"x-ratelimit-reset": str(int(NOW) + 10)}, json=PRIMARY_BODY
        )
    )

    with pytest.raises(GitHubHttpError, match="primary rate limit is exhausted"):
        api._request("GET", "/x")

    assert route.call_count == 2
    assert sleeps == [12.0]


def test_primary_limit_without_a_reset_header_stays_on_the_back_off_path(
    respx_mock: respx.MockRouter,
) -> None:
    sleeps: list[float] = []
    api = primary_api(sleeps, [])
    respx_mock.get(f"{BASE}/x").mock(
        side_effect=[
            httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json=PRIMARY_BODY),
            httpx.Response(200, json={}),
        ]
    )

    api._request("GET", "/x")

    assert sleeps == [1.0]


def test_retry_after_or_secondary_words_never_count_as_primary(
    respx_mock: respx.MockRouter,
) -> None:
    sleeps: list[float] = []
    api = primary_api(sleeps, [])
    reset = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + 3000)}
    respx_mock.get(f"{BASE}/x").mock(
        side_effect=[
            httpx.Response(403, headers={**reset, "retry-after": "5"}, text="slow down"),
            httpx.Response(
                403, headers=reset, json={"message": "You have exceeded a secondary rate limit."}
            ),
            httpx.Response(200, json={}),
        ]
    )

    api._request("GET", "/x")

    assert sleeps == [5.0, 2.0]


def test_last_rate_limit_follows_every_response(respx_mock: respx.MockRouter) -> None:
    api = primary_api([], [])
    before = api.last_rate_limit
    assert before is None
    respx_mock.get(f"{BASE}/a").mock(
        return_value=httpx.Response(
            200, headers={"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "1700003600"}
        )
    )
    respx_mock.get(f"{BASE}/b").mock(
        return_value=httpx.Response(200, headers={"x-ratelimit-remaining": "4998"})
    )
    respx_mock.get(f"{BASE}/c").mock(return_value=httpx.Response(200))

    api._request("GET", "/a")
    assert api.last_rate_limit == RateLimit(4999, 1_700_003_600)
    api._request("GET", "/b")
    assert api.last_rate_limit == RateLimit(4998, None)
    api._request("GET", "/c")  # no header: the last known budget stays
    assert api.last_rate_limit == RateLimit(4998, None)


def test_the_default_log_writes_one_line_to_stderr(
    respx_mock: respx.MockRouter, capsys: pytest.CaptureFixture[str]
) -> None:
    sleeps: list[float] = []
    api = HttpGitHubApi("t", base_url=BASE, sleep=sleeps.append, clock=lambda: NOW)
    respx_mock.get(f"{BASE}/x").mock(
        side_effect=[
            httpx.Response(
                403, headers={"x-ratelimit-reset": str(int(NOW) + 1)}, json=PRIMARY_BODY
            ),
            httpx.Response(200, json={}),
        ]
    )

    api._request("GET", "/x")

    assert "waiting 3 s until it resets" in capsys.readouterr().err


def test_a_plain_403_with_a_reset_header_is_not_a_rate_limit(respx_mock: respx.MockRouter) -> None:
    sleeps: list[float] = []
    api = primary_api(sleeps, [])
    route = respx_mock.get(f"{BASE}/x").mock(
        return_value=httpx.Response(
            403, headers={"x-ratelimit-reset": str(int(NOW) + 10)}, json={"message": "Forbidden"}
        )
    )

    with pytest.raises(GitHubHttpError, match="403"):
        api._request("GET", "/x")

    assert route.call_count == 1
    assert sleeps == []


@respx.mock(base_url=BASE)
def test_merge_pull_request_sends_method_and_sha_and_returns_the_merge_commit(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.put("/repos/acme/widgets/pulls/5/merge").respond(
        200, json={"sha": "abc", "merged": True, "message": "Pull Request successfully merged"}
    )

    assert api.merge_pull_request("acme", "widgets", 5, "SQUASH", "head1") == "abc"
    assert json.loads(route.calls[0].request.content) == {"merge_method": "squash", "sha": "head1"}


@pytest.mark.parametrize("status", [405, 409])
@respx.mock(base_url=BASE)
def test_merge_pull_request_refusals_are_not_errors(
    status: int, api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.put("/repos/acme/widgets/pulls/5/merge").respond(
        status, json={"message": "Head branch was modified. Review and try the merge again."}
    )

    with pytest.raises(MergeRefused, match="Head branch was modified"):
        api.merge_pull_request("acme", "widgets", 5, "MERGE", "head1")

    assert route.call_count == 1


@respx.mock(base_url=BASE)
def test_merge_pull_request_other_errors_stay_errors(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.put("/repos/acme/widgets/pulls/5/merge").respond(403, json={"message": "nope"})

    with pytest.raises(GitHubHttpError, match="403"):
        api.merge_pull_request("acme", "widgets", 5, "REBASE", "head1")
