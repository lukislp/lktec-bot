# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The ``automerge arm`` and ``automerge sweep`` commands: output, summary, exit codes."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lktec_bot.application.gate import Gate
from lktec_bot.application.verbs import handlers
from lktec_bot.cli import app
from lktec_bot.domain.policy import POLICY_PATH
from lktec_bot.ports.github_api import RateLimit
from lktec_bot.summary import mask_secret
from tests.fakes import FakeApiFactory, FakeRepository, FakeTokenProvider
from tests.test_automerge_service import MAJOR, PATCH, POLICY, pr

runner = CliRunner()


class Wired:
    def __init__(self, state: FakeRepository, tokens: FakeTokenProvider) -> None:
        self.state = state
        self.tokens = tokens


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Wired:
    state = FakeRepository(
        pull_requests={
            7: pr(7, title=PATCH),
            8: pr(8, title=MAJOR),
            9: pr(9, author="someone", title="Fix", merge_state="BEHIND"),
        }
    )
    tokens = FakeTokenProvider()
    factory = FakeApiFactory({POLICY_PATH: POLICY}, state)

    def fake_build_gate(env: Mapping[str, str] | None = None) -> Gate:
        return Gate(tokens, factory, handlers(), mask=mask_secret)

    monkeypatch.setattr("lktec_bot.cli.build_gate", fake_build_gate)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return Wired(state, tokens)


def outputs(tmp_path: Path) -> dict[str, str]:
    lines = (tmp_path / "output.txt").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines)


def test_arm_arms_a_routine_pull_request(wired: Wired, tmp_path: Path) -> None:
    result = runner.invoke(app, ["automerge", "arm", "-R", "acme/widgets", "--number", "7"])

    assert result.exit_code == 0, result.output
    assert "armed: patch update: approved, auto-merge (squash) enabled" in result.stdout
    assert "url: https://github.com/acme/widgets/pull/7" in result.stdout
    assert wired.state.log == ["approve #7", "enable-automerge #7 squash"]
    values = outputs(tmp_path)
    assert values["result"] == "ok"
    assert values["decision"] == "armed"
    assert values["reason"] == values["message"]
    assert values["url"] == "https://github.com/acme/widgets/pull/7"
    assert "**ARMED** `automerge arm` #7" in (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert wired.tokens.closed


def test_arm_skips_a_major_with_exit_0(wired: Wired, tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["automerge", "arm", "--repository", "acme/widgets", "--number", "8"]
    )

    assert result.exit_code == 0
    assert "skipped: major update (majors: manual)" in result.stdout
    assert outputs(tmp_path)["decision"] == "skipped"
    assert outputs(tmp_path)["result"] == "ok"
    assert wired.state.log == []


def test_arm_denied_through_the_command_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wired: Wired
) -> None:
    factory = FakeApiFactory({POLICY_PATH: "version: 1\nmodules:\n  ping:\n    enabled: true\n"})
    monkeypatch.setattr(
        "lktec_bot.cli.build_gate", lambda env=None: Gate(FakeTokenProvider(), factory, handlers())
    )

    result = runner.invoke(app, ["automerge", "arm", "-R", "acme/widgets", "--number", "7"])

    assert result.exit_code == 3
    assert "denied: module 'automerge' is not declared" in result.stdout
    assert outputs(tmp_path)["result"] == "denied"


def test_arm_error_exits_1_and_prints_warnings(wired: Wired, tmp_path: Path) -> None:
    result = runner.invoke(app, ["automerge", "arm", "-R", "acme/widgets", "--number", "99"])

    assert result.exit_code == 1
    assert "error: automerge could not read" in result.stdout
    assert outputs(tmp_path)["result"] == "error"


def test_arm_prints_revoke_warnings_on_stderr(wired: Wired) -> None:
    wired.tokens.failing_revokes = 5

    result = runner.invoke(app, ["automerge", "arm", "-R", "acme/widgets", "--number", "7"])

    assert result.exit_code == 0
    assert result.stderr.count("warning: token not revoked") > 0


@pytest.mark.parametrize("repository", ["no-slash", "a/../b", ""])
def test_commands_reject_a_bad_repository(wired: Wired, repository: str) -> None:
    for command in (["arm", "--number", "7"], ["sweep"]):
        result = runner.invoke(app, ["automerge", *command, "-R", repository])

        assert result.exit_code == 2


def test_commands_reject_a_bad_number_and_a_missing_repository(wired: Wired) -> None:
    assert runner.invoke(app, ["automerge", "arm", "-R", "a/b", "--number", "0"]).exit_code == 2
    assert runner.invoke(app, ["automerge", "arm", "--number", "7"]).exit_code == 2
    assert runner.invoke(app, ["automerge", "sweep"]).exit_code == 2


def test_commands_report_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LKTEC_BOT_TOKEN_PROVIDER", "nope")  # the real build_gate reads this

    for command in (["arm", "--number", "7"], ["sweep"]):
        result = runner.invoke(app, ["automerge", *command, "-R", "acme/widgets"])

        assert result.exit_code == 2
        assert "not a known token provider" in result.stderr


