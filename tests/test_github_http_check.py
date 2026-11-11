# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The HTTP adapter's calls for check runs."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from lktec_bot.adapters.github_http import GitHubHttpError, HttpGitHubApi
from lktec_bot.ports.github_api import CheckRun

BASE = "https://api.github.test"
REPO = "/repos/acme/widgets"
SHA = "0123456789abcdef0123456789abcdef01234567"
RUNS = f"{REPO}/commits/{SHA}/check-runs"


@pytest.fixture
def api() -> HttpGitHubApi:
    return HttpGitHubApi("ghs_token", base_url=BASE, sleep=lambda _: None, max_attempts=2)


def body_of(route: respx.Route, call: int = 0) -> dict[str, object]:
    return json.loads(route.calls[call].request.content)  # type: ignore[no-any-return]


def payload(run_id: int = 7, **changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": run_id,
        "html_url": f"https://github.test/runs/{run_id}",
        "name": "lock files",
        "status": "completed",
        "conclusion": "success",
        "details_url": "https://example.com/d",
        "external_id": "ext-1",
        "output": {"title": "T", "summary": "S", "text": None},
        "app": {"id": 42, "slug": "my-bot"},
    }
    return {**base, **changes}


@respx.mock(base_url=BASE)
def test_list_check_runs_maps_the_fields(api: HttpGitHubApi, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(RUNS).respond(200, json={"total_count": 1, "check_runs": [payload()]})

    runs = api.list_check_runs("acme", "widgets", SHA, "lock files")

    assert runs == [
        CheckRun(
            id=7,
            html_url="https://github.test/runs/7",
            name="lock files",
            status="completed",
            conclusion="success",
            details_url="https://example.com/d",
            title="T",
            summary="S",
            text=None,
            app_id=42,
            app_slug="my-bot",
            external_id="ext-1",
        )
    ]
    params = route.calls[0].request.url.params
    assert params["check_name"] == "lock files"
    assert params["per_page"] == "100"
    assert params["page"] == "1"
    assert route.calls[0].request.headers["authorization"] == "Bearer ghs_token"


@respx.mock(base_url=BASE)
def test_list_check_runs_tolerates_missing_app_output_and_conclusion(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    bare = {"id": 3, "status": "in_progress", "conclusion": None, "app": None, "output": None}
    respx_mock.get(RUNS).respond(200, json={"check_runs": [bare, "junk"]})

    runs = api.list_check_runs("acme", "widgets", SHA, "n")

    assert runs == [
        CheckRun(
            id=3,
            html_url="",
            name="",
            status="in_progress",
            conclusion=None,
            details_url=None,
            title=None,
            summary=None,
            text=None,
            app_id=None,
            app_slug="",
            external_id=None,
        )
    ]


@respx.mock(base_url=BASE)
def test_list_check_runs_follows_the_pages(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(RUNS)
    route.side_effect = [
        httpx.Response(
            200, json={"total_count": 101, "check_runs": [payload(i) for i in range(100)]}
        ),
        httpx.Response(200, json={"total_count": 101, "check_runs": [payload(100)]}),
    ]

    runs = api.list_check_runs("acme", "widgets", SHA, "lock files")

    assert [entry.id for entry in runs] == list(range(101))
    assert route.calls[1].request.url.params["page"] == "2"


@pytest.mark.parametrize("answer", [[], {"message": "x"}, {"check_runs": "nope"}])
@respx.mock(base_url=BASE)
def test_list_check_runs_rejects_an_answer_without_the_list(
    api: HttpGitHubApi, respx_mock: respx.MockRouter, answer: object
) -> None:
    respx_mock.get(RUNS).respond(200, json=answer)

    with pytest.raises(GitHubHttpError, match="did not answer with a list"):
        api.list_check_runs("acme", "widgets", SHA, "n")


@respx.mock(base_url=BASE)
def test_list_check_runs_rejects_an_entry_without_an_id(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(RUNS).respond(200, json={"check_runs": [{"name": "x"}]})

    with pytest.raises(GitHubHttpError, match="has no id"):
        api.list_check_runs("acme", "widgets", SHA, "n")


@respx.mock(base_url=BASE)
def test_list_check_runs_maps_http_errors(api: HttpGitHubApi, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(RUNS).respond(403, json={"message": "Resource not accessible by integration"})

    with pytest.raises(GitHubHttpError, match="403") as excinfo:
        api.list_check_runs("acme", "widgets", SHA, "n")

    assert excinfo.value.status == 403


@respx.mock(base_url=BASE)
def test_create_check_run_posts_the_full_payload(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(f"{REPO}/check-runs").respond(201, json=payload())
    annotations = [{"path": "a.py", "start_line": 1, "end_line": 1, "message": "m"}]

    created = api.create_check_run(
        "acme",
        "widgets",
        name="lock files",
        sha=SHA,
        status="completed",
        conclusion="success",
        details_url="https://example.com/d",
        external_id="ext-1",
        title="T",
        summary="S",
        text="details",
        annotations=annotations,
    )

    assert created.id == 7
    assert created.app_slug == "my-bot"
    assert body_of(route) == {
        "name": "lock files",
        "head_sha": SHA,
        "status": "completed",
        "conclusion": "success",
        "details_url": "https://example.com/d",
        "external_id": "ext-1",
        "output": {"title": "T", "summary": "S", "text": "details", "annotations": annotations},
    }


@respx.mock(base_url=BASE)
def test_create_check_run_leaves_out_what_is_not_given(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(f"{REPO}/check-runs").respond(201, json=payload())

    api.create_check_run(
        "acme",
        "widgets",
        name="n",
        sha=SHA,
        status="in_progress",
        conclusion=None,
        details_url=None,
        external_id=None,
        title="T",
        summary="S",
        text=None,
        annotations=[],
    )

    assert body_of(route) == {
        "name": "n",
        "head_sha": SHA,
        "status": "in_progress",
        "output": {"title": "T", "summary": "S"},
    }


@respx.mock(base_url=BASE)
def test_create_check_run_is_not_retried_after_a_server_error(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(f"{REPO}/check-runs").respond(502, text="bad gateway")

    with pytest.raises(GitHubHttpError, match="502"):
        api.create_check_run(
            "acme",
            "widgets",
            name="n",
            sha=SHA,
            status="completed",
            conclusion="success",
            details_url=None,
            external_id=None,
            title="T",
            summary="S",
            text=None,
            annotations=[],
        )

    assert route.call_count == 1


@respx.mock(base_url=BASE)
def test_create_check_run_maps_validation_errors(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(f"{REPO}/check-runs").respond(422, json={"message": "Validation Failed"})

    with pytest.raises(GitHubHttpError, match=r"422: .*Validation Failed") as excinfo:
        api.create_check_run(
            "acme",
            "widgets",
            name="n",
            sha=SHA,
            status="completed",
            conclusion="success",
            details_url=None,
            external_id=None,
            title="T",
            summary="S",
            text=None,
            annotations=[],
        )

    assert excinfo.value.status == 422


@respx.mock(base_url=BASE)
def test_update_check_run_patches_only_what_is_given(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.patch(f"{REPO}/check-runs/7").respond(200, json=payload())

    updated = api.update_check_run(
        "acme",
        "widgets",
        7,
        status="completed",
        conclusion="failure",
        title="T",
        summary="S",
        text="",
    )

    assert updated.id == 7
    assert body_of(route) == {
        "status": "completed",
        "conclusion": "failure",
        "output": {"title": "T", "summary": "S", "text": ""},
    }


@respx.mock(base_url=BASE)
def test_update_check_run_can_send_annotations_alone_with_title_and_summary(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.patch(f"{REPO}/check-runs/7").respond(200, json=payload())
    annotations = [{"path": "a.py", "start_line": 1, "end_line": 1, "message": "m"}]

    api.update_check_run("acme", "widgets", 7, title="T", summary="S", annotations=annotations)

    assert body_of(route) == {"output": {"title": "T", "summary": "S", "annotations": annotations}}


@respx.mock(base_url=BASE)
def test_update_check_run_with_the_url_and_id_and_no_output(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.patch(f"{REPO}/check-runs/7").respond(200, json=payload())

    api.update_check_run(
        "acme", "widgets", 7, details_url="https://example.com/d", external_id="ext-1"
    )

    assert body_of(route) == {"details_url": "https://example.com/d", "external_id": "ext-1"}


@respx.mock(base_url=BASE)
def test_update_check_run_is_not_retried_on_5xx_and_maps_404(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    flaky = respx_mock.patch(f"{REPO}/check-runs/7").respond(500, text="boom")
    respx_mock.patch(f"{REPO}/check-runs/8").respond(404, json={"message": "Not Found"})

    with pytest.raises(GitHubHttpError, match="500"):
        api.update_check_run("acme", "widgets", 7, title="T", summary="S")
    with pytest.raises(GitHubHttpError, match="404") as excinfo:
        api.update_check_run("acme", "widgets", 8, title="T", summary="S")

    assert flaky.call_count == 1
    assert excinfo.value.status == 404


@respx.mock(base_url=BASE)
def test_list_head_check_runs_reads_every_run_of_the_head_across_pages(
    api: HttpGitHubApi, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(RUNS)
    route.side_effect = [
        httpx.Response(
            200, json={"total_count": 101, "check_runs": [payload(i) for i in range(100)]}
        ),
        httpx.Response(200, json={"total_count": 101, "check_runs": [payload(100)]}),
    ]

    runs = api.list_head_check_runs("acme", "widgets", SHA)

    assert [entry.id for entry in runs] == list(range(101))
    params = route.calls[0].request.url.params
    assert "check_name" not in params
    assert params["filter"] == "latest"
