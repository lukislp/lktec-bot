# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Release notes in semantic-release's style. Pure and deterministic: same input, same text."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date

from lktec_bot.application.modules.release.commits import ParsedCommit
from lktec_bot.application.modules.release.options import OTHER_SECTION, Bump, ReleaseOptions

BREAKING_HEADING = "BREAKING CHANGES"
_PULL_REQUEST = re.compile(r"\(#(\d+)\)")


def render_notes(
    *,
    server_url: str,
    repository: str,
    version: str,
    tag: str,
    previous_tag: str | None,
    day: date,
    commits: Sequence[tuple[ParsedCommit, Bump]],
    options: ReleaseOptions,
) -> str:
    """The body of the release.

    The headline links to the comparison with the previous tag (or, for the first release, to
    the tag's tree). A commit is listed when its type has a heading or when it moves the version;
    one that moves the version through a rule but has no heading goes under "Other changes". A
    trailing ``(#123)`` becomes a pull request link. Breaking changes get a section of their own.
    """
    base = f"{server_url.rstrip('/')}/{repository}"
    target = f"{base}/compare/{previous_tag}...{tag}" if previous_tag else f"{base}/tree/{tag}"
    lines = [f"## [{version}]({target}) ({day.isoformat()})"]

    headings = options.headings()
    grouped: dict[str, list[ParsedCommit]] = {}
    for commit, bump in commits:
        if commit.type in headings:
            grouped.setdefault(headings[commit.type], []).append(commit)
        elif bump is not Bump.NONE:
            grouped.setdefault(OTHER_SECTION, []).append(commit)

    order = [*dict.fromkeys(headings.values()), OTHER_SECTION]
    for heading in order:
        entries = grouped.get(heading)
        if entries:
            lines += ["", f"### {heading}", ""]
            lines += [_entry(commit, base, commit.subject) for commit in entries]

    breaking = [(commit, note) for commit, _ in commits for note in commit.notes if commit.breaking]
    if breaking:
        lines += ["", f"### {BREAKING_HEADING}", ""]
        lines += [_entry(commit, base, note, link_commit=False) for commit, note in breaking]
    return "\n".join(lines) + "\n"


def _entry(commit: ParsedCommit, base: str, text: str, *, link_commit: bool = True) -> str:
    scope = f"**{commit.scope}:** " if commit.scope else ""
    text = _PULL_REQUEST.sub(lambda m: f"([#{m[1]}]({base}/pull/{m[1]}))", text)
    line = f"* {scope}{text}"
    if link_commit:
        line += f" ([{commit.sha[:7]}]({base}/commit/{commit.sha}))"
    return line
