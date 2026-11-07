# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Table tests for the classification, ported from the cases the shared ``dependabot-auto-merge``
workflow in ``lukislp/ci-workflows`` documents in its comments and handles in shell."""

from __future__ import annotations

import pytest

from lktec_bot.application.modules.automerge.classify import (
    Classification,
    Eligibility,
    UpdateKind,
    classify,
    evaluate,
    excluded_names,
    kind_of_versions,
    most_severe,
    touches_workflow_files,
)
from lktec_bot.application.modules.automerge.options import AutomergeOptions

PATCH, MINOR, MAJOR, DIGEST = (
    UpdateKind.PATCH,
    UpdateKind.MINOR,
    UpdateKind.MAJOR,
    UpdateKind.DIGEST,
)


def options(**values: object) -> AutomergeOptions:
    return AutomergeOptions.model_validate(values)


# -- single dependency, from the title (the sweep's only input in the old workflow) ----------

TITLES = [
    # semver patch / minor / major
    ("bump ruff from 0.13.0 to 0.13.1", PATCH),
    ("Bump ruff from 0.13.0 to 0.14.0", MINOR),
    ("Bump ruff from 0.13.5 to 1.0.0", MAJOR),
    ("fix(deps): bump httpx from 0.28.0 to 0.28.1 in /services/api", PATCH),
    ("Bump requests from 2.31 to 2.32.3", MINOR),
    ("Bump lodash from 4.17.20 to 4.17.21", PATCH),
    ("Bump @types/node from 22.1.0 to 24.0.0", MAJOR),
    ("Bump actions/checkout from 4 to 5", MAJOR),
    ("Bump actions/checkout from v4 to v4.2.1", MINOR),
    ("Bump actions/checkout from v4.1.1 to v4.1.7", PATCH),
    # suffixes: only the numeric core decides
    ("Bump pytest from 8.4.0 to 8.4.1-rc.1", PATCH),
    ("Bump pytest from 8.4.0-rc.1 to 8.4.0", PATCH),
    ("Bump python from 3.13-slim to 3.14-slim", MINOR),
    ("Bump python from 3.13-slim to 4.0-slim", MAJOR),
    ("Bump node from 22.11.0+build.5 to 22.12.0", MINOR),
    # a 0.x minor is a minor, exactly like fetch-metadata reported it
    ("Bump thing from 0.1.0 to 0.2.0", MINOR),
    # Docker digest refresh of an unchanged tag: both sides hex in backticks
    ("bump python from `78387bc` to `2f17fc0`", DIGEST),
    ("Bump python from `78387bc1d2e3` to `2f17fc0a9b8c` in /docker", DIGEST),
    ("chore(docker): bump node from `0123456` to `fedcba9876543210fedcba9876543210`", DIGEST),
]


@pytest.mark.parametrize(("title", "kind"), TITLES)
def test_single_dependency_titles(title: str, kind: UpdateKind) -> None:
    result = classify(title)

    assert result.kind is kind
    assert result.grouped is False
    assert result.problem is None
    assert len(result.dependencies) == 1


# Anything the old rules did not recognise stayed manual; it must never be armed.
UNREADABLE = [
    # a SHA-to-SHA action bump is not a version (fetch-metadata called it a major)
    "Bump actions/checkout from 3d3c42e to a1b2c3d",
    # hex without backticks that is not a digest refresh
    "bump python from 78387bc to 2f17fc0",
    "Bump the python group with 3 updates",
    "Update README",
    "",
    "Bump foo from latest to stable",
    # a digest needs seven to 64 hex digits on both sides
    "bump python from `abc` to `def`",
]


@pytest.mark.parametrize("title", UNREADABLE)
def test_unreadable_titles_are_manual(title: str) -> None:
    result = classify(title)

    assert result.kind is None
    assert result.problem is not None
    assert evaluate(result, options()).arm is False


def test_the_dependency_is_named_with_old_and_new_version() -> None:
    result = classify("Bump ruff from 0.13.0 to 0.13.1")

    assert [(d.name, d.old, d.new, d.kind) for d in result.dependencies] == [
        ("ruff", "0.13.0", "0.13.1", PATCH)
    ]
    assert result.names == ("ruff",)


def test_digest_dependency_keeps_the_hashes() -> None:
    result = classify("bump python from `78387bc` to `2f17fc0`")

    assert (result.dependencies[0].old, result.dependencies[0].new) == ("78387bc", "2f17fc0")


# -- single dependency, from the body ---------------------------------------------------------


def test_single_dependency_body_with_a_link() -> None:
    body = "Bumps [ruff](https://github.com/astral-sh/ruff) from 0.13.0 to 0.14.1.\n"

    result = classify("Update ruff", body)

    assert result.kind is MINOR
    assert result.names == ("ruff",)


def test_single_dependency_body_of_a_digest_refresh() -> None:
    result = classify("Update python", "Bumps python from `78387bc` to `2f17fc0`.\n")

    assert result.kind is DIGEST


def test_title_and_body_of_the_same_update_count_once() -> None:
    title = "Bump ruff from 0.13.0 to 0.13.1"
    body = "Bumps [ruff](https://x.test/ruff) from 0.13.0 to 0.13.1.\n"

    assert len(classify(title, body).dependencies) == 1


def test_release_notes_are_not_read() -> None:
    body = (
        "Bumps [ruff](https://x.test/ruff) from 0.13.0 to 0.13.1.\n\n"
        "<details>\n<summary>Release notes</summary>\n"
        "Updates `ruff` from 0.13.0 to 9.0.0\n</details>\n"
    )

    result = classify("Bump ruff from 0.13.0 to 0.13.1", body)

    assert result.kind is PATCH  # the notes are the dependency's maintainers' text


# -- group pull requests ----------------------------------------------------------------------

GROUP_TITLE = "Bump the python group with 3 updates"


def group_body(*lines: str) -> str:
    notes = "<details>\n<summary>Release notes</summary>\n<p>whatever</p>\n</details>\n"
    return "Bumps the python group with updates.\n\n" + "\n".join(
        f"{line}\n{notes}" for line in lines
    )


def test_group_is_as_severe_as_its_most_severe_member() -> None:
    body = group_body(
        "Updates `ruff` from 0.13.0 to 0.13.1",
        "Updates `pytest` from 8.3.0 to 8.4.0",
        "Updates `mypy` from 1.16.0 to 1.16.4",
    )

    result = classify(GROUP_TITLE, body)

    assert result.grouped is True
    assert result.kind is MINOR
    assert result.names == ("ruff", "pytest", "mypy")


def test_group_with_a_major_member_is_a_major() -> None:
    body = group_body(
        "Updates `ruff` from 0.13.0 to 0.13.1",
        "Updates `pytest` from 8.3.0 to 9.0.0",
        "Updates `mypy` from 1.16.0 to 1.16.4",
    )

    assert classify(GROUP_TITLE, body).kind is MAJOR


def test_group_of_digest_refreshes_is_a_digest() -> None:
    body = group_body(
        "Updates `python` from `78387bc` to `2f17fc0`",
        "Updates `node` from `abcdef0` to `1234567`",
    )

    result = classify("Bump the docker group with 2 updates", body)

    assert result.kind is DIGEST


def test_group_with_digests_and_patches_is_a_patch() -> None:
    body = group_body(
        "Updates `python` from `78387bc` to `2f17fc0`",
        "Updates `ruff` from 1.0.0 to 1.0.1",
    )

    assert classify("Bump the x group with 2 updates", body).kind is PATCH


def test_group_that_lists_fewer_updates_than_announced_is_manual() -> None:
    body = group_body("Updates `ruff` from 0.13.0 to 0.13.1")

    result = classify(GROUP_TITLE, body)

    assert result.kind is None
    assert result.problem == "the title announces 3 updates but only 1 could be read"
    assert result.names == ("ruff",)  # still known, so an exclude list can act on them


def test_group_title_across_directories_and_lowercase_prefix() -> None:
    body = group_body("Updates `a` from 1.0.0 to 1.0.1", "Updates `b` from 2.0.0 to 2.0.1")

    result = classify("fix(deps): bump the pip group across 2 directories with 2 updates", body)

    assert result.grouped is True
    assert result.kind is PATCH


def test_group_without_any_readable_line_is_manual() -> None:
    result = classify(GROUP_TITLE, "Bumps the python group.\n")

    assert result.kind is None
    assert result.grouped is True


def test_group_without_an_announced_count_is_read_as_it_is() -> None:
    body = group_body("Updates `a` from 1.0.0 to 1.1.0")

    assert classify("Bump the pip group", body).kind is MINOR


def test_crlf_bodies_are_read() -> None:
    body = (
        "Bumps the g group with 2 updates.\r\n\r\n"
        "Updates `a` from 1.0.0 to 1.0.1\r\nUpdates `b` from 1.0.0 to 2.0.0\r\n"
    )

    assert classify("Bump the g group with 2 updates", body).kind is MAJOR


# -- version arithmetic and severity ---------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "kind"),
    [
        ("1", "2", MAJOR),
        ("1.9.9", "2.0.0", MAJOR),
        ("1.2", "1.3", MINOR),
        ("1.2.3", "1.2.4", PATCH),
        ("1.2", "1.2.0", PATCH),
        ("v2.1", "v2.1.5", PATCH),
        ("2024.5.1", "2025.1.1", MAJOR),
        ("1.2.3.4", "1.2.3.5", PATCH),
    ],
)
def test_kind_of_versions(old: str, new: str, kind: UpdateKind) -> None:
    assert kind_of_versions(old, new) is kind


def test_most_severe_orders_digest_patch_minor_major() -> None:
    assert most_severe([]) is None
    assert most_severe([DIGEST]) is DIGEST
    assert most_severe([DIGEST, PATCH]) is PATCH
    assert most_severe([PATCH, MINOR, DIGEST]) is MINOR
    assert most_severe([MAJOR, MINOR]) is MAJOR


# -- the exclude list ------------------------------------------------------------------------


def test_excluded_names_are_exact_and_case_insensitive() -> None:
    assert excluded_names(["Pillow", "ruff"], ["pillow"]) == ["Pillow"]
    assert excluded_names(["pillow-heif"], ["pillow"]) == []
    assert excluded_names(["a", "a", "b"], [" A ", ""]) == ["a"]
    assert excluded_names(["a"], []) == []


def test_excluded_dependency_is_never_armed_whatever_its_kind() -> None:
    result = evaluate(classify("Bump Pillow from 11.0.0 to 11.0.1"), options(exclude=["pillow"]))

    assert result == Eligibility(False, "excluded dependency: Pillow", excluded=True)


def test_a_group_is_skipped_as_a_whole_when_one_member_is_excluded() -> None:
    body = group_body(
        "Updates `ruff` from 0.13.0 to 0.13.1",
        "Updates `mowdeck-control` from 1.0.0 to 1.0.1",
        "Updates `mypy` from 1.16.0 to 1.16.4",
    )

    result = evaluate(classify(GROUP_TITLE, body), options(exclude=["Mowdeck-Control"]))

    assert result.arm is False
    assert result.excluded is True
    assert result.reason == "excluded dependency: mowdeck-control"


def test_unknown_dependencies_with_an_exclude_list_are_excluded() -> None:
    result = evaluate(classify("Update stuff"), options(exclude=["x"]))

    assert result == Eligibility(
        False, "dependencies could not be determined while exclude is set", excluded=True
    )


def test_unknown_dependencies_without_an_exclude_list_are_just_unreadable() -> None:
    result = evaluate(classify("Update stuff"), options())

    assert result.arm is False
    assert result.excluded is False


# -- the decision ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "arm", "reason"),
    [
        ("Bump a from 1.0.0 to 1.0.1", True, "patch update"),
        ("Bump a from 1.0.0 to 1.1.0", True, "minor update"),
        ("bump a from `1234567` to `7654321`", True, "digest update"),
        ("Bump a from 1.0.0 to 2.0.0", False, "major update (majors: manual)"),
    ],
)
def test_default_options(title: str, arm: bool, reason: str) -> None:
    result = evaluate(classify(title), options())

    assert (result.arm, result.reason, result.excluded) == (arm, reason, False)


def test_majors_auto_arms_a_major() -> None:
    result = evaluate(classify("Bump a from 1.0.0 to 2.0.0"), options(majors="auto"))

    assert result == Eligibility(True, "major update", MAJOR)


def test_a_group_says_so_in_the_reason() -> None:
    body = group_body("Updates `a` from 1.0.0 to 1.0.1", "Updates `b` from 1.0.0 to 1.0.2")

    result = evaluate(classify("Bump the g group with 2 updates", body), options())

    assert result.reason == "patch update (group)"


def test_unreadable_pull_request_without_a_problem_text_still_has_a_reason() -> None:
    result = evaluate(Classification((), None, False, None), options())

    assert result.reason == "update kind unknown"


# -- workflow files --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("paths", "touched"),
    [
        ([".github/workflows/ci.yml"], True),
        (["README.md", ".github/workflows/sub/x.yml"], True),
        ([".github/dependabot.yml", "workflows/ci.yml", "src/.github/workflows/x.yml"], False),
        ([".github/actions/setup/action.yml"], False),
        ([], False),
    ],
)
def test_touches_workflow_files(paths: list[str], touched: bool) -> None:
    assert touches_workflow_files(paths) is touched


# -- NuGet groups: Dependabot writes "Updated [Name](url) from a to b." per member ---------------

NUGET_GROUP_TITLE = "Bump the skiasharp group with 2 updates"
NUGET_GROUP_BODY = (
    "Updated [SkiaSharp](https://go.microsoft.com/fwlink/?linkid=868515) from 4.154.0 to 4.155.0.\n"
    "\n"
    "Updated [SkiaSharp.NativeAssets.Linux](https://go.microsoft.com/fwlink/?linkid=868515) "
    "from 4.154.0 to 4.155.0.\n"
    "\n"
    "Dependabot will resolve any conflicts with this PR as long as you don't alter it yourself.\n"
)


def test_nuget_group_body_with_links_is_read() -> None:
    result = classify(NUGET_GROUP_TITLE, NUGET_GROUP_BODY)

    assert result.grouped is True
    assert result.kind is MINOR
    assert result.names == ("SkiaSharp", "SkiaSharp.NativeAssets.Linux")
    assert all(d.old == "4.154.0" and d.new == "4.155.0" for d in result.dependencies)


def test_nuget_group_with_a_major_member_is_a_major() -> None:
    body = NUGET_GROUP_BODY.replace("to 4.155.0.\n\nDependabot", "to 5.0.0.\n\nDependabot")

    assert classify(NUGET_GROUP_TITLE, body).kind is MAJOR
