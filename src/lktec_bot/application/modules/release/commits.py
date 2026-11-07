# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Conventional Commit parsing, release rules and version arithmetic. Pure: no I/O.

Behaves like semantic-release's commit analyzer with the Angular defaults (breaking: major,
``feat``: minor, ``fix``, ``perf`` and ``revert``: patch, everything else: nothing), plus the
policy's own rules evaluated first. Only the first line of a message decides the type, scope and
subject, so a squash-merge commit is analysed by its headline like semantic-release does; a
``BREAKING CHANGE:`` line anywhere in the message still marks it as breaking.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from lktec_bot.application.modules.release.options import SEMVER_CORE, Bump, Rule
from lktec_bot.ports.github_api import CommitInfo

HEADER = re.compile(r"^(\w+)(?:\(([^)]*)\))?(!)?: (.+)$")
_BREAKING_NOTE = re.compile(r"^BREAKING[ -]CHANGE:[ \t]*(.*)$")
_MERGE_PREFIXES = ("Merge pull request", "Merge branch", "Merge remote-tracking branch")
_VERSION = re.compile(rf"^{SEMVER_CORE}\Z")

Version = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class ParsedCommit:
    sha: str
    type: str
    scope: str | None
    subject: str
    breaking: bool
    # Texts of the breaking changes: the ``BREAKING CHANGE:`` notes, or the subject when the
    # commit is marked with ``!`` only.
    notes: tuple[str, ...] = ()


def parse_commit(commit: CommitInfo) -> ParsedCommit | None:
    """The commit as a Conventional Commit, or ``None`` when it contributes nothing.

    Merge commits and headers that do not follow ``type(scope)!: subject`` are ignored.
    """
    lines = commit.message.replace("\r\n", "\n").split("\n")
    header = lines[0].strip()
    if header.startswith(_MERGE_PREFIXES):
        return None
    match = HEADER.match(header)
    if match is None:
        return None
    commit_type, scope, bang, subject = match.groups()
    found = _breaking_notes(lines[1:])
    breaking = bool(bang) or bool(found)
    notes = tuple(text for text in found if text) or ((subject,) if breaking else ())
    return ParsedCommit(commit.sha, commit_type, scope or None, subject, breaking, notes)


def _breaking_notes(body: Sequence[str]) -> list[str]:
    """Each ``BREAKING CHANGE:`` line with the lines that follow it up to a blank line.

    One entry per marker line, an empty string when the marker has no text.
    """
    notes: list[list[str]] = []
    collecting = False
    for line in body:
        match = _BREAKING_NOTE.match(line)
        if match is not None:
            notes.append([match.group(1)] if match.group(1) else [])
            collecting = True
        elif collecting and line.strip():
            notes[-1].append(line.strip())
        else:
            collecting = False
    return [" ".join(parts) for parts in notes]


def bump_for(commit: ParsedCommit, rules: Iterable[Rule]) -> Bump:
    """What ``commit`` contributes: the first matching rule, else the defaults."""
    for rule in rules:
        if rule.type is not None and rule.type != commit.type:
            continue
        if rule.scope is not None and rule.scope != commit.scope:
            continue
        if rule.breaking is not None and rule.breaking != commit.breaking:
            continue
        return rule.bump
    if commit.breaking:
        return Bump.MAJOR
    if commit.type == "feat":
        return Bump.MINOR
    if commit.type in ("fix", "perf", "revert"):
        return Bump.PATCH
    return Bump.NONE


def parse_version(text: str) -> Version | None:
    """A strict ``X.Y.Z`` (no leading zeros, no prerelease, no build metadata), else ``None``."""
    match = _VERSION.fullmatch(text)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)


def version_of_tag(tag: str, prefix: str, suffix: str) -> Version | None:
    """The version inside ``tag`` for a tag format split at ``{version}``, else ``None``."""
    if not tag.startswith(prefix) or not tag.endswith(suffix):
        return None
    if len(tag) < len(prefix) + len(suffix):
        return None
    return parse_version(tag[len(prefix) : len(tag) - len(suffix)])


def highest_version(tags: Iterable[str], prefix: str, suffix: str) -> tuple[Version, str] | None:
    """The highest release version among ``tags`` and its tag name; prereleases and tags that
    do not fit the format are ignored."""
    found = [
        (version, tag)
        for tag in tags
        if (version := version_of_tag(tag, prefix, suffix)) is not None
    ]
    return max(found) if found else None


def bumped(version: Version, bump: Bump) -> Version:
    major, minor, patch = version
    if bump is Bump.MAJOR:
        return major + 1, 0, 0
    if bump is Bump.MINOR:
        return major, minor + 1, 0
    if bump is Bump.PATCH:
        return major, minor, patch + 1
    return version
