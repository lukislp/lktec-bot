# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The HTTP adapter's calls for releases, tags, comparisons, branches and pull requests."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from lktec_bot.adapters.github_http import GitHubHttpError, HttpGitHubApi
from lktec_bot.ports.github_api import CommitInfo, Comparison, Release

BASE = "https://api.github.test"
REPO = "/repos/acme/widgets"
SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def api() -> HttpGitHubApi:
    return HttpGitHubApi("ghs_token", base_url=BASE, sleep=lambda _: None, max_attempts=2)


def body_of(route: respx.Route, call: int = 0) -> dict[str, object]:
    return json.loads(route.calls[call].request.content)  # type: ignore[no-any-return]


@respx.mock(base_url=BASE)
def test_create_branch_posts_a_ref(api: HttpGitHubApi, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{REPO}/git/refs").respond(201, json={})

    api.create_branch("acme", "widgets", "release/next", SHA)

    assert body_of(route) == {"ref": "refs/heads/release/next", "sha": SHA}


@respx.mock(base_url=BASE)
def test_get_release_by_tag(api: HttpGitHubApi, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{REPO}/releases/tags/v1.0.0").respond(
        200, json={"tag_name": "v1.0.0", "html_url": "https://github.test/r"}
    )
    respx_mock.get(f"{REPO}/releases/tags/v0.9.0").respond(404)
    respx_mock.get(f"{REPO}/releases/tags/v0.8.0").respond(200, json={"tag_name": "v0.8.0"})

    assert api.get_release_by_tag("acme", "widgets", "v1.0.0") == Release(
        "v1.0.0", "https://github.test/r"
    )
    assert api.get_release_by_tag("acme", "widgets", "v0.9.0") is None
    with pytest.raises(GitHubHttpError, match="has no html_url"):
        api.get_release_by_tag("acme", "widgets", "v0.8.0")


@respx.mock(base_url=BASE)
def test_get_tag_commit_returns_the_commit_of_a_lightweight_tag(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{REPO}/git/ref/tags/v1.0.0").respond(
        200, json={"object": {"type": "commit", "sha": SHA}}
    )
    respx_mock.get(f"{REPO}/git/ref/tags/v9").respond(404)

    assert api.get_tag_commit("acme", "widgets", "v1.0.0") == SHA
    assert api.get_tag_commit("acme", "widgets", "v9") is None


@respx.mock(base_url=BASE)
def test_get_tag_commit_resolves_an_annotated_tag(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{REPO}/git/ref/tags/v2").respond(
        200, json={"object": {"type": "tag", "sha": "t" * 40}}
    )
    respx_mock.get(f"{REPO}/git/tags/{'t' * 40}").respond(
        200, json={"object": {"type": "commit", "sha": SHA}}
    )

    assert api.get_tag_commit("acme", "widgets", "v2") == SHA


@respx.mock(base_url=BASE)
def test_get_tag_commit_rejects_odd_answers(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{REPO}/git/ref/tags/none").respond(200, json={"ref": "x"})
    respx_mock.get(f"{REPO}/git/ref/tags/nosha").respond(200, json={"object": {"type": "commit"}})
    respx_mock.get(f"{REPO}/git/ref/tags/loop").respond(
        200, json={"object": {"type": "tag", "sha": "l" * 40}}
    )
    respx_mock.get(f"{REPO}/git/tags/{'l' * 40}").respond(
        200, json={"object": {"type": "tag", "sha": "l" * 40}}
    )

    with pytest.raises(GitHubHttpError, match="tag none has no object"):
        api.get_tag_commit("acme", "widgets", "none")
    with pytest.raises(GitHubHttpError, match="tag nosha has no object"):
        api.get_tag_commit("acme", "widgets", "nosha")
    with pytest.raises(GitHubHttpError, match="more than 5 tag objects"):
        api.get_tag_commit("acme", "widgets", "loop")


@respx.mock(base_url=BASE)
def test_create_release_sends_target_commitish_and_make_latest(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(f"{REPO}/releases").respond(201, json={"html_url": "https://r.test/1"})

    url = api.create_release(
        "acme", "widgets", "v1.0.0", SHA, "v1.0.0", "notes", latest=False, prerelease=True
    )
    api.create_release(
        "acme", "widgets", "v1.0.1", SHA, "v1.0.1", "n", latest=True, prerelease=False
    )

    assert url == "https://r.test/1"
    assert body_of(route, 0) == {
        "tag_name": "v1.0.0",
        "target_commitish": SHA,
        "name": "v1.0.0",
        "body": "notes",
        "draft": False,
        "prerelease": True,
        "make_latest": "false",
    }
    assert body_of(route, 1)["make_latest"] == "true"


@respx.mock(base_url=BASE)
def test_create_release_is_not_retried_after_a_server_error(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(f"{REPO}/releases").respond(502)

    with pytest.raises(GitHubHttpError, match="502"):
        api.create_release("acme", "widgets", "v1", SHA, "v1", "", latest=True, prerelease=False)

    assert route.call_count == 1


@respx.mock(base_url=BASE)
def test_list_tags_reads_every_matching_ref_in_one_request(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    # matching-refs answers with the whole list and ignores per_page/page: 139 entries, one call.
    refs: list[object] = [{"ref": f"refs/tags/v1.{i}.0"} for i in range(138)]
    refs += [{"ref": "refs/tags/v2.0.0"}, {"ref": "refs/heads/odd"}, {"nope": 1}, "junk"]
    route = respx_mock.get(f"{REPO}/git/matching-refs/tags/v").respond(200, json=refs)

    tags = api.list_tags("acme", "widgets", "v")

    assert len(tags) == 139
    assert tags[0] == "v1.0.0"
    assert tags[-1] == "v2.0.0"
    assert route.call_count == 1
    assert "page" not in route.calls[0].request.url.params


@respx.mock(base_url=BASE)
def test_list_tags_rejects_a_non_list_answer(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{REPO}/git/matching-refs/tags/v").respond(200, json={"ref": "x"})

    with pytest.raises(GitHubHttpError, match="did not answer with a list"):
        api.list_tags("acme", "widgets", "v")


@respx.mock(base_url=BASE)
def test_list_tags_with_an_empty_prefix_has_no_trailing_slash(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(f"{REPO}/git/matching-refs/tags").respond(
        200, json=[{"ref": "refs/tags/1.0.0"}]
    )

    assert api.list_tags("acme", "widgets", "") == ["1.0.0"]
    assert route.calls[0].request.url.path == f"{REPO}/git/matching-refs/tags"


@respx.mock(base_url=BASE)
def test_compare_commits_reads_every_page(api: HttpGitHubApi, respx_mock: respx.MockRouter) -> None:
    def entry(index: int) -> dict[str, object]:
        return {
            "sha": f"{index:040x}",
            "html_url": f"https://c.test/{index}",
            "commit": {"message": f"feat: {index}"},
        }

    route = respx_mock.get(f"{REPO}/compare/v1.0.0...{SHA}").mock(
        side_effect=[
            httpx.Response(
                200, json={"status": "ahead", "total_commits": 3, "commits": [entry(1), entry(2)]}
            ),
            httpx.Response(
                200, json={"status": "ahead", "total_commits": 3, "commits": [entry(3)]}
            ),
        ]
    )

    comparison = api.compare_commits("acme", "widgets", "v1.0.0", SHA)

    assert comparison.status == "ahead"
    assert comparison.total_commits == 3
    assert [c.message for c in comparison.commits] == ["feat: 1", "feat: 2", "feat: 3"]
    assert comparison.commits[0] == CommitInfo(f"{1:040x}", "feat: 1", "https://c.test/1")
    assert route.calls[0].request.url.params["per_page"] == "100"
    assert route.calls[1].request.url.params["page"] == "2"


@respx.mock(base_url=BASE)
def test_compare_commits_drops_commits_an_overlapping_page_repeats(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    def entry(index: int) -> dict[str, object]:
        return {"sha": f"{index:040x}", "commit": {"message": f"fix: {index}"}}

    route = respx_mock.get(f"{REPO}/compare/v1...{SHA}").mock(
        side_effect=[
            httpx.Response(
                200, json={"status": "ahead", "total_commits": 3, "commits": [entry(1), entry(2)]}
            ),
            # the second page starts with a commit the first one already had
            httpx.Response(
                200, json={"status": "ahead", "total_commits": 3, "commits": [entry(2)]}
            ),
            httpx.Response(
                200, json={"status": "ahead", "total_commits": 3, "commits": [entry(3)]}
            ),
        ]
    )

    comparison = api.compare_commits("acme", "widgets", "v1", SHA)

    assert [c.message for c in comparison.commits] == ["fix: 1", "fix: 2", "fix: 3"]
    assert route.call_count == 3


@respx.mock(base_url=BASE)
def test_compare_commits_stops_on_an_empty_page_and_reports_the_shortfall(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{REPO}/compare/v1...{SHA}").mock(
        side_effect=[
            httpx.Response(
                200, json={"status": "ahead", "total_commits": 5, "commits": [{"x": 1}]}
            ),
            httpx.Response(200, json={"status": "ahead", "total_commits": 5, "commits": []}),
        ]
    )

    comparison = api.compare_commits("acme", "widgets", "v1", SHA)

    assert comparison == Comparison("ahead", 5, (CommitInfo("", "", ""),))


@respx.mock(base_url=BASE)
def test_compare_commits_gives_up_after_the_page_ceiling(
    api: HttpGitHubApi, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lktec_bot.adapters.github_http._MAX_COMPARE_PAGES", 2)
    route = respx_mock.get(f"{REPO}/compare/v1...{SHA}").respond(
        200,
        json={"status": "ahead", "total_commits": 9999, "commits": [{"sha": "a" * 40}]},
    )

    comparison = api.compare_commits("acme", "widgets", "v1", SHA)

    # the same commit on both pages counts once; the shortfall stays visible to the caller
    assert route.call_count == 2
    assert comparison.total_commits == 9999
    assert len(comparison.commits) == 1


@respx.mock(base_url=BASE)
def test_compare_commits_of_identical_refs_has_no_commits(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{REPO}/compare/v1...{SHA}").respond(
        200, json={"status": "identical", "total_commits": 0, "commits": []}
    )

    assert api.compare_commits("acme", "widgets", "v1", SHA) == Comparison("identical", 0, ())


@respx.mock(base_url=BASE)
def test_compare_commits_rejects_an_answer_that_is_no_comparison(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{REPO}/compare/v1...{SHA}").respond(200, json=[])

    with pytest.raises(GitHubHttpError, match="did not answer with a comparison"):
        api.compare_commits("acme", "widgets", "v1", SHA)


@respx.mock(base_url=BASE)
def test_list_commits_serves_messages_newest_first(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(f"{REPO}/commits").respond(
        200,
        json=[
            {"sha": "a" * 40, "commit": {"message": "fix: b"}},
            {"sha": "b" * 40, "commit": None, "html_url": None},
        ],
    )

    commits = api.list_commits("acme", "widgets", SHA)

    assert commits == [CommitInfo("a" * 40, "fix: b"), CommitInfo("b" * 40, "")]
    assert route.calls[0].request.url.params["sha"] == SHA


PULL = {
    "number": 5,
    "html_url": "https://p.test/5",
    "state": "open",
    "draft": False,
    "title": "t",
    "body": None,
    "head": {"ref": "release/next", "sha": "c" * 40, "repo": {"full_name": "acme/widgets"}},
    "user": {"login": "bot"},
}


@respx.mock(base_url=BASE)
def test_find_open_pull_request_filters_by_head_and_base(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(f"{REPO}/pulls").mock(
        side_effect=[httpx.Response(200, json=[PULL]), httpx.Response(200, json=[])]
    )

    found = api.find_open_pull_request("acme", "widgets", "release/next", "main")
    missing = api.find_open_pull_request("acme", "widgets", "other", "main")

    assert found is not None
    assert (found.number, found.title, found.body, found.head_ref) == (5, "t", "", "release/next")
    assert missing is None
    params = route.calls[0].request.url.params
    assert (params["head"], params["base"], params["state"]) == (
        "acme:release/next",
        "main",
        "open",
    )


@respx.mock(base_url=BASE)
def test_create_pull_request_posts_and_returns_it(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(f"{REPO}/pulls").respond(201, json=PULL)

    created = api.create_pull_request(
        "acme", "widgets", "release/next", "main", "t", "b", draft=True
    )

    assert created.number == 5
    assert body_of(route) == {
        "title": "t",
        "head": "release/next",
        "base": "main",
        "body": "b",
        "draft": True,
    }


@respx.mock(base_url=BASE)
def test_update_pull_request_sends_only_the_given_fields(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.patch(f"{REPO}/pulls/5").respond(200, json=PULL)

    api.update_pull_request("acme", "widgets", 5, title="new", body=None)
    api.update_pull_request("acme", "widgets", 5, title=None, body="")

    assert body_of(route, 0) == {"title": "new"}
    assert body_of(route, 1) == {"body": ""}


@respx.mock(base_url=BASE)
def test_set_pull_request_draft_uses_the_graphql_mutations(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("/graphql").mock(
        side_effect=[
            httpx.Response(200, json={"data": {"repository": {"pullRequest": {"id": "PR_1"}}}}),
            httpx.Response(200, json={"data": {"convertPullRequestToDraft": {}}}),
            httpx.Response(200, json={"data": {"repository": {"pullRequest": {"id": "PR_1"}}}}),
            httpx.Response(200, json={"data": {"markPullRequestReadyForReview": {}}}),
        ]
    )

    api.set_pull_request_draft("acme", "widgets", 5, True)
    api.set_pull_request_draft("acme", "widgets", 5, False)

    assert "convertPullRequestToDraft" in str(body_of(route, 1)["query"])
    assert body_of(route, 1)["variables"] == {"pullRequestId": "PR_1"}
    assert "markPullRequestReadyForReview" in str(body_of(route, 3)["query"])
