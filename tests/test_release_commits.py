# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Commit parsing, release rules and version arithmetic of the ``release`` module."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lktec_bot.application.modules.release.commits import (
    ParsedCommit,
    bump_for,
    bumped,
    format_version,
    highest_version,
    parse_commit,
    parse_version,
    version_of_tag,
)
from lktec_bot.application.modules.release.options import Bump, Rule
from lktec_bot.ports.github_api import CommitInfo

SHA = "a" * 40


def commit(message: str) -> CommitInfo:
    return CommitInfo(SHA, message)


def parsed(message: str) -> ParsedCommit:
    result = parse_commit(commit(message))
    assert result is not None
    return result


@pytest.mark.parametrize(
    ("message", "kind", "scope", "subject", "breaking"),
    [
        ("feat: add x", "feat", None, "add x", False),
        ("fix(api): handle y", "fix", "api", "handle y", False),
        ("feat(ui)!: drop z", "feat", "ui", "drop z", True),
        ("refactor!: rename", "refactor", None, "rename", True),
        ("chore(deps): bump a (#12)", "chore", "deps", "bump a (#12)", False),
        ("fix(): empty scope is no scope", "fix", None, "empty scope is no scope", False),
        ("feat: x\n\nbody text\n", "feat", None, "x", False),
        ("feat: x\r\n\r\nBREAKING CHANGE: gone\r\n", "feat", None, "x", True),
        ("fix: x\n\nBREAKING-CHANGE: also gone", "fix", None, "x", True),
    ],
)
def test_parse_commit_reads_conventional_headers(
    message: str, kind: str, scope: str | None, subject: str, breaking: bool
) -> None:
    result = parsed(message)

    assert (result.type, result.scope, result.subject, result.breaking) == (
        kind,
        scope,
        subject,
        breaking,
    )
    assert result.sha == SHA


@pytest.mark.parametrize(
    "message",
    [
        "Merge pull request #5 from acme/feature",
        "Merge branch 'main' into feature",
        "Merge remote-tracking branch 'origin/main'",
        "Update README",
        "feat add x",
        "feat:no space",
        "feat(unclosed: x",
        "",
        "  \n",
    ],
)
def test_parse_commit_ignores_what_is_not_conventional(message: str) -> None:
    assert parse_commit(commit(message)) is None


def test_squash_commit_is_analysed_by_its_headline_and_keeps_the_pr_reference() -> None:
    squash = "feat(api): add endpoint (#42)\n\n* feat: add endpoint\n* fix: typo\n"

    result = parsed(squash)

    assert (result.type, result.scope, result.subject) == ("feat", "api", "add endpoint (#42)")
    assert result.breaking is False


def test_breaking_notes_are_collected_with_their_continuation_lines() -> None:
    message = (
        "feat: x\n\n"
        "BREAKING CHANGE: first line\n"
        "second line\n"
        "\n"
        "unrelated paragraph\n"
        "BREAKING-CHANGE: another\n"
    )

    result = parsed(message)

    assert result.breaking
    assert result.notes == ("first line second line", "another")


def test_breaking_marker_without_text_and_bang_use_the_subject() -> None:
    assert parsed("feat!: drop it").notes == ("drop it",)
    assert parsed("feat: x\n\nBREAKING CHANGE:\n").notes == ("x",)
    assert parsed("feat: x\n\nBREAKING CHANGE:\n").breaking
    assert parsed("feat: x").notes == ()


def test_a_breaking_line_must_start_the_line() -> None:
    assert parsed("feat: x\n\n* BREAKING CHANGE: nope").breaking is False


@pytest.mark.parametrize(
    ("message", "bump"),
    [
        ("feat: a", Bump.MINOR),
        ("fix: a", Bump.PATCH),
        ("perf: a", Bump.PATCH),
        ("docs: a", Bump.NONE),
        ("chore(deps): a", Bump.NONE),
        ("refactor: a", Bump.NONE),
        ("revert: a", Bump.PATCH),
        ("revert(api): a", Bump.PATCH),
        ("feat!: a", Bump.MAJOR),
        ("docs!: a", Bump.MAJOR),
        ("fix: a\n\nBREAKING CHANGE: b", Bump.MAJOR),
    ],
)
def test_default_rules_match_semantic_release(message: str, bump: Bump) -> None:
    assert bump_for(parsed(message), []) is bump


def rule(release: str, **conditions: object) -> Rule:
    return Rule.model_validate({"release": release, **conditions})


