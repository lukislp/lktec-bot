# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest
from tools.check_yaml import main, problems_in

SHA = "0123456789abcdef0123456789abcdef01234567"


def test_valid_yaml_has_no_problems(tmp_path: Path) -> None:
    path = tmp_path / "policy.yml"
    path.write_text("version: 1\nmodules:\n  automerge:\n    enabled: true\n", encoding="utf-8")

    assert problems_in(path) == []


def test_duplicate_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "dup.yml"
    path.write_text("a: 1\na: 2\n", encoding="utf-8")

    problems = problems_in(path)

    assert len(problems) == 1
    assert "duplicate key 'a'" in problems[0]


def test_top_level_must_be_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "list.yml"
    path.write_text("- one\n- two\n", encoding="utf-8")

    assert problems_in(path) == [f"{path}: top level is not a mapping"]


def test_uses_must_be_pinned_to_a_sha_with_a_version_comment(tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    path = workflows / "ci.yml"
    path.write_text(
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - uses: owner/action@v1\n"
        f"      - uses: owner/action@{SHA}\n"
        f"      - uses: owner/action@{SHA} # v1.2.3\n"
        "      - uses: ./local/action\n"
        "      - uses: docker://alpine:3\n",
        encoding="utf-8",
    )

    assert problems_in(path) == [
        f"{path}:4: owner/action@v1 is not pinned to a commit SHA",
        f"{path}:5: owner/action@{SHA} has no version comment",
    ]


def test_main_returns_one_with_problems_and_zero_when_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.yml"
    bad.write_text("a: 1\na: 2\n", encoding="utf-8")
    good = tmp_path / "good.yml"
    good.write_text("a: 1\n", encoding="utf-8")

    assert main([str(bad)]) == 1
    captured = capsys.readouterr()
    assert "duplicate key" in captured.err
    assert "checked 1 YAML files, 1 problems" in captured.out

    assert main([str(good)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "checked 1 YAML files, 0 problems" in captured.out


def test_main_walks_directories(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    nested = tmp_path / "workflows"
    nested.mkdir()
    (nested / "a.yml").write_text("a: 1\n", encoding="utf-8")
    (nested / "b.yaml").write_text("b: 2\n", encoding="utf-8")
    (nested / "notes.txt").write_text("ignored\n", encoding="utf-8")

    assert main([str(tmp_path)]) == 0
    assert "checked 2 YAML files, 0 problems" in capsys.readouterr().out
