# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The ``sync`` module's opt-in check run: options, the three outcomes, and what stays silent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from lktec_bot.application.modules.sync import service
from lktec_bot.application.modules.sync.options import DEFAULT_CHECK_NAME, SyncOptions
from lktec_bot.application.modules.sync.service import Decision, SyncOutcome
from tests.fakes import FakeGitHubApi
from tests.test_sync import BASE, HEAD, POLICY, Setup, handover, policy

CHECK_POLICY = policy("check: true")


def created(setup: Setup) -> dict[str, Any]:
    """The arguments of the one check run the fake recorded."""
    calls = [call for call in setup.state.check_calls if call[0] == "create"]
    assert len(calls) == 1
    return calls[0][2]


# -- options --------------------------------------------------------------------------------


def test_the_check_is_off_by_default_with_a_default_name() -> None:
    options = SyncOptions.model_validate({"paths": ["k8s"]})

    assert options.check is False
    assert options.check_name == DEFAULT_CHECK_NAME == "lktec-bot / deploy bump"


def test_the_check_options_are_accepted() -> None:
    options = SyncOptions.model_validate(
        {"paths": ["k8s"], "check": True, "check-name": "  deploy / bump  "}
    )

    assert options.check is True
    assert options.check_name == "deploy / bump"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "one non-empty line"),
        ("  ", "one non-empty line"),
        ("a\nb", "one non-empty line"),
        ("x" * 101, "at most 100"),
    ],
)
def test_the_check_name_is_validated(value: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        SyncOptions.model_validate({"paths": ["k8s"], "check-name": value})


def test_a_check_name_of_100_characters_is_fine() -> None:
    assert (
        len(SyncOptions.model_validate({"paths": ["k8s"], "check-name": "x" * 100}).check_name)
        == 100
    )


# -- the three outcomes -----------------------------------------------------------------------


def test_a_committed_run_publishes_a_success(tmp_path: Path) -> None:
    setup = Setup(policy_text=CHECK_POLICY)
    changed = handover(tmp_path, {"k8s/web.yaml": b"image: x:2\n", "k8s/new.yaml": b"n\n"})

    outcome = setup.sync.commit(changed, "2.0.0", "main", HEAD)

    assert outcome.decision is Decision.COMMITTED
    assert outcome.exit_code == 0
    assert outcome.warnings == ()
    args = created(setup)
    assert args["sha"] == HEAD
    assert args["name"] == "lktec-bot / deploy bump"
    assert args["status"] == "completed"
    assert args["conclusion"] == "success"
    assert args["title"] == "deploy bump: 2.0.0 committed"
    assert args["external_id"] == "deploy-bump:2.0.0"
    assert outcome.url is not None
    assert args["details_url"] == outcome.url
    lines = args["summary"].splitlines()
    assert "Branch: `main`" in lines
    assert "Version: `2.0.0`" in lines
    assert "- `k8s/new.yaml`" in lines
    assert "- `k8s/web.yaml`" in lines
    assert f"[Commit]({outcome.url})" in lines
    assert outcome.check_url == "https://github.com/acme/widgets/runs/1"
    assert {"checks": "write"} in setup.permissions()


def test_a_committed_run_without_a_version(tmp_path: Path) -> None:
    setup = Setup(policy_text=CHECK_POLICY)
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    setup.sync.commit(changed, base_sha=HEAD)

    args = created(setup)
    assert args["title"] == "deploy bump committed"
    assert args["external_id"] == "deploy-bump"
    assert "Version: none" in args["summary"]


@pytest.mark.parametrize("version", ["1.0.0+build.5", "1" * 53])
def test_a_version_the_external_id_cannot_hold_is_left_out_of_it(
    tmp_path: Path, version: str
) -> None:
    setup = Setup(policy_text=CHECK_POLICY)
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    outcome = setup.sync.commit(changed, version, base_sha=HEAD)

    assert outcome.decision is Decision.COMMITTED
    assert outcome.warnings == ()
    assert created(setup)["external_id"] == "deploy-bump"


def test_an_unchanged_run_publishes_a_neutral_check(tmp_path: Path) -> None:
    setup = Setup(policy_text=CHECK_POLICY)
    changed = handover(tmp_path, {"k8s/web.yaml": b"image: x:1\n"})

    outcome = setup.sync.commit(changed, "1.0.0", base_sha=HEAD.upper())

    assert outcome.decision is Decision.UNCHANGED
    args = created(setup)
    assert args["sha"] == HEAD
    assert args["conclusion"] == "neutral"
    assert args["title"] == "deploy bump: nothing to change for 1.0.0"
    assert args["details_url"] is None
    assert "Branch: `main`" in args["summary"]
    assert "Version: `1.0.0`" in args["summary"]
    assert "already matched" in args["summary"]
    assert outcome.check_url is not None


def test_an_unchanged_run_without_a_version(tmp_path: Path) -> None:
    setup = Setup(policy_text=CHECK_POLICY)
    changed = handover(tmp_path, {"k8s/web.yaml": b"image: x:1\n"})

    setup.sync.commit(changed, base_sha=HEAD)

    assert created(setup)["title"] == "deploy bump: nothing to change"


def test_an_error_after_the_options_publishes_a_failure(tmp_path: Path) -> None:
    setup = Setup(policy_text=CHECK_POLICY)
    setup.state.branches.clear()
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    outcome = setup.sync.commit(changed, "1.0.0", base_sha=HEAD)

    assert outcome.decision is Decision.ERROR
    assert outcome.exit_code == 1
    args = created(setup)
    assert args["conclusion"] == "failure"
    assert args["title"] == "deploy bump failed"
    assert "branch 'main' does not exist" in args["summary"]
    assert outcome.check_url is not None


def test_a_failing_commit_publishes_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = Setup(policy_text=CHECK_POLICY)
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})
    state = setup.state
    original = FakeGitHubApi.get_branch_head
    reads = 0

    def moving(self: FakeGitHubApi, owner: str, repo: str, branch: str) -> str | None:
        nonlocal reads
        reads += 1
        # The first read resolves the head; the commit then finds the branch moved on.
        head = original(self, owner, repo, branch)
        if reads == 1:
            state.branches[branch] = "f" * 40
        return head

    monkeypatch.setattr(FakeGitHubApi, "get_branch_head", moving)

    outcome = setup.sync.commit(changed, "1.0.0", base_sha=HEAD)

    assert outcome.decision is Decision.ERROR
    assert created(setup)["conclusion"] == "failure"