def test_sweep_prints_a_line_per_pull_request_and_writes_the_table(
    wired: Wired, tmp_path: Path
) -> None:
    result = runner.invoke(app, ["automerge", "sweep", "-R", "acme/widgets"])

    assert result.exit_code == 0, result.output
    assert "#7 (dependabot[bot], CLEAN): patch update: approved, auto-merge (squash) enabled" in (
        result.stdout
    )
    assert "#8 (dependabot[bot], -): left for a person (major update (majors: manual))" in (
        result.stdout
    )
    assert "#9 (someone, BEHIND): updated onto its base" in result.stdout
    assert (
        "ok: armed 1, ready 0, re-approved 0, updated 1, recreate needed 0, errors 0"
        in result.stdout
    )
    assert wired.state.log == ["approve #7", "enable-automerge #7 squash", "update-branch #9"]
    values = outputs(tmp_path)
    assert (values["result"], values["armed"], values["updated"]) == ("ok", "1", "1")
    assert (values["recreated"], values["errors"], values["ready"]) == ("0", "0", "0")
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "| #9 | someone | BEHIND | updated onto its base |" in summary


def test_arm_of_a_mergeable_pull_request_is_green_with_the_decision_ready(
    wired: Wired, tmp_path: Path, verb_sleeps: list[float]
) -> None:
    wired.state.auto_merge_refusals[7] = ["clean"] * 6

    result = runner.invoke(app, ["automerge", "arm", "-R", "acme/widgets", "--number", "7"])

    assert result.exit_code == 0, result.output
    assert (
        "ready: patch update: approved; already mergeable, needs a person to merge" in result.stdout
    )
    values = outputs(tmp_path)
    assert (values["result"], values["decision"]) == ("ok", "ready")
    assert wired.state.log == ["approve #7"]
    assert verb_sleeps == [10.0] * 5


def test_sweep_counts_a_mergeable_pull_request_as_ready_and_not_as_an_error(
    wired: Wired, tmp_path: Path
) -> None:
    wired.state.auto_merge_refusals[7] = ["clean"] * 6

    result = runner.invoke(app, ["automerge", "sweep", "-R", "acme/widgets"])

    assert result.exit_code == 0, result.output
    assert "#7 (dependabot[bot], CLEAN): approved; already mergeable, needs a person to merge" in (
        result.stdout
    )
    assert (
        "ok: armed 0, ready 1, re-approved 0, updated 1, recreate needed 0, errors 0"
        in result.stdout
    )
    values = outputs(tmp_path)
    assert (values["armed"], values["ready"], values["errors"]) == ("0", "1", "0")


def test_sweep_prints_revoke_warnings(wired: Wired) -> None:
    wired.tokens.failing_revokes = 5

    result = runner.invoke(app, ["automerge", "sweep", "-R", "acme/widgets"])

    assert result.exit_code == 0
    assert "warning: token not revoked" in result.stderr


def test_sweep_denied_exits_3(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wired: Wired
) -> None:
    factory = FakeApiFactory({})
    monkeypatch.setattr(
        "lktec_bot.cli.build_gate", lambda env=None: Gate(FakeTokenProvider(), factory, handlers())
    )

    result = runner.invoke(app, ["automerge", "sweep", "-R", "acme/widgets"])

    assert result.exit_code == 3
    assert "denied: " in result.stdout
    assert "missing" in result.stdout
    assert outputs(tmp_path)["result"] == "denied"


def test_sweep_error_exits_1(wired: Wired, tmp_path: Path) -> None:
    wired.tokens.permissions_by_mint = {1: None}

    result = runner.invoke(app, ["automerge", "sweep", "-R", "acme/widgets"])

    assert result.exit_code == 1
    assert "error: read token refused" in result.stdout
    assert outputs(tmp_path)["result"] == "error"


def test_the_help_lists_both_commands() -> None:
    result = runner.invoke(app, ["automerge", "--help"])

    assert result.exit_code == 0
    assert "arm" in result.stdout
    assert "sweep" in result.stdout


def test_arm_and_sweep_append_the_remaining_rate_limit(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeApiFactory, "last_rate_limit", RateLimit(4321, 1_700_000_000))

    arm = runner.invoke(app, ["automerge", "arm", "-R", "acme/widgets", "--number", "7"])
    sweep = runner.invoke(app, ["automerge", "sweep", "-R", "acme/widgets"])

    assert "approved, auto-merge (squash) enabled (rate limit remaining: 4321)" in arm.stdout
    assert "errors 0 (rate limit remaining: 4321)" in sweep.stdout


def test_the_rate_limit_suffix_is_left_out_when_unknown(wired: Wired) -> None:
    result = runner.invoke(app, ["automerge", "sweep", "-R", "acme/widgets"])

    assert "rate limit" not in result.stdout
