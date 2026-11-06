"""Property-based tests (hypothesis) for the pure input rules of the bot."""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lktec_bot.application.gate import parse_repository, validate_name
from lktec_bot.domain.paths import WORKFLOWS_DIR, repo_path_problem
from lktec_bot.text import escape_markdown, neutralise_commands, sanitize


@given(st.text())
def test_an_accepted_repository_path_is_plain_relative_and_outside_workflows(path: str) -> None:
    if repo_path_problem(path) is not None:
        return
    segments = path.split("/")
    assert not path.startswith("/")
    assert "\\" not in path
    assert all(segment not in ("", ".", "..") for segment in segments)
    assert all(segment.lower() != ".git" for segment in segments)
    assert not path.lower().startswith(WORKFLOWS_DIR)
    assert not any(ord(char) < 0x20 or ord(char) == 0x7F for char in path)


@given(st.text(alphabet=st.characters(blacklist_categories=["Cs"])))
def test_an_accepted_name_has_only_the_allowed_characters(value: str) -> None:
    try:
        name = validate_name("owner", value)
    except ValueError:
        return
    assert name == value
    assert name not in (".", "..")
    assert name
    assert all(char.isascii() and (char.isalnum() or char in "-_.") for char in name)


@given(st.text(alphabet=st.characters(blacklist_categories=["Cs"])))
def test_an_accepted_repository_has_exactly_two_safe_parts(text: str) -> None:
    try:
        owner, repo = parse_repository(text)
    except ValueError:
        return
    assert f"{owner}/{repo}" == text
    assert text.count("/") == 1


@pytest.mark.parametrize("value", ["octo\n", "octo\r\n", "\nocto", "octo cat"])
def test_a_name_with_a_line_break_or_space_is_refused(value: str) -> None:
    # ``$`` in a regular expression also matches before a trailing newline; the check must not.
    with pytest.raises(ValueError, match="may only contain"):
        validate_name("owner", value)


@given(st.text())
def test_sanitised_text_is_one_line_and_has_no_workflow_command(text: str) -> None:
    clean = sanitize(text)
    assert "\n" not in clean
    assert "\r" not in clean
    assert "::" not in clean


@given(st.text())
def test_neutralised_text_never_starts_a_line_with_a_command(text: str) -> None:
    assert not any(line.startswith("::") for line in neutralise_commands(text).split("\n"))


@given(st.text())
def test_escaped_markdown_stays_on_one_line(text: str) -> None:
    assert "\n" not in escape_markdown(text)


def _anchored_validators() -> list[tuple[str, re.Pattern[str]]]:
    from lktec_bot.application.modules import _handover
    from lktec_bot.application.modules.release import commits as release_commits
    from lktec_bot.application.modules.release import options as release_options
    from lktec_bot.application.modules.sync import options as sync_options
    from lktec_bot.application.modules.sync import service as sync_service
    from lktec_bot.application.verbs import _OPTION_NAME
    from lktec_bot.application.verbs import comment as comment_verb
    from lktec_bot.application.verbs import commit as commit_verb
    from lktec_bot.application.verbs import release as release_verb

    candidates = {
        "_handover._SAFE_PATH": _handover._SAFE_PATH,
        "commits._VERSION": release_commits._VERSION,
        "release.options._SEMVER": release_options._SEMVER,
        "release.options._TAG_TEXT": release_options._TAG_TEXT,
        "release.options._BRANCH": release_options._BRANCH,
        "sync.options._BRANCH": sync_options._BRANCH,
        "sync.service._VERSION": sync_service._VERSION,
        "sync.service._BASE_SHA": sync_service._BASE_SHA,
        "verbs._OPTION_NAME": _OPTION_NAME,
        "verbs.comment._MARKER": comment_verb._MARKER,
        "verbs.commit._OID": commit_verb._OID,
        "verbs.release._OID": release_verb._OID,
        "verbs.release._TAG": release_verb._TAG,
    }
    return list(candidates.items())


@pytest.mark.parametrize(("name", "pattern"), _anchored_validators())
def test_validation_patterns_end_at_the_end_of_the_text(
    name: str, pattern: re.Pattern[str]
) -> None:
    # ``$`` also matches before a trailing newline, so a validator ending in it would accept
    # "value\n". Every pattern that validates a whole value is anchored with ``\Z``.
    assert pattern.pattern.endswith("\\Z"), name