def test_an_unreadable_handed_over_file_publishes_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = Setup(policy_text=CHECK_POLICY)
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})
    monkeypatch.setattr(service, "local_file", lambda directory, path: directory / "gone")

    outcome = setup.sync.commit(changed, base_sha=HEAD)

    assert outcome.decision is Decision.ERROR
    assert "cannot read a handed-over file" in created(setup)["summary"]


# -- what publishes nothing -------------------------------------------------------------------


def test_denied_runs_publish_nothing(tmp_path: Path) -> None:
    setup = Setup(policy_text=CHECK_POLICY)
    outside = handover(tmp_path / "a", {"src/app.py": b"x"})
    other_branch = handover(tmp_path / "b", {"k8s/web.yaml": b"new\n"})

    assert setup.sync.commit(outside, "1", None, HEAD).decision is Decision.DENIED
    assert setup.sync.commit(other_branch, "1", "develop", HEAD).decision is Decision.DENIED

    assert setup.state.check_calls == []


def test_a_skipped_run_publishes_nothing(tmp_path: Path) -> None:
    setup = Setup(policy_text=CHECK_POLICY)
    setup.state.ref_files[(BASE, "k8s/web.yaml")] = "older\n"
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    outcome = setup.sync.commit(changed, "1", None, BASE)

    assert outcome.decision is Decision.SKIPPED
    assert outcome.check_url is None
    assert setup.state.check_calls == []


