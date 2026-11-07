# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""``lktec-bot lockfiles commit`` and the ``commit`` verb subcommand: output and exit codes."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from lktec_bot.application.gate import Gate
from lktec_bot.application.verbs import REGISTRY, handlers, load_verbs
from lktec_bot.cli import app, collect_args
from lktec_bot.domain.permissions import Verb
from lktec_bot.domain.policy import POLICY_PATH
from lktec_bot.ports.github_api import RateLimit
from lktec_bot.summary import mask_secret
from tests.fakes import FakeApiFactory, FakeRepository, FakeTokenProvider
from tests.test_lockfiles import BRANCH, ENABLED, HEAD, dependabot_pr, handover

runner = CliRunner()


class Wired:
    def __init__(self, state: FakeRepository, tokens: FakeTokenProvider) -> None:
        self.state = state
        self.tokens = tokens


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Wired:
    state = FakeRepository(
        pull_requests={7: dependabot_pr()},
        branches={BRANCH: HEAD},
        ref_files={(HEAD, "uv.lock"): "old\n"},
    )
    tokens = FakeTokenProvider()
    factory = FakeApiFactory({POLICY_PATH: ENABLED}, state)

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


def run_commit(handed: Path, *extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "lockfiles",
            "commit",
            "-R",
            "acme/widgets",
            "--number",
            "7",
            "--changed-dir",
            str(handed),
            *list(extra),
        ],
    )


def test_lockfiles_commit_commits_and_reports(wired: Wired, tmp_path: Path) -> None:
    handed = handover(tmp_path / "handover", {"uv.lock": b"new\n"})

    result = run_commit(handed, "--head-sha", HEAD)

    assert result.exit_code == 0, result.output
    assert "committed: created commit " in result.stdout
    assert "url: https://github.com/acme/widgets/commit/" in result.stdout
    values = outputs(tmp_path)
    assert values["result"] == "ok"
    assert values["decision"] == "committed"
    assert values["reason"] == values["message"]
    assert values["url"].startswith("https://github.com/acme/widgets/commit/")
    assert "**COMMITTED** `lockfiles commit` #7" in (tmp_path / "summary.md").read_text(
        encoding="utf-8"
    )
    assert wired.state.commits[0][4] == {"uv.lock": b"new\n"}
    assert wired.tokens.closed


def test_lockfiles_commit_with_nothing_to_commit_is_ok(wired: Wired, tmp_path: Path) -> None:
    handed = handover(tmp_path / "handover", {"uv.lock": b"old\n"})

    result = run_commit(handed)

    assert result.exit_code == 0
    assert "unchanged: nothing to commit" in result.stdout
    assert outputs(tmp_path)["result"] == "ok"
    assert wired.state.commits == []


def test_lockfiles_commit_refuses_an_unknown_file_with_exit_1(wired: Wired, tmp_path: Path) -> None:
    handed = handover(tmp_path / "handover", {"evil.sh": b"x"})

    result = run_commit(handed)

    assert result.exit_code == 1
    assert "not the name of a known lock file" in result.stdout
    assert outputs(tmp_path)["result"] == "error"


def test_lockfiles_commit_denied_exits_3(
    monkeypatch: pytest.MonkeyPatch, wired: Wired, tmp_path: Path
) -> None:
    factory = FakeApiFactory({POLICY_PATH: "version: 1\nmodules:\n  ping:\n    enabled: true\n"})
    monkeypatch.setattr(
        "lktec_bot.cli.build_gate", lambda env=None: Gate(FakeTokenProvider(), factory, handlers())
    )
    handed = handover(tmp_path / "handover", {"uv.lock": b"new\n"})

    result = run_commit(handed)

    assert result.exit_code == 3
    assert outputs(tmp_path)["result"] == "denied"


def test_lockfiles_commit_prints_revoke_warnings(wired: Wired, tmp_path: Path) -> None:
    wired.tokens.failing_revokes = 5
    handed = handover(tmp_path / "handover", {"uv.lock": b"old\n"})

    result = run_commit(handed)

    assert result.exit_code == 0
    assert "warning: token not revoked" in result.stderr


