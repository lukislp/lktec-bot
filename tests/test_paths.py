# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from lktec_bot.domain.paths import repo_path_problem


@pytest.mark.parametrize(
    "path",
    ["uv.lock", "src/app/packages.lock.json", "docs/a b.md", ".github/dependabot.yml", "a/.b/c"],
)
def test_plain_relative_paths_are_fine(path: str) -> None:
    assert repo_path_problem(path) is None


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("", "empty"),
        ("a\x00b", "control character"),
        ("a\nb", "control character"),
        ("a\\b", "backslash"),
        ("/etc/passwd", "absolute"),
        ("C:/Windows", "absolute"),
        ("c:foo", "absolute"),
        ("../x", "'..' segment"),
        ("a/../b", "'..' segment"),
        ("a//b", "empty"),
        ("a/", "empty"),
        ("./a", "'.' or"),
        (".git/config", "points into .git"),
        ("sub/.GIT/hooks", "points into .git"),
        (".github/workflows", "no 'workflows' permission"),
        (".github/workflows/ci.yml", "no 'workflows' permission"),
        (".GitHub/Workflows/ci.yml", "no 'workflows' permission"),
    ],
)
def test_unsafe_paths_are_refused_with_a_reason(path: str, reason: str) -> None:
    problem = repo_path_problem(path)

    assert problem is not None
    assert reason in problem


def test_a_neighbour_of_the_workflows_directory_is_fine() -> None:
    assert repo_path_problem(".github/workflows-notes/readme.md") is None
    assert repo_path_problem(".github/lktec-bot.yml") is None