def test_errors_before_the_options_were_read_publish_nothing(tmp_path: Path) -> None:
    setup = Setup(policy_text=CHECK_POLICY)
    good = handover(tmp_path / "a", {"k8s/web.yaml": b"new\n"})
    bad = handover(tmp_path / "b", {"k8s/web.yaml": b"x"}, manifest=["../x"])

    results = [
        setup.sync.commit(good, "1", None, "abc"),
        setup.sync.commit(good, "1\nx", None, HEAD),
        setup.sync.commit(bad, "1", None, HEAD),
    ]

    assert [result.decision for result in results] == [Decision.ERROR] * 3
    assert all(result.warnings == () for result in results)
    assert setup.state.check_calls == []


def test_an_unreadable_policy_publishes_nothing(tmp_path: Path) -> None:
    setup = Setup(policy_text="version: 1\nmodules: [")
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    outcome = setup.sync.commit(changed, "1", None, HEAD)

    assert outcome.decision is not Decision.COMMITTED
    assert setup.state.check_calls == []


def test_without_the_option_no_check_is_published(tmp_path: Path) -> None:
    setup = Setup(policy_text=POLICY)
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    outcome = setup.sync.commit(changed, "1", None, HEAD)

    assert outcome.decision is Decision.COMMITTED
    assert outcome.warnings == ()
    assert outcome.check_url is None
    assert setup.state.check_calls == []

    explicit = Setup(policy_text=policy("check: false"))
    explicit.sync.commit(changed, "1", None, HEAD)
    assert explicit.state.check_calls == []


def test_without_a_base_sha_a_warning_says_so(tmp_path: Path) -> None:
    setup = Setup(policy_text=CHECK_POLICY)
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    outcome = setup.sync.commit(changed, "1")

    assert outcome.decision is Decision.COMMITTED
    assert outcome.exit_code == 0
    assert outcome.warnings == ("check: no --base-sha, nothing to attach the check run to",)
    assert outcome.check_url is None
    assert setup.state.check_calls == []


def test_a_committed_run_without_a_commit_url_has_no_details_url() -> None:
    options = SyncOptions.model_validate({"paths": ["k8s"], "check": True})
    run = service._Run(SyncOutcome(Decision.COMMITTED, "done"), options, ("k8s/web.yaml",))

    args = service._check_args(run, options, "1.0.0", HEAD)

    assert "details_url" not in args
    assert "[Commit]" not in str(args["summary"])


# -- a failing check --------------------------------------------------------------------------


def test_a_failing_check_is_only_a_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: FakeGitHubApi, *args: object, **kwargs: object) -> object:
        raise RuntimeError("POST check-runs -> 403: Resource not accessible by integration")

    monkeypatch.setattr(FakeGitHubApi, "create_check_run", refuse)
    setup = Setup(policy_text=CHECK_POLICY)
    changed = handover(tmp_path, {"k8s/web.yaml": b"new\n"})

    outcome = setup.sync.commit(changed, "1.0.0", None, HEAD)

    assert outcome.decision is Decision.COMMITTED
    assert outcome.exit_code == 0
    assert outcome.check_url is None
    assert len(outcome.warnings) == 1
    assert outcome.warnings[0].startswith("check: the check run could not be published")
    assert "403" in outcome.warnings[0]
    assert len(setup.state.commits) == 1


def test_a_second_run_for_the_same_commit_updates_the_check(tmp_path: Path) -> None:
    setup = Setup(policy_text=CHECK_POLICY)
    changed = handover(tmp_path, {"k8s/web.yaml": b"image: x:1\n"})

    first = setup.sync.commit(changed, "1.0.0", None, HEAD)
    setup.state.ref_files[(HEAD, "k8s/web.yaml")] = "older\n"
    second = setup.sync.commit(changed, "1.0.0", None, HEAD.upper())

    assert first.decision is Decision.UNCHANGED
    assert second.decision is Decision.COMMITTED
    assert len(setup.state.check_runs[HEAD]) == 1
    assert [call[0] for call in setup.state.check_calls] == ["create", "update"]


# -- the markdown -----------------------------------------------------------------------------


def test_the_markdown_shows_the_check_run() -> None:
    outcome = SyncOutcome(Decision.COMMITTED, "done", check_url="https://x.test/runs/9")

    assert "Check run: <https://x.test/runs/9>" in outcome.to_markdown("acme/widgets")
    assert "Check run" not in SyncOutcome(Decision.COMMITTED, "done").to_markdown("acme/widgets")
