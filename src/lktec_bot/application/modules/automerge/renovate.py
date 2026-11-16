# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Classification of Renovate pull requests. Pure: title and body in, decision out, no I/O.

Renovate's own metadata comes first. Every Renovate pull request body starts with a table:

    This PR contains the following updates:

    | Package | Update | Change |
    |---|---|---|
    | [redis](https://hub.docker.com/_/redis) | digest | `71885d8` → `7958cfb` |
    | grafana/loki | patch | `3.7.9` → `3.7.10` |

One row per update (a group has several). ``Update`` is Renovate's ``updateType``; ``Change``
holds the old and the new value in backticks (``→``, or ``->`` in older Renovate versions,
sometimes wrapped in a link). A row's kind is the larger of the two readings: what Renovate
declares, and what :mod:`~lktec_bot.application.modules.automerge.versions` reads from the
change. So a mislabelled row can make an update bigger, never smaller. A table without an
``Update`` column is read from the change alone.

Whatever cannot be read is manual: an update type other than major, minor, patch or digest
(``pin``, ``pinDigest``, ``rollback``, ``replacement``, ``bump``, ``lockFileMaintenance``), a
change without both values, a version of an unknown shape (a range such as ``^1.2.3``), a row
that does not have the table's shape. Release notes inside ``<details>`` are the dependency
maintainers' text and are never read.

Only when the body has no such table is the title read, and only in the ``from A to B`` form
(``update dependency x from 1.0.0 to 1.0.1``, a custom ``commitMessageExtra``). Renovate's
default title (``update x to v1.2.3``) names only the new version and is not enough.
"""

from __future__ import annotations

import re

from lktec_bot.application.modules.automerge.classify import Classification, Dependency
from lktec_bot.application.modules.automerge.versions import (
    UpdateKind,
    is_digest,
    kind_of_versions,
    most_severe,
)

RENOVATE_LOGIN = "renovate[bot]"
RENOVATE_BRANCH_PREFIX = "renovate/"

_DECLARED: dict[str, UpdateKind] = {
    "major": UpdateKind.MAJOR,
    "minor": UpdateKind.MINOR,
    "patch": UpdateKind.PATCH,
    "digest": UpdateKind.DIGEST,
}
_DETAILS = re.compile(r"<details>.*?</details>", re.DOTALL | re.IGNORECASE)
_SEPARATOR = re.compile(r"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?")
_CHANGE = re.compile(r"`(?P<old>[^`\s]+)`\s*(?:→|->)\s*`(?P<new>[^`\s]+)`")
_LINKED_NAME = re.compile(r"\[(?P<name>[^\]\s]+)\]\(")
_TITLE = re.compile(
    r"\b(?:update|bump)s?\s+(?:dependency\s+)?`?(?P<name>[^\s`]+)`?"
    r"(?:\s+(?:docker|helm|github|action|tag|image|digest|chart|package)){0,3}"
    r"\s+from\s+`?(?P<old>[^\s`]+)`?\s+to\s+`?(?P<new>[^\s`]+)`?(?=\s|$)",
    re.IGNORECASE,
)


def classify_renovate(title: str, body: str = "") -> Classification:
    """Classify a Renovate pull request from its update table, or from its title without one."""
    table = _update_table(_DETAILS.sub("", body))
    if table is None:
        return _from_title(title)
    header, rows = table
    if not rows:
        return Classification((), None, False, "the update table lists no updates")
    dependencies: list[Dependency] = []
    problems: list[str] = []
    for cells in rows:
        dependency, problem = _row(header, cells)
        if dependency is not None:
            dependencies.append(dependency)
        if problem is not None:
            problems.append(problem)
    grouped = len(rows) > 1
    if problems:
        return Classification(tuple(dependencies), None, grouped, "; ".join(problems))
    kinds = [dep.kind for dep in dependencies if dep.kind is not None]
    return Classification(tuple(dependencies), most_severe(kinds), grouped)


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    return [cell.strip() for cell in stripped.removeprefix("|").removesuffix("|").split("|")]


def _update_table(text: str) -> tuple[list[str], list[list[str]]] | None:
    """The first table whose header has ``Package`` and ``Change``: lower-case header, rows."""
    lines = text.splitlines()
    for index, line in enumerate(lines[:-1]):
        if not line.lstrip().startswith("|"):
            continue
        header = [cell.casefold() for cell in _cells(line)]
        if "package" not in header or "change" not in header:
            continue
        if _SEPARATOR.fullmatch(lines[index + 1].strip()) is None:
            continue
        rows: list[list[str]] = []
        for row in lines[index + 2 :]:
            if not row.lstrip().startswith("|"):
                break
            rows.append(_cells(row))
        return header, rows
    return None


def _name(cell: str) -> str:
    match = _LINKED_NAME.match(cell)
    if match is not None:
        return match.group("name")
    words = cell.replace("`", "").split()
    return words[0] if words else ""


def _row(header: list[str], cells: list[str]) -> tuple[Dependency | None, str | None]:
    """One table row as a dependency (``None`` without a name) and what is wrong with it."""
    if len(cells) != len(header):
        return None, f"a row of the update table has {len(cells)} cells, not {len(header)}"
    row = dict(zip(header, cells, strict=True))
    name = _name(row["package"])
    if not name:
        return None, "a row of the update table names no package"
    change = _CHANGE.search(row["change"])
    if change is None:
        return Dependency(name, "", "", None), f"{name}: no old and new value in the change"
    old, new = change.group("old"), change.group("new")
    declared: UpdateKind | None = None
    if "update" in header:
        update = row["update"].strip("` ")
        declared = _DECLARED.get(update.casefold())
        if declared is None:
            return Dependency(name, old, new, None), f"{name}: update type {update!r} is manual"
    read = kind_of_versions(old, new)
    if read is None and declared is UpdateKind.DIGEST and is_digest(old) and is_digest(new):
        read = UpdateKind.DIGEST
    if read is None:
        return Dependency(name, old, new, None), f"version not understood: {name} {old} to {new}"
    kind = most_severe([read] if declared is None else [declared, read])
    return Dependency(name, old, new, kind), None


def _from_title(title: str) -> Classification:
    match = _TITLE.search(title)
    if match is None:
        return Classification((), None, False, "no update table and no version change in the title")
    name, old, new = match.group("name"), match.group("old"), match.group("new").rstrip(".,;)")
    # Hex without Renovate's table is not trusted as a digest: only versions count here.
    kind = kind_of_versions(old, new)
    dependency = Dependency(name, old, new, kind)
    if kind is None:
        problem = f"version not understood: {name} {old} to {new}"
        return Classification((dependency,), None, False, problem)
    return Classification((dependency,), kind, False)
