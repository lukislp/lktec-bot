# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Table and property tests for the version reader: the shapes Dependabot (and Renovate) write
for pip, npm, NuGet, Docker and GitHub Actions, and the ones that must stay unreadable."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lktec_bot.application.modules.automerge.versions import (
    MAX_LENGTH,
    UpdateKind,
    Version,
    kind_of_change,
    kind_of_versions,
    most_severe,
    parse_version,
)

PATCH, MINOR, MAJOR, DIGEST = (
    UpdateKind.PATCH,
    UpdateKind.MINOR,
    UpdateKind.MAJOR,
    UpdateKind.DIGEST,
)

PARSED = [
    # plain releases, any number of parts, optional v prefix
    ("1", Version(0, (1,), "")),
    ("1.2.3", Version(0, (1, 2, 3), "")),
    ("1.2.3.4", Version(0, (1, 2, 3, 4), "")),
    ("v4", Version(0, (4,), "")),
    ("V4.2", Version(0, (4, 2), "")),
    ("v3.34.5", Version(0, (3, 34, 5), "")),
    ("2024.5.1", Version(0, (2024, 5, 1), "")),
    # PEP 440: post, pre, dev, local, epoch, in normalised and permitted spellings
    ("1.4.1.post1", Version(0, (1, 4, 1), ".post1")),
    ("1.4.1post1", Version(0, (1, 4, 1), "post1")),
    ("1.4.1-post.1", Version(0, (1, 4, 1), "-post.1")),
    ("1.0-1", Version(0, (1, 0), "-1")),
    ("1.0.rev2", Version(0, (1, 0), ".rev2")),
    ("2.0.0rc1", Version(0, (2, 0, 0), "rc1")),
    ("2.0.0RC1", Version(0, (2, 0, 0), "RC1")),
    ("1.0a2", Version(0, (1, 0), "a2")),
    ("1.0b", Version(0, (1, 0), "b")),
    ("1.0.0-beta.3", Version(0, (1, 0, 0), "-beta.3")),
    ("1.0.0.alpha1", Version(0, (1, 0, 0), ".alpha1")),
    ("1.0.0c1", Version(0, (1, 0, 0), "c1")),
    ("1.0.0preview2", Version(0, (1, 0, 0), "preview2")),
    ("1.0_beta", Version(0, (1, 0), "_beta")),
    ("1.0.dev0", Version(0, (1, 0), ".dev0")),
    ("1.0.0rc1.post2.dev3", Version(0, (1, 0, 0), "rc1.post2.dev3")),
    ("1.0+ubuntu1", Version(0, (1, 0), "+ubuntu1")),
    ("1.0.post1+local.7", Version(0, (1, 0), ".post1+local.7")),
    ("1!2.0", Version(1, (2, 0), "")),
    ("v1!2.0.post1", Version(1, (2, 0), ".post1")),
    # SemVer pre-release and build metadata
    ("1.2.3-rc.1", Version(0, (1, 2, 3), "-rc.1")),
    ("1.2.3-0.3.7", Version(0, (1, 2, 3), "-0.3.7")),
    ("1.2.3-x.7.z.92", Version(0, (1, 2, 3), "-x.7.z.92")),
    ("1.2.3+build.5", Version(0, (1, 2, 3), "+build.5")),
    ("2.0.0-beta.1+exp.sha.5114f85", Version(0, (2, 0, 0), "-beta.1+exp.sha.5114f85")),
    ("v5.0.0-alpha", Version(0, (5, 0, 0), "-alpha")),
    # Docker tag variants
    ("3.13-slim", Version(0, (3, 13), "-slim")),
    ("3.13-slim-bookworm", Version(0, (3, 13), "-slim-bookworm")),
    ("22-alpine3.20", Version(0, (22,), "-alpine3.20")),
    ("8.10.3-alpine", Version(0, (8, 10, 3), "-alpine")),
]


@pytest.mark.parametrize(("text", "version"), PARSED)
def test_known_shapes_are_parsed(text: str, version: Version) -> None:
    assert parse_version(text) == version


NOT_VERSIONS = [
    "",
    "   ",
    "latest",
    "stable",
    "v",
    "vv1",
    ".1",
    "1.",
    "1..2",
    "-1.0",
    # commit shas without backticks: the release is followed by hex that is no known suffix
    "3d3c42e",
    "a1b2c3d",
    "2f17fc0",
    "78387bc",
    # a suffix that is neither PEP 440 nor SemVer
    "1.0~rc1",
    "1.0.0-",
    "1.0.0+",
    "1.0.0-rc..1",
    "1.0!2",
    "1.0 2.0",
    "^1.2.3",
    "~1.2",
    ">=1.0",
    "1.x",
    "1" * (MAX_LENGTH + 1),
]