def test_lockfiles_commit_usage_errors(wired: Wired, tmp_path: Path) -> None:
    handed = handover(tmp_path / "handover", {"uv.lock": b"new\n"})

    bad_repository = runner.invoke(
        app, ["lockfiles", "commit", "-R", "nope", "--number", "7", "--changed-dir", str(handed)]
    )
    missing_dir = runner.invoke(
        app,
        ["lockfiles", "commit", "-R", "acme/widgets", "--number", "7", "--changed-dir", "nowhere"],
    )

    assert bad_repository.exit_code == 2
    assert missing_dir.exit_code == 2


# -- the commit verb as a subcommand -------------------------------------------------------


def test_the_commit_verb_declares_its_options() -> None:
    spec = load_verbs()[Verb.COMMIT]

    assert [(option.name, option.kind) for option in spec.options] == [
        ("branch", "str"),
        ("from_branch", "str"),
        ("message", "str"),
        ("body", "text"),
        ("add", "multi"),
        ("delete", "multi"),
        ("expected_head", "str"),
    ]
    assert REGISTRY[Verb.COMMIT] is spec


def test_collect_args_turns_repeated_flags_into_lists() -> None:
    spec = load_verbs()[Verb.COMMIT]

    repeated = collect_args(spec, {"branch": "b", "message": "m", "add": ["a=1", "b=2"]})
    empty = collect_args(spec, {"branch": "b", "message": "m", "add": None, "delete": []})
    single = collect_args(spec, {"branch": "b", "message": "m", "delete": "only"})

    assert repeated["add"] == ["a=1", "b=2"]
    assert "add" not in empty
    assert "delete" not in empty
    assert single["delete"] == ["only"]


def test_the_commit_subcommand_commits_repeated_adds(wired: Wired, tmp_path: Path) -> None:
    lock = tmp_path / "local.lock"
    lock.write_bytes(b"content\n")

    result = runner.invoke(
        app,
        [
            "commit",
            "-R",
            "acme/widgets",
            "-m",
            "lockfiles",
            "--branch",
            BRANCH,
            "--message",
            "chore: refresh",
            "--body-file",
            str(lock),
            "--add",
            f"uv.lock={lock}",
            "--add",
            f"sub/uv.lock={lock}",
            "--delete",
            "gone.lock",
            "--expected-head",
            HEAD,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "ok: created commit " in result.stdout
    branch, headline, body, expected, additions, deletions = wired.state.commits[0]
    assert (branch, headline, body, expected) == (BRANCH, "chore: refresh", "content\n", HEAD)
    assert additions == {"uv.lock": b"content\n", "sub/uv.lock": b"content\n"}
    assert deletions == ["gone.lock"]


def test_the_commit_subcommand_creates_a_missing_branch_with_from(
    wired: Wired, tmp_path: Path
) -> None:
    lock = tmp_path / "local.lock"
    lock.write_bytes(b"content\n")

    result = runner.invoke(
        app,
        [
            "commit",
            "-R",
            "acme/widgets",
            "-m",
            "lockfiles",
            "--branch",
            "chore/new",
            "--from",
            BRANCH,
            "--message",
            "chore: add",
            "--add",
            f"uv.lock={lock}",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "new branch from" in result.stdout
    assert wired.state.created_branches == [("chore/new", HEAD)]
    assert wired.state.commits[0][0] == "chore/new"


def test_the_commit_subcommand_reports_a_refused_path_as_an_error(
    wired: Wired, tmp_path: Path
) -> None:
    lock = tmp_path / "local.lock"
    lock.write_bytes(b"x")

    result = runner.invoke(
        app,
        [
            "commit",
            "-R",
            "acme/widgets",
            "-m",
            "lockfiles",
            "--branch",
            BRANCH,
            "--message",
            "m",
            "--add",
            f".github/workflows/ci.yml={lock}",
        ],
    )

    assert result.exit_code == 1
    assert "no 'workflows' permission" in result.stdout
    assert wired.state.commits == []


def test_lockfiles_commit_appends_the_remaining_rate_limit(
    wired: Wired, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeApiFactory, "last_rate_limit", RateLimit(77))
    handed = handover(tmp_path / "handover", {"uv.lock": b"old\n"})

    result = run_commit(handed)

    assert "the pull request head (rate limit remaining: 77)" in result.stdout
