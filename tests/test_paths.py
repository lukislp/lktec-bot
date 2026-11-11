# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from lktec_bot.domain import paths
from lktec_bot.domain.paths import generated_path_problem, repo_path_problem
from lktec_bot.domain.policy import POLICY_PATH


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


@pytest.mark.parametrize(
    "path", ["THIRD-PARTY-NOTICES.md", "apps/mobile/public/third-party-notices.json", "a b/c.md"]
)
def test_an_exact_file_outside_dot_github_may_be_generated(path: str) -> None:
    assert generated_path_problem(path) is None


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("*.md", "is a pattern"),
        ("docs/**/NOTICES.md", "is a pattern"),
        ("NOTICES?.md", "is a pattern"),
        ("NOTICES[1].md", "is a pattern"),
        ("{a,b}.md", "is a pattern"),
        ("../NOTICES.md", "'..' segment"),
        ("/NOTICES.md", "absolute"),
        ("docs/", "empty"),
        (".git/hooks/pre-commit", "points into .git"),
        ("a=b.md", "characters outside"),
        ("noticesä.md", "characters outside"),
        (".github/workflows/ci.yml", "no 'workflows' permission"),
        (".github", "under .github/"),
        (".github/dependabot.yml", "under .github/"),
        (".GitHub/CODEOWNERS", "under .github/"),
        (POLICY_PATH, "own policy file"),
        (POLICY_PATH.upper(), "own policy file"),
    ],
)
def test_a_path_that_may_not_be_generated_is_refused_with_a_reason(path: str, reason: str) -> None:
    problem = generated_path_problem(path)

    assert problem is not None
    assert reason in problem


def test_a_neighbour_of_dot_github_may_be_generated() -> None:
    assert generated_path_problem(".githubx/notes.md") is None
    assert generated_path_problem("docs/.github/notes.md") is None


def test_the_refused_policy_file_is_the_one_the_gate_reads() -> None:
    assert POLICY_PATH.lower() == paths._POLICY_FILE
