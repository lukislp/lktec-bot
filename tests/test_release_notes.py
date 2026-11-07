# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Release notes rendering and the options of the ``release`` module."""

from __future__ import annotations

from datetime import date

import pytest

from lktec_bot.application.modules import MODULES
from lktec_bot.application.modules.release.commits import ParsedCommit, bump_for, parse_commit
from lktec_bot.application.modules.release.notes import render_notes
from lktec_bot.application.modules.release.options import Bump, ReleaseOptions
from lktec_bot.ports.github_api import CommitInfo

DAY = date(2026, 11, 6)
BASE = "https://github.com/acme/widgets"


def commits(options: ReleaseOptions, *messages: str) -> list[tuple[ParsedCommit, Bump]]:
    result: list[tuple[ParsedCommit, Bump]] = []
    for index, message in enumerate(messages):
        analysed = parse_commit(CommitInfo(f"{index:x}" * 40, message))
        if analysed is not None:
            result.append((analysed, bump_for(analysed, options.rules)))
    return result


def render(options: ReleaseOptions, *messages: str, previous: str | None = "v1.0.0") -> str:
    return render_notes(
        server_url="https://github.com/",
        repository="acme/widgets",
        version="1.1.0",
        tag="v1.1.0",
        previous_tag=previous,
        day=DAY,
        commits=commits(options, *messages),
        options=options,
    )


def test_notes_follow_semantic_releases_layout() -> None:
    notes = render(
        ReleaseOptions(),
        "feat(api): add endpoint (#42)",
        "fix: handle nil",
        "perf(db): index it",
        "chore: ignored",
        "Merge pull request #1 from x/y",
    )

    assert notes == (
        f"## [1.1.0]({BASE}/compare/v1.0.0...v1.1.0) (2026-11-06)\n"
        "\n"
        "### Features\n"
        "\n"
        f"* **api:** add endpoint ([#42]({BASE}/pull/42)) ([0000000]({BASE}/commit/{'0' * 40}))\n"
        "\n"
        "### Bug Fixes\n"
        "\n"
        f"* handle nil ([1111111]({BASE}/commit/{'1' * 40}))\n"
        "\n"
        "### Performance Improvements\n"
        "\n"
        f"* **db:** index it ([2222222]({BASE}/commit/{'2' * 40}))\n"
    )


def test_the_first_release_links_to_the_tag_tree() -> None:
    notes = render(ReleaseOptions(), "feat: first", previous=None)

    assert notes.splitlines()[0] == f"## [1.1.0]({BASE}/tree/v1.1.0) (2026-11-06)"


def test_breaking_changes_get_a_section_with_every_note() -> None:
    notes = render(
        ReleaseOptions(),
        "feat(core)!: new api",
        "fix: x\n\nBREAKING CHANGE: first\n\nBREAKING-CHANGE: second",
    )

    tail = notes.split("### BREAKING CHANGES\n\n")[1]
    assert tail == "* **core:** new api\n* first\n* second\n"
    assert "### Features" in notes
    assert "### Bug Fixes" in notes


def test_a_breaking_commit_that_does_not_bump_is_listed_only_as_breaking() -> None:
    options = ReleaseOptions.model_validate(
        {"rules": [{"type": "docs", "breaking": True, "release": "none"}]}
    )

    notes = render(options, "docs!: rewrite", "fix: a")

    assert "### Other changes" not in notes
    assert "### BREAKING CHANGES\n\n* rewrite\n" in notes


def test_a_type_that_bumps_through_a_rule_without_a_heading_goes_under_other_changes() -> None:
    options = ReleaseOptions.model_validate({"rules": [{"type": "refactor", "release": "patch"}]})

    notes = render(options, "refactor(core): tidy", "docs: skipped", "feat: shiny")

    assert "### Other changes\n\n* **core:** tidy (" in notes
    assert "skipped" not in notes
    # headings come in the order of the sections, "Other changes" last
    assert notes.index("### Features") < notes.index("### Other changes")


