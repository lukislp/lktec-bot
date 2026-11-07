# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
from pathlib import Path

import pytest

from lktec_bot.summary import mask_secret, write_outputs, write_summary


def test_appends_to_the_step_summary_when_set(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    summary.write_text("existing\n", encoding="utf-8")

    write_summary("first", env={"GITHUB_STEP_SUMMARY": str(summary)})
    write_summary("second\n", env={"GITHUB_STEP_SUMMARY": str(summary)})

    assert summary.read_text(encoding="utf-8") == "existing\nfirst\nsecond\n"


def test_prints_when_not_in_actions() -> None:
    out = io.StringIO()

    write_summary("hello", env={}, out=out)

    assert out.getvalue() == "hello\n"


def test_empty_summary_variable_counts_as_unset() -> None:
    out = io.StringIO()

    write_summary("x\n", env={"GITHUB_STEP_SUMMARY": ""}, out=out)

    assert out.getvalue() == "x\n"


def test_summary_lines_never_start_with_a_workflow_command() -> None:
    out = io.StringIO()

    write_summary("ok\n::set-env name=A::b\n\n::warning::x", env={}, out=out)

    assert out.getvalue() == "ok\n: :set-env name=A::b\n\n: :warning::x\n"


def test_outputs_are_appended_one_per_line(tmp_path: Path) -> None:
    output = tmp_path / "output.txt"
    output.write_text("earlier=1\n", encoding="utf-8")

    write_outputs(
        {"result": "ok", "message": "line one\nresult=evil\n::cmd", "url": None},
        env={"GITHUB_OUTPUT": str(output)},
    )

    assert output.read_text(encoding="utf-8") == (
        "earlier=1\nresult=ok\nmessage=line one result=evil : :cmd\nurl=\n"
    )


def test_outputs_are_not_written_outside_actions(tmp_path: Path) -> None:
    write_outputs({"result": "ok"}, env={})
    write_outputs({"result": "ok"}, env={"GITHUB_OUTPUT": ""})

    assert list(tmp_path.iterdir()) == []


def test_mask_secret_emits_the_command_only_in_actions() -> None:
    out = io.StringIO()

    mask_secret("ghs_x", env={"GITHUB_ACTIONS": "true"}, out=out)
    mask_secret("ghs_y", env={}, out=out)
    mask_secret("", env={"GITHUB_ACTIONS": "true"}, out=out)

    assert out.getvalue() == "::add-mask::ghs_x\n"


def test_mask_secret_defaults_to_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    out = io.StringIO()

    mask_secret("ghs_z", out=out)

    assert out.getvalue() == ""
