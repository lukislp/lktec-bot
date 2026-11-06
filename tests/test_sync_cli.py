"""``lktec-bot sync commit``: output, outputs file and exit codes."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from lktec_bot.application.gate import Gate
from lktec_bot.application.verbs import handlers
from lktec_bot.cli import app
from lktec_bot.domain.policy import POLICY_PATH
from lktec_bot.summary import mask_secret
from tests.fakes import FakeApiFactory, FakeRepository, FakeTokenProvider
from tests.test_sync import HEAD, POLICY, handover

runner = CliRunner()


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRepository:
    repository = FakeRepository(
        branches={"main": HEAD}, ref_files={(HEAD, "k8s/web.yaml"): "image: x:1\n"}
    )
    tokens = FakeTokenProvider()
    factory = FakeApiFactory({POLICY_PATH: POLICY}, repository)

    def fake_build_gate(env: Mapping[str, str] | None = None) -> Gate:
        return Gate(tokens, factory, handlers(), mask=mask_secret)

    monkeypatch.setattr("lktec_bot.cli.build_gate", fake_build_gate)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return repository


def outputs(tmp_path: Path) -> dict[str, str]:
    lines = (tmp_path / "output.txt").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines)


def run_commit(handed: Path, *extra: str) -> Result:
    return runner.invoke(
        app, ["sync", "commit", "-R", "acme/widgets", "--changed-dir", str(handed), *extra]
    )


def test_sync_commit_commits_and_reports(state: FakeRepository, tmp_path: Path) -> None:
    handed = handover(tmp_path / "handover", {"k8s/web.yaml": b"image: x:2\n"})

    result = run_commit(handed, "--version", "2.0.0", "--branch", "main")

    assert result.exit_code == 0, result.output
    assert "committed: created commit " in result.stdout
    assert "url: https://github.com/acme/widgets/commit/" in result.stdout
    values = outputs(tmp_path)
    assert values["result"] == "ok"
    assert values["decision"] == "committed"
    assert values["reason"] == values["message"]
    assert values["url"].startswith("https://github.com/acme/widgets/commit/")
    assert "**COMMITTED** `sync commit`" in (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert state.commits[0][1] == "chore(deploy): image 2.0.0 [skip ci]"


def test_sync_commit_without_changes_is_ok(state: FakeRepository, tmp_path: Path) -> None:
    handed = handover(tmp_path / "handover", {"k8s/web.yaml": b"image: x:1\n"})

    result = run_commit(handed)

    assert result.exit_code == 0
    assert "unchanged: nothing to commit" in result.stdout
    assert outputs(tmp_path)["decision"] == "unchanged"
    assert state.commits == []


def test_sync_commit_outside_the_paths_exits_3(state: FakeRepository, tmp_path: Path) -> None:
    handed = handover(tmp_path / "handover", {"src/app.py": b"x"})

    result = run_commit(handed)

    assert result.exit_code == 3
    assert outputs(tmp_path)["result"] == "denied"
    assert state.commits == []


def test_sync_commit_on_another_branch_exits_3(state: FakeRepository, tmp_path: Path) -> None:
    handed = handover(tmp_path / "handover", {"k8s/web.yaml": b"new\n"})

    result = run_commit(handed, "--branch", "develop")

    assert result.exit_code == 3
    assert state.commits == []


def test_sync_commit_refuses_a_bad_handover_with_exit_1(
    state: FakeRepository, tmp_path: Path
) -> None:
    handed = handover(tmp_path / "handover", {"k8s/web.yaml": b"x"}, manifest=["../x"])

    result = run_commit(handed)

    assert result.exit_code == 1
    assert outputs(tmp_path)["result"] == "error"


def test_sync_commit_prints_revoke_warnings(
    monkeypatch: pytest.MonkeyPatch, state: FakeRepository, tmp_path: Path
) -> None:
    tokens = FakeTokenProvider(failing_revokes=5)
    factory = FakeApiFactory({POLICY_PATH: POLICY}, state)
    monkeypatch.setattr(
        "lktec_bot.cli.build_gate", lambda env=None: Gate(tokens, factory, handlers())
    )
    handed = handover(tmp_path / "handover", {"k8s/web.yaml": b"image: x:1\n"})

    result = run_commit(handed)

    assert result.exit_code == 0
    assert "warning: token not revoked" in result.stderr


def test_sync_commit_usage_errors(state: FakeRepository, tmp_path: Path) -> None:
    handed = handover(tmp_path / "handover", {"k8s/web.yaml": b"x"})

    bad_repository = runner.invoke(
        app, ["sync", "commit", "-R", "nope", "--changed-dir", str(handed)]
    )
    missing_dir = runner.invoke(
        app, ["sync", "commit", "-R", "acme/widgets", "--changed-dir", "nowhere"]
    )

    assert bad_repository.exit_code == 2
    assert missing_dir.exit_code == 2


def test_sync_commit_skips_when_the_branch_changed_the_file_since_base(
    state: FakeRepository, tmp_path: Path
) -> None:
    base = "b" * 40
    state.ref_files[(base, "k8s/web.yaml")] = "older\n"
    handed = handover(tmp_path / "handover", {"k8s/web.yaml": b"new\n"})

    result = run_commit(handed, "--base-sha", base)

    assert result.exit_code == 0, result.output
    assert "skipped: the branch moved on" in result.stdout
    assert outputs(tmp_path)["decision"] == "skipped"
    assert outputs(tmp_path)["result"] == "ok"
    assert state.commits == []
