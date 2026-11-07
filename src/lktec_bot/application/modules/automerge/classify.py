# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Classification of Dependabot pull requests. Pure: text in, decision out, no I/O.

This is the port of the rules of the shared ``dependabot-auto-merge`` workflow in
``lukislp/ci-workflows``, which had them in shell and in two places (the ``auto-merge`` job read
GitHub's ``fetch-metadata``, the hourly ``update-stale`` job re-read the title). Here one
function reads the title and the body and both paths use it.

What counts as routine (armed):

* a **patch** or **minor** version bump (the major number is unchanged);
* a **digest** refresh: the same tag, only the pinned sha256 moves. Dependabot titles those
  ``bump python from `78387bc` to `2f17fc0` ``; both sides are hex in backticks;
* a **major** bump only with ``majors: auto``.

Everything that cannot be read is manual, never armed: a SHA-to-SHA action bump (written
without backticks, it is not a version), a title without versions, a group whose body does not
list as many updates as the title announces. A grouped pull request is as severe as its most
severe member (``digest`` < ``patch`` < ``minor`` < ``major``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from lktec_bot.application.modules.automerge.options import AutomergeOptions

DEPENDABOT_LOGIN = "dependabot[bot]"
WORKFLOWS_DIR = ".github/workflows/"


class UpdateKind(StrEnum):
    """How big one dependency update is, from the smallest to the largest."""

    DIGEST = "digest"
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"


_SEVERITY: dict[UpdateKind, int] = {kind: rank for rank, kind in enumerate(UpdateKind)}

# ``1``, ``1.2.3``, ``v1.2``, ``1.2.3-rc.1``, ``3.13-slim``: a numeric core, an optional suffix.
_VERSION = r"v?[0-9]+(?:\.[0-9]+)*(?:[-+][0-9A-Za-z.-]*)?"
_HEX = r"[0-9a-f]{7,64}"
# After the new version: end of text, whitespace or the punctuation a sentence puts there.
_END = r"(?=[\s.,;)]|$)"

_DIGEST = rf"`(?P<dold>{_HEX})` to `(?P<dnew>{_HEX})`"
_BUMP = rf"(?:{_DIGEST}|`?(?P<old>{_VERSION})`? to `?(?P<new>{_VERSION})`?){_END}"

_TITLE_SINGLE = re.compile(rf"\bbumps?\s+`?(?P<name>[^\s`]+)`?\s+from\s+{_BUMP}", re.IGNORECASE)
_BODY_SINGLE = re.compile(
    rf"\bBumps\s+(?:\[(?P<lname>[^\]]+)\]\([^)\s]*\)|`?(?P<name>[^\s`\[]+)`?)\s+from\s+{_BUMP}"
)
# One line per member of a group: `Updates `ruff` from ...` (pip, npm, actions) or, for NuGet,
# `Updated [SkiaSharp](url) from ...`.
_BODY_UPDATES = re.compile(
    rf"^Update[sd]\s+(?:\[(?P<lname>[^\]]+)\]\([^)\s]*\)|`(?P<name>[^`]+)`)\s+from\s+{_BUMP}",
    re.MULTILINE,
)
_GROUP_TITLE = re.compile(r"\bbumps?\s+the\s+\S+\s+group\b", re.IGNORECASE)
_GROUP_COUNT = re.compile(r"\bwith\s+(\d+)\s+updates?\b", re.IGNORECASE)
_DETAILS = re.compile(r"<details>.*?</details>", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Dependency:
    """One dependency update read from a pull request."""

    name: str
    old: str
    new: str
    kind: UpdateKind


@dataclass(frozen=True, slots=True)
class Classification:
    """What a Dependabot pull request updates and how severe the update is.

    ``kind`` is ``None`` when the pull request cannot be read with confidence; ``problem`` then
    says why. Such a pull request is manual.
    """

    dependencies: tuple[Dependency, ...]
    kind: UpdateKind | None
    grouped: bool = False
    problem: str | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(dep.name for dep in self.dependencies))


def _major_minor(version: str) -> tuple[int, int]:
    core = version.removeprefix("v")
    core = re.split(r"[-+]", core, maxsplit=1)[0]
    parts = [int(part) for part in core.split(".") if part.isdigit()]
    return (parts[0] if parts else 0, parts[1] if len(parts) > 1 else 0)


def kind_of_versions(old: str, new: str) -> UpdateKind:
    """Patch, minor or major from the first two numbers; a differing major wins."""
    old_major, old_minor = _major_minor(old)
    new_major, new_minor = _major_minor(new)
    if old_major != new_major:
        return UpdateKind.MAJOR
    if old_minor != new_minor:
        return UpdateKind.MINOR
    return UpdateKind.PATCH


def _dependency(name: str, match: re.Match[str]) -> Dependency:
    if match.group("dold") is not None:
        return Dependency(name, match.group("dold"), match.group("dnew"), UpdateKind.DIGEST)
    old, new = match.group("old").rstrip("."), match.group("new").rstrip(".")
    return Dependency(name, old, new, kind_of_versions(old, new))


def _read_dependencies(title: str, body: str) -> list[Dependency]:
    found: dict[tuple[str, str, str], Dependency] = {}

    def add(dependency: Dependency) -> None:
        found.setdefault((dependency.name.casefold(), dependency.old, dependency.new), dependency)

    if (match := _TITLE_SINGLE.search(title)) is not None:
        add(_dependency(match.group("name"), match))
    # Release notes sit inside <details> blocks and are written by the dependency's own
    # maintainers; only what Dependabot wrote around them is read.
    text = _DETAILS.sub("", body)
    if (match := _BODY_SINGLE.search(text)) is not None:
        add(_dependency(match.group("lname") or match.group("name"), match))
    for match in _BODY_UPDATES.finditer(text):
        add(_dependency(match.group("lname") or match.group("name"), match))
    return list(found.values())


def most_severe(kinds: Iterable[UpdateKind]) -> UpdateKind | None:
    """The largest of ``kinds`` (``None`` for none)."""
    return max(kinds, key=_SEVERITY.__getitem__, default=None)


def classify(title: str, body: str = "") -> Classification:
    """Classify a Dependabot pull request from its title and body."""
    dependencies = _read_dependencies(title, body)
    grouped = _GROUP_TITLE.search(title) is not None
    announced = _GROUP_COUNT.search(title)
    if not dependencies:
        return Classification((), None, grouped, "no version change could be read from the title")
    if grouped and announced is not None and len(dependencies) < int(announced.group(1)):
        problem = (
            f"the title announces {announced.group(1)} updates but only "
            f"{len(dependencies)} could be read"
        )
        return Classification(tuple(dependencies), None, grouped, problem)
    return Classification(
        tuple(dependencies), most_severe(dep.kind for dep in dependencies), grouped
    )


def excluded_names(names: Iterable[str], exclude: Iterable[str]) -> list[str]:
    """The ``names`` that are on the ``exclude`` list (exact, case-insensitive), in order."""
    listed = {entry.strip().casefold() for entry in exclude if entry.strip()}
    return [name for name in dict.fromkeys(names) if name.casefold() in listed]


def touches_workflow_files(paths: Iterable[str]) -> bool:
    """Whether any changed path is under ``.github/workflows/``."""
    return any(path.startswith(WORKFLOWS_DIR) for path in paths)


@dataclass(frozen=True, slots=True)
class Eligibility:
    """Whether a pull request may be armed, and why.

    ``excluded`` marks a pull request that is on the exclude list (or whose dependencies are
    unknown while a list is set): a person decides about those, but the sweep still keeps one
    that a person armed mergeable.
    """

    arm: bool
    reason: str
    kind: UpdateKind | None = None
    excluded: bool = False


def evaluate(classification: Classification, options: AutomergeOptions) -> Eligibility:
    """The module's decision for one classified pull request, in the documented order.

    1. a dependency on the exclude list: never;
    2. an exclude list is set but no dependency name could be read: never (fail safe);
    3. the update kind is unknown: never;
    4. a major with ``majors: manual``: never;
    5. otherwise: arm.

    The ``workflow-files`` rule needs the changed files and is applied by the caller after this
    (see :func:`touches_workflow_files`), so that the files are only fetched when it matters.
    """
    hits = excluded_names(classification.names, options.exclude)
    if hits:
        return Eligibility(False, f"excluded dependency: {', '.join(hits)}", excluded=True)
    if options.exclude and not classification.names:
        return Eligibility(
            False, "dependencies could not be determined while exclude is set", excluded=True
        )
    kind = classification.kind
    if kind is None:
        return Eligibility(False, classification.problem or "update kind unknown")
    if kind is UpdateKind.MAJOR and options.majors == "manual":
        return Eligibility(False, "major update (majors: manual)", kind)
    label = f"{kind.value} update" + (" (group)" if classification.grouped else "")
    return Eligibility(True, label, kind)