@pytest.mark.parametrize("text", NOT_VERSIONS)
def test_unknown_shapes_are_not_versions(text: str) -> None:
    assert parse_version(text) is None


def test_surrounding_whitespace_is_not_part_of_the_version() -> None:
    assert parse_version(" 1.0\n") == Version(0, (1, 0), "")


KINDS = [
    # the incident of 2026-11-16: a PEP 440 post-release as the old version
    ("1.4.1.post1", "1.5.0", MINOR),
    ("1.4.1.post1", "1.4.2", PATCH),
    ("1.4.1", "1.4.1.post1", PATCH),
    ("1.9.0.post3", "2.0.0", MAJOR),
    # pre-releases and dev releases: only the numbers decide
    ("2.0.0rc1", "2.0.0", PATCH),
    ("1.9.0", "2.0.0rc1", MAJOR),
    ("1.0.dev0", "1.0", PATCH),
    ("1.0.dev0", "1.1.dev0", MINOR),
    ("8.4.0", "8.4.1-rc.1", PATCH),
    ("8.4.0-rc.1", "8.4.0", PATCH),
    # an epoch change is a major whatever the numbers say
    ("1.0", "1!1.0", MAJOR),
    ("1!1.0", "1!1.1", MINOR),
    # local versions and build metadata
    ("1.0+ubuntu1", "1.0+ubuntu2", PATCH),
    ("22.11.0+build.5", "22.12.0", MINOR),
    # v prefixes (actions, the studylife pin) and mixed prefixes
    ("v3.34.4", "v3.34.5", PATCH),
    ("v3.34.5", "v3.35.0", MINOR),
    ("v3.35.0", "v4.0.0", MAJOR),
    ("v4", "v4.2.1", MINOR),
    ("4", "v5", MAJOR),
    # NuGet four-part versions
    ("1.2.3.4", "1.2.3.5", PATCH),
    ("4.154.0.1", "4.155.0", MINOR),
    # Docker tags
    ("3.13-slim", "3.14-slim", MINOR),
    ("22-alpine3.20", "22-alpine3.21", PATCH),
    ("8.10.3-alpine", "9.0.0-alpine", MAJOR),
    # fewer parts on one side
    ("1.2", "1.2.0", PATCH),
    ("1", "1.0.1", PATCH),
    ("0.13", "0.14", MINOR),
]


@pytest.mark.parametrize(("old", "new", "kind"), KINDS)
def test_kind_of_versions(old: str, new: str, kind: UpdateKind) -> None:
    assert kind_of_versions(old, new) is kind


@pytest.mark.parametrize(
    ("old", "new"),
    [("latest", "1.0.0"), ("1.0.0", "latest"), ("3d3c42e", "a1b2c3d"), ("1.0!2", "1.0")],
)
def test_kind_of_versions_is_none_when_one_side_is_not_understood(old: str, new: str) -> None:
    assert kind_of_versions(old, new) is None


def test_kind_of_change_compares_epoch_major_and_minor_only() -> None:
    assert kind_of_change(Version(0, (1, 2, 3)), Version(0, (1, 2, 99, 1))) is PATCH
    assert kind_of_change(Version(0, (1, 2)), Version(0, (1, 3))) is MINOR
    assert kind_of_change(Version(0, (1,)), Version(1, (1,))) is MAJOR


def test_most_severe_orders_digest_patch_minor_major() -> None:
    assert most_severe([]) is None
    assert most_severe([DIGEST, PATCH]) is PATCH
    assert most_severe([MAJOR, DIGEST, MINOR]) is MAJOR


@given(st.text())
def test_the_reader_never_raises_and_a_version_has_a_release(text: str) -> None:
    version = parse_version(text)
    if version is not None:
        assert version.release
        assert all(part >= 0 for part in version.release)
        assert len(text.strip()) <= MAX_LENGTH


@given(st.text(alphabet="0123456789.v!+-_abcdeilnoprstuvABCRV ", max_size=40))
def test_a_version_compared_with_itself_is_a_patch(text: str) -> None:
    if parse_version(text) is None:
        assert kind_of_versions(text, text) is None
    else:
        assert kind_of_versions(text, text) is PATCH
