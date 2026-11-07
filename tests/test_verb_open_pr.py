# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lktec_bot.application.gate import Gate, VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.modules import ModuleSpec
from lktec_bot.application.verbs import handlers
from lktec_bot.cli import app
from lktec_bot.domain.permissions import Verb, as_token_request, permissions_for
from lktec_bot.domain.policy import POLICY_PATH
from lktec_bot.ports.github_api import PullRequest
from tests.fakes import FakeApiFactory, FakeRepository, FakeTokenProvider
from tests.verb_helpers import Harness

# No shipped module may run open-pr yet; the tests use a module of their own.
ADDON = ModuleSpec("addon", frozenset({Verb.OPEN_PR}))
POLICY = "version: 1\nmodules:\n  addon:\n    enabled: true\n"
URL = "https://github.com/acme/widgets/pull/"


def existing(**changes: object) -> PullRequest:
    base = PullRequest(
        number=5,
        html_url=f"{URL}5",
        state="open",
        draft=False,
        head_sha="a" * 40,
        title="old title",
        body="old body",
        head_ref="release/next",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


class Setup:
    def __init__(self, state: FakeRepository | None = None) -> None:
        self.state = state or FakeRepository()
        self.tokens = FakeTokenProvider()
        self.factory = FakeApiFactory({POLICY_PATH: POLICY}, self.state)
        self.gate = Gate(self.tokens, self.factory, handlers(), modules={"addon": ADDON})

    def run(self, **args: object) -> VerbResult:
        base: dict[str, object] = {"head": "release/next", "base": "main", "title": "chore: next"}
        request = VerbRequest("acme", "widgets", "addon", Verb.OPEN_PR, args={**base, **args})
        result = self.gate.run(request)
        if len(self.tokens.requests) == 2:
            assert self.tokens.requests[1][2] == {"pull_requests": "write"}
            assert self.tokens.requests[1][2] == as_token_request(permissions_for(Verb.OPEN_PR))
        assert all(token.revoked for token in self.tokens.minted)
        return result


def with_open_pr(**changes: object) -> Setup:
    return Setup(FakeRepository(pull_requests={5: existing(**changes)}, pull_bases={5: "main"}))


def test_opens_a_pull_request_when_none_exists() -> None:
    s = Setup()

    result = s.run(body="the body", draft=True)

    assert result.outcome is VerbOutcome.OK
    assert result.message == "opened #1 from release/next into main"
    assert result.url == f"{URL}1"
    assert dict(result.outputs) == {"decision": "opened", "number": "1"}
    assert s.state.pulls_created == [("release/next", "main", "chore: next", "the body", True)]


def test_opens_with_an_empty_body_and_no_draft_by_default() -> None:
    s = Setup()

    s.run()

    assert s.state.pulls_created == [("release/next", "main", "chore: next", "", False)]


def test_a_pull_request_for_another_base_does_not_count() -> None:
    s = Setup(FakeRepository(pull_requests={5: existing()}, pull_bases={5: "develop"}))

    result = s.run()

    assert dict(result.outputs)["decision"] == "opened"
    assert len(s.state.pulls_created) == 1


def test_a_closed_pull_request_does_not_count() -> None:
    s = Setup(FakeRepository(pull_requests={5: existing(state="closed")}, pull_bases={5: "main"}))

    assert dict(s.run().outputs)["decision"] == "opened"


def test_unchanged_when_everything_matches() -> None:
    s = with_open_pr(title="chore: next", body="same")

    result = s.run(body="same")

    assert result.ok
    assert result.message == "#5 from release/next into main is already up to date"
    assert dict(result.outputs) == {"decision": "unchanged", "number": "5"}
    assert result.url == f"{URL}5"
    assert s.state.pulls_updated == []
    assert s.state.draft_changes == []
    assert s.state.pulls_created == []


def test_a_missing_body_leaves_the_existing_body_alone() -> None:
    s = with_open_pr(title="chore: next")

    result = s.run()

    assert dict(result.outputs)["decision"] == "unchanged"
    assert s.state.pulls_updated == []


def test_updates_the_title_only() -> None:
    s = with_open_pr(body="same")

    result = s.run(body="same")

    assert dict(result.outputs) == {"decision": "updated", "number": "5"}
    assert result.message == "updated #5 (title)"
    assert s.state.pulls_updated == [(5, "chore: next", None)]


def test_updates_the_body_only_and_can_clear_it() -> None:
    s = with_open_pr(title="chore: next")

    result = s.run(body="")

    assert result.message == "updated #5 (body)"
    assert s.state.pulls_updated == [(5, None, "")]


def test_updates_title_and_body_in_one_call() -> None:
    s = with_open_pr()

    result = s.run(body="new")

    assert result.message == "updated #5 (title, body)"
    assert s.state.pulls_updated == [(5, "chore: next", "new")]


def test_converts_to_draft_and_back_through_the_draft_call() -> None:
    to_draft = with_open_pr(title="chore: next")
    ready = with_open_pr(title="chore: next", draft=True)

    first = to_draft.run(draft=True)
    second = ready.run()

    assert first.message == "updated #5 (draft)"
    assert to_draft.state.draft_changes == [(5, True)]
    assert second.message == "updated #5 (ready for review)"
    assert ready.state.draft_changes == [(5, False)]
    assert to_draft.state.pulls_updated == []


def test_title_and_draft_changes_are_both_reported() -> None:
    s = with_open_pr()

    result = s.run(draft=True)

    assert result.message == "updated #5 (title, draft)"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"head": ""}, "--head and --base are required"),
        ({"base": ""}, "--head and --base are required"),
        ({"head": "refs/heads/x"}, "must be branch names, not full refs"),
        ({"base": "refs/heads/main"}, "must be branch names, not full refs"),
        ({"head": "fork:branch"}, "without an owner prefix"),
        ({"head": "main"}, "must be different branches"),
        ({"title": " "}, "--title is required and must be one line"),
        ({"title": "a\nb"}, "--title is required and must be one line"),
    ],
)
def test_refuses_bad_arguments_before_any_call(args: dict[str, object], message: str) -> None:
    s = Setup()

    result = s.run(**args)

    assert result.outcome is VerbOutcome.ERROR
    assert message in result.message
    assert dict(result.outputs) == {"decision": "error"}
    assert s.state.pulls_created == []
    assert s.factory.created[1].calls == []  # the verb's client was never used