def test_sections_override_and_extend_the_default_headings() -> None:
    options = ReleaseOptions.model_validate({"sections": {"feat": "New", "docs": "Documentation"}})

    notes = render(options, "feat: a", "docs: b", "fix: c")

    assert "### New\n" in notes
    assert "### Documentation\n" in notes
    assert "### Bug Fixes\n" in notes
    assert "### Features" not in notes
    assert notes.index("### New") < notes.index("### Bug Fixes") < notes.index("### Documentation")


def test_a_type_with_a_heading_is_listed_even_when_a_rule_says_it_does_not_release() -> None:
    options = ReleaseOptions.model_validate(
        {"rules": [{"type": "fix", "scope": "ci", "release": "none"}]}
    )

    notes = render(options, "fix(ci): pin", "feat: a")

    assert "**ci:** pin" in notes


def test_notes_are_deterministic() -> None:
    options = ReleaseOptions()
    messages = ("feat: a", "fix: b", "feat(x): c")

    assert render(options, *messages) == render(options, *messages)


# -- options -----------------------------------------------------------------------------------


def test_defaults() -> None:
    options = ReleaseOptions.model_validate({})

    assert options.mode == "tag"
    assert options.tag_format == "v{version}"
    assert options.branch == "main"
    assert options.first_version == "1.0.0"
    assert options.rules == []
    assert options.sections == {}
    assert (options.prefix, options.suffix) == ("v", "")
    assert options.tag_for("1.2.3") == "v1.2.3"
    assert options.headings()["feat"] == "Features"


def test_every_option_is_accepted() -> None:
    options = ReleaseOptions.model_validate(
        {
            "mode": "tag",
            "tag-format": "app/{version}-stable",
            "branch": "master",
            "first-version": "0.1.0",
            "rules": [{"breaking": True, "release": "minor"}],
            "sections": {"chore": "Chores"},
        }
    )

    assert options.tag_for("2.0.0") == "app/2.0.0-stable"
    assert (options.prefix, options.suffix) == ("app/", "-stable")
    assert options.branch == "master"
    assert options.rules[0].bump is Bump.MINOR


@pytest.mark.parametrize(
    ("options", "problem"),
    [
        ({"mode": "pr"}, "mode 'pr' is not available yet; only 'tag' is supported"),
        ({"mode": "weekly"}, "unknown mode 'weekly'"),
        ({"tag-format": "v"}, "must contain '{version}' exactly once"),
        ({"tag-format": "{version}{version}"}, "exactly once"),
        ({"tag-format": "v {version}"}, "may only contain letters"),
        ({"tag-format": "{version} rc"}, "may only contain letters"),
        ({"tag-format": "v{version}\n"}, "may only contain letters"),
        ({"branch": "-x"}, "must be a plain branch name"),
        ({"branch": "a..b"}, "must be a plain branch name"),
        ({"branch": "a/"}, "must be a plain branch name"),
        ({"branch": "x.lock"}, "must be a plain branch name"),
        ({"branch": ""}, "must be a plain branch name"),
        ({"first-version": "1.0"}, "must be a version like 1.0.0"),
        ({"first-version": "1.0.0-rc.1"}, "must be a version like 1.0.0"),
        ({"first-version": "1.0.0\n"}, "must be a version like 1.0.0"),
        ({"sections": {"fe at": "x"}}, "is not a commit type"),
        ({"sections": {"feat": " "}}, "must be one non-empty line"),
        ({"sections": {"feat": "a\nb"}}, "must be one non-empty line"),
        ({"rules": [{"release": "patch"}]}, "at least one of type, scope and breaking"),
        ({"typo": 1}, "Extra inputs are not permitted"),
        ({"tag_format": "v{version}"}, "Extra inputs are not permitted"),
    ],
)
def test_invalid_options_are_reported(options: dict[str, object], problem: str) -> None:
    problems = MODULES["release"].option_problems(options)

    assert problems
    assert any(problem in text for text in problems), problems