def test_rules_are_evaluated_in_order_before_the_defaults() -> None:
    # The mowdeck configuration: breaking is only a minor, refactor a patch, fix(ci) nothing.
    rules = [
        rule("minor", breaking=True),
        rule("patch", type="refactor"),
        rule("none", type="fix", scope="ci"),
    ]

    assert bump_for(parsed("feat!: a"), rules) is Bump.MINOR
    assert bump_for(parsed("refactor: a"), rules) is Bump.PATCH
    assert bump_for(parsed("fix(ci): a"), rules) is Bump.NONE
    assert bump_for(parsed("fix(api): a"), rules) is Bump.PATCH
    assert bump_for(parsed("fix(ci)!: a"), rules) is Bump.MINOR
    assert bump_for(parsed("feat: a"), rules) is Bump.MINOR
    assert bump_for(parsed("docs: a"), rules) is Bump.NONE


def test_the_first_matching_rule_wins_and_a_none_rule_stops_the_defaults() -> None:
    rules = [rule("none", type="feat", scope="wip"), rule("major", type="feat")]

    assert bump_for(parsed("feat(wip): a"), rules) is Bump.NONE
    assert bump_for(parsed("feat(other): a"), rules) is Bump.MAJOR
    assert bump_for(parsed("feat: a"), rules) is Bump.MAJOR


def test_rule_conditions_must_all_match() -> None:
    only_scoped = [rule("major", type="docs", scope="api")]

    assert bump_for(parsed("docs: a"), only_scoped) is Bump.NONE
    assert bump_for(parsed("docs(web): a"), only_scoped) is Bump.NONE
    assert bump_for(parsed("fix(api): a"), only_scoped) is Bump.PATCH
    assert bump_for(parsed("docs(api): a"), only_scoped) is Bump.MAJOR
    non_breaking = [rule("none", breaking=False, type="feat")]
    assert bump_for(parsed("feat: a"), non_breaking) is Bump.NONE
    assert bump_for(parsed("feat!: a"), non_breaking) is Bump.MAJOR


def test_a_rule_needs_a_condition_and_a_known_release() -> None:
    with pytest.raises(ValidationError, match="at least one of type, scope and breaking"):
        rule("patch")
    with pytest.raises(ValidationError):
        rule("huge", type="feat")
    with pytest.raises(ValidationError):
        Rule.model_validate({"type": "feat", "release": "patch", "extra": 1})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.2.3", (1, 2, 3)),
        ("0.0.0", (0, 0, 0)),
        ("10.20.30", (10, 20, 30)),
        ("1.2", None),
        ("01.2.3", None),
        ("1.2.3-rc.1", None),
        ("1.2.3+build", None),
        ("v1.2.3", None),
        ("1.2.3\n", None),
        ("", None),
    ],
)
def test_parse_version_is_strict(text: str, expected: tuple[int, int, int] | None) -> None:
    assert parse_version(text) == expected


def test_version_of_tag_uses_prefix_and_suffix() -> None:
    assert version_of_tag("v1.2.3", "v", "") == (1, 2, 3)
    assert version_of_tag("app/1.2.3-stable", "app/", "-stable") == (1, 2, 3)
    assert version_of_tag("1.2.3", "", "") == (1, 2, 3)
    assert version_of_tag("v1.2.3", "app/", "") is None
    assert version_of_tag("v1.2.3", "v", "-stable") is None
    assert version_of_tag("v1.2.3-rc.1", "v", "") is None
    assert version_of_tag("vv1.2.3", "v", "") is None
    # prefix and suffix may not overlap
    assert version_of_tag("ab", "ab", "b") is None


def test_highest_version_compares_numerically_and_ignores_the_rest() -> None:
    tags = ["v1.9.0", "v1.10.0", "v1.2.3-rc.1", "v2.0.0-beta", "latest", "v1.10", "x1.99.0"]

    assert highest_version(tags, "v", "") == ((1, 10, 0), "v1.10.0")
    assert highest_version(["latest", "v1.0"], "v", "") is None
    assert highest_version([], "v", "") is None


def test_bumped_and_format() -> None:
    assert bumped((1, 2, 3), Bump.MAJOR) == (2, 0, 0)
    assert bumped((1, 2, 3), Bump.MINOR) == (1, 3, 0)
    assert bumped((1, 2, 3), Bump.PATCH) == (1, 2, 4)
    assert bumped((1, 2, 3), Bump.NONE) == (1, 2, 3)
    assert bumped((0, 9, 9), Bump.MAJOR) == (1, 0, 0)
    assert format_version((1, 2, 3)) == "1.2.3"