def test_the_open_pr_subcommand_opens_and_reports_the_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s = Setup()
    monkeypatch.setattr("lktec_bot.cli.build_gate", lambda env=None: s.gate)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))
    body = tmp_path / "body.md"
    body.write_text("from a file\n", encoding="utf-8")
    command = [
        "open-pr",
        "-R",
        "acme/widgets",
        "-m",
        "addon",
        "--head",
        "release/next",
        "--base",
        "main",
        "--title",
        "chore: next",
        "--body-file",
        str(body),
        "--draft",
    ]

    first = CliRunner().invoke(app, command)
    second = CliRunner().invoke(app, command)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert s.state.pulls_created == [("release/next", "main", "chore: next", "from a file\n", True)]
    lines = (tmp_path / "output.txt").read_text(encoding="utf-8").splitlines()
    values = [line.split("=", 1) for line in lines]
    assert ["decision", "opened"] in values
    assert ["decision", "unchanged"] in values
    assert ["number", "1"] in values


def test_open_pr_is_denied_for_the_shipped_modules() -> None:
    h = Harness(FakeRepository())

    result = h.gate.run(
        VerbRequest("acme", "widgets", "automerge", Verb.OPEN_PR, args={"head": "a", "base": "b"})
    )

    assert result.outcome is VerbOutcome.DENIED
    assert "may not run 'open-pr'" in result.message
