# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lktec_bot.application.gate import Gate
from lktec_bot.application.verbs import handlers
from lktec_bot.cli import app
from lktec_bot.summary import mask_secret
from tests.fakes import BOT, FakeApiFactory, FakeRepository, FakeTokenProvider
from tests.verb_helpers import LOCKFILES

runner = CliRunner()

BASE = ["open-issue", "-R", "acme/widgets", "-m", "lockfiles"]


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> FakeRepository:
    repository = FakeRepository()
    factory = FakeApiFactory({".github/lktec-bot.yml": LOCKFILES}, repository)

    def fake_build_gate(env: Mapping[str, str] | None = None) -> Gate:
        return Gate(FakeTokenProvider(), factory, handlers(), mask=mask_secret)

    monkeypatch.setattr("lktec_bot.cli.build_gate", fake_build_gate)
    for name in ("GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT", "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)
    return repository


def test_open_issue_command_opens_then_updates_the_marked_issue(
    state: FakeRepository, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    body = tmp_path / "body.md"
    body.write_text("image tag not found\n", encoding="utf-8")
    args = [*BASE, "--title", "Deploy bump", "--body-file", str(body), "--marker", "deploy"]

    first = runner.invoke(app, [*args, "--label", "ci", "--label", "bug"])
    second = runner.invoke(app, [*args, "--label", "ci", "--no-reopen"])

    assert first.exit_code == 0, first.output
    assert "ok: opened #1 with marker 'deploy'" in first.output
    assert second.exit_code == 0, second.output
    assert "ok: #1 with marker 'deploy' is already up to date" in second.output
    assert state.issues[1].labels == ("ci", "bug")
    assert state.issues[1].author_login == BOT
    assert output.read_text(encoding="utf-8").endswith(
        "url=https://github.com/acme/widgets/issues/1\ndecision=unchanged\nnumber=1\n"
    )


def test_open_issue_command_without_a_body_is_a_usage_error(state: FakeRepository) -> None:
    result = runner.invoke(app, [*BASE, "--title", "x"])

    assert result.exit_code == 2, result.output
    assert "--body or --body-file is required" in result.output
    assert state.issues == {}


def test_open_issue_command_validation_failure_exits_1(state: FakeRepository) -> None:
    result = runner.invoke(app, [*BASE, "--title", "x", "--body", "b", "--label", ""])

    assert result.exit_code == 1, result.output
    assert "--label" in result.output
    assert state.issues == {}
