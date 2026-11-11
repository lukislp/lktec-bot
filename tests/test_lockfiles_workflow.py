# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The wiring of the reusable lockfiles workflow that the security model rests on.

The rules themselves live in the code and are tested there; these tests pin what only the
workflow can get wrong: which job holds the secrets, which job runs the pull request's code and
the generators, what each job's token may do, and that the job with the key never checks out or
runs anything but the bot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "lockfiles.yml"


def _jobs() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs: dict[str, Any] = document["jobs"]
    return jobs


def _text(value: object) -> str:
    return yaml.safe_dump(value, sort_keys=True)


def _steps(job: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _jobs()[job]["steps"]
    return steps


def test_only_the_commit_job_references_a_secret() -> None:
    jobs = _jobs()

    assert set(jobs) == {"source", "regenerate", "commit"}
    assert "secrets." in _text(jobs["commit"])
    assert "secrets." not in _text(jobs["source"])
    assert "secrets." not in _text(jobs["regenerate"])


@pytest.mark.parametrize(
    ("job", "permissions"),
    [
        ("source", {"contents": "read", "actions": "read"}),
        ("regenerate", {"contents": "read"}),
        ("commit", {"contents": "read"}),
    ],
)
def test_every_job_has_a_read_only_token(job: str, permissions: dict[str, str]) -> None:
    assert _jobs()[job]["permissions"] == permissions


def test_the_commit_job_checks_out_the_bot_only_and_runs_only_the_bot() -> None:
    steps = _steps("commit")
    checkouts = [step for step in steps if str(step.get("uses", "")).startswith("actions/checkout")]
    runs = [step["run"] for step in steps if "run" in step]

    assert len(checkouts) == 1
    assert checkouts[0]["with"]["repository"] == "${{ needs.source.outputs.repository }}"
    assert checkouts[0]["with"]["ref"] == "${{ needs.source.outputs.sha }}"
    assert checkouts[0]["with"]["persist-credentials"] is False
    assert len(runs) == 1
    assert runs[0].lstrip().startswith("lktec-bot lockfiles commit")
    assert "inputs.allowed-endpoints" not in _text(_jobs()["commit"])


def test_the_generators_run_in_the_job_without_secrets_and_cannot_fail_it() -> None:
    steps = _steps("regenerate")
    derive = next(step for step in steps if step.get("id") == "derive")
    collect = next(index for index, step in enumerate(steps) if step.get("id") == "collect")
    upload = next(
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    )

    assert "lktec-bot lockfiles derive" in derive["run"]
    assert "--trusted-ref" in derive["run"]
    assert derive["env"]["TRUSTED_REF"] == "origin/${{ github.event.repository.default_branch }}"
    # After the lock files are collected, before the upload; never able to stop the lock files.
    assert collect < steps.index(derive) < upload
    for step_id in ("bot", "setup", "derive"):
        step = next(step for step in steps if step.get("id") == step_id)
        assert step["continue-on-error"] is True
    assert "lktec-bot lockfiles derive" not in _text(_jobs()["commit"])


def test_the_policy_check_reads_the_default_branch_before_any_pull_request_code() -> None:
    steps = _steps("regenerate")
    wants = next(index for index, step in enumerate(steps) if step.get("id") == "wants")
    first_tool = next(
        index for index, step in enumerate(steps) if "setup-dotnet" in str(step.get("uses", ""))
    )

    assert wants < first_tool
    assert "origin/$DEFAULT_BRANCH:.github/lktec-bot.yml" in steps[wants]["run"]


def test_every_job_acts_only_for_dependabot_pull_requests_of_the_repository() -> None:
    jobs = _jobs()

    for job in ("source", "regenerate"):
        condition = jobs[job]["if"]
        assert "github.event.pull_request.user.login == 'dependabot[bot]'" in condition
        assert "github.event.pull_request.head.repo.full_name == github.repository" in condition
    assert jobs["commit"]["needs"] == ["source", "regenerate"]
    assert jobs["regenerate"]["needs"] == "source"
