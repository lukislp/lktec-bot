# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from lktec_bot.application.gate import VerbOutcome, VerbRequest, VerbResult
from lktec_bot.domain.permissions import Verb
from lktec_bot.ports.github_api import Release
from tests.fakes import FakeRepository
from tests.verb_helpers import HEAD, Harness

RELEASE_POLICY = "version: 1\nmodules:\n  release:\n    enabled: true\n"
OTHER = "f" * 40
URL = "https://github.com/acme/widgets/releases/tag/v1.2.3"


def harness(state: FakeRepository | None = None) -> tuple[Harness, FakeRepository]:
    repository = state or FakeRepository()
    return Harness(repository, policy=RELEASE_POLICY), repository


def run(h: Harness, **args: object) -> VerbResult:
    base: dict[str, object] = {"tag": "v1.2.3", "sha": HEAD, "name": "v1.2.3", "body": "notes"}
    return h.run(Verb.RELEASE, {**base, **args}, module="release")


def test_release_creates_the_release_with_contents_write_only() -> None:
    h, state = harness()

    result = run(h)

    assert result.outcome is VerbOutcome.OK
    assert result.message == f"created release v1.2.3 at {HEAD}"
    assert result.url == URL
    assert dict(result.outputs) == {"decision": "created", "tag": "v1.2.3"}
    assert h.tokens.requests[1][2] == {"contents": "write"}
    assert state.releases_created == [("v1.2.3", HEAD, "v1.2.3", "notes", True, False)]
    assert h.verb_api_calls == ["get_release_by_tag", "get_tag_commit", "create_release"]


def test_release_is_idempotent_for_an_existing_release_at_the_same_commit() -> None:
    h, state = harness(
        FakeRepository(tags={"v1.2.3": HEAD}, releases={"v1.2.3": Release("v1.2.3", URL)})
    )

    result = run(h)

    assert result.ok
    assert result.message == f"release v1.2.3 already exists at {HEAD}"
    assert result.url == URL
    assert dict(result.outputs) == {"decision": "unchanged", "tag": "v1.2.3"}
    assert state.releases_created == []


def test_release_for_a_tag_that_already_points_at_the_commit_creates_only_the_release() -> None:
    h, state = harness(FakeRepository(tags={"v1.2.3": HEAD}))

    result = run(h)

    assert result.ok
    assert dict(result.outputs)["decision"] == "created"
    assert len(state.releases_created) == 1


@pytest.mark.parametrize("with_release", [False, True])
def test_release_never_moves_a_tag(with_release: bool) -> None:
    releases = {"v1.2.3": Release("v1.2.3", URL)} if with_release else {}
    h, state = harness(FakeRepository(tags={"v1.2.3": OTHER}, releases=releases))

    result = run(h)

    assert result.outcome is VerbOutcome.ERROR
    assert f"tag v1.2.3 already points at {OTHER}, not at {HEAD}; a tag is never moved" in (
        result.message
    )
    assert dict(result.outputs) == {"decision": "error", "tag": "v1.2.3"}
    assert state.releases_created == []


def test_release_without_its_tag_is_an_error() -> None:
    h, state = harness(FakeRepository(releases={"v1.2.3": Release("v1.2.3", URL)}))

    result = run(h)

    assert result.outcome is VerbOutcome.ERROR
    assert "a release for v1.2.3 exists but the tag does not" in result.message
    assert state.releases_created == []


def test_prerelease_is_never_marked_latest() -> None:
    h, state = harness()

    result = run(h, prerelease=True, latest=True)

    assert result.ok
    assert state.releases_created[0][4:] == (False, True)


def test_no_latest_is_passed_through() -> None:
    h, state = harness()

    run(h, latest=False)

    assert state.releases_created[0][4:] == (False, False)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"tag": ""}, "is not a usable tag name"),
        ({"tag": "-x"}, "is not a usable tag name"),
        ({"tag": "v1 2"}, "is not a usable tag name"),
        ({"tag": "v1..2"}, "is not a usable tag name"),
        ({"tag": "v1.2.3\n"}, "is not a usable tag name"),
        ({"tag": "v1/"}, "is not a usable tag name"),
        ({"tag": "v1.lock"}, "is not a usable tag name"),
        ({"sha": "main"}, "is not a full commit id"),
        ({"sha": HEAD[:7]}, "is not a full commit id"),
        ({"name": "  "}, "--name is required"),
    ],
)
def test_release_refuses_bad_arguments_before_any_call(
    args: dict[str, object], message: str
) -> None:
    h, state = harness()

    result = run(h, **args)

    assert result.outcome is VerbOutcome.ERROR
    assert message in result.message
    assert dict(result.outputs)["decision"] == "error"
    assert state.releases_created == []
    assert h.verb_api_calls == []


def test_release_without_a_body_has_empty_notes() -> None:
    h, state = harness()

    result = h.run(Verb.RELEASE, {"tag": "v1.2.3", "sha": HEAD, "name": "v1.2.3"}, module="release")

    assert result.ok
    assert state.releases_created[0][3] == ""


def test_release_is_denied_for_a_module_that_may_not_release() -> None:
    h = Harness(FakeRepository())

    result = h.gate.run(
        VerbRequest("acme", "widgets", "automerge", Verb.RELEASE, args={"tag": "v1", "sha": HEAD})
    )

    assert result.outcome is VerbOutcome.DENIED
