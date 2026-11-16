# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Version strings as dependency bots write them, read just far enough to size an update.

Pure: text in, value out, no I/O. Only the epoch and the numeric release (``1.4.1`` of
``v1.4.1.post1``) decide how big an update is; whatever follows the release numbers must still
have a known shape, or the version is not understood and the update is left to a person:

* PEP 440 (Python): pre-releases (``2.0.0rc1``, ``1.0a2``, ``1.0-beta.3``), post-releases
  (``1.4.1.post1``, ``1.0-1``), development releases (``1.0.dev0``), local versions
  (``1.0+ubuntu1``) and epochs (``1!2.0``), in the normalised and the permitted spellings;
* SemVer (npm, Cargo, Go, actions): pre-release and build metadata (``1.2.3-rc.1+build.5``);
* Docker tags and NuGet: variant suffixes (``3.13-slim``, ``22-alpine3.20``) and any number of
  release parts (``1.2.3.4``);
* an optional ``v`` prefix (``v4``, ``v3.34.5``).

Anything else (``latest``, a commit sha without backticks, ``3d3c42e``) is ``None``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class UpdateKind(StrEnum):
    """How big one dependency update is, from the smallest to the largest."""

    DIGEST = "digest"
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"


_SEVERITY: dict[UpdateKind, int] = {kind: rank for rank, kind in enumerate(UpdateKind)}

# A version longer than this is not a version anyone publishes; refusing it also keeps int()
# away from digit strings beyond Python's conversion limit.
MAX_LENGTH = 128

_CORE = re.compile(r"[vV]?(?:(?P<epoch>[0-9]+)!)?(?P<release>[0-9]+(?:\.[0-9]+)*)(?P<rest>.*)")
# PEP 440 (normalised and permitted spellings): pre, post, dev, local; each optional.
_PEP440_REST = re.compile(
    r"""
    (?:[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview)[-_.]?[0-9]*)?
    (?:-[0-9]+|[-_.]?(?:post|rev|r)[-_.]?[0-9]*)?
    (?:[-_.]?dev[-_.]?[0-9]*)?
    (?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?
    """,
    re.VERBOSE | re.IGNORECASE,
)
# SemVer pre-release and build metadata; also the variant of a Docker tag (``-slim``).
_SEMVER_REST = re.compile(
    r"(?:-[0-9a-z]+(?:[.-][0-9a-z]+)*)?(?:\+[0-9a-z]+(?:[.-][0-9a-z]+)*)?", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class Version:
    """The parts of a version that size an update; ``suffix`` is kept for messages only."""

    epoch: int
    release: tuple[int, ...]
    suffix: str = ""

    @property
    def major(self) -> int:
        return self.release[0]

    @property
    def minor(self) -> int:
        return self.release[1] if len(self.release) > 1 else 0


def parse_version(text: str) -> Version | None:
    """``text`` as a :class:`Version`, or ``None`` when its shape is not one listed above."""
    text = text.strip()
    if not text or len(text) > MAX_LENGTH:
        return None
    match = _CORE.fullmatch(text)
    if match is None:
        return None
    rest = match.group("rest")
    if _PEP440_REST.fullmatch(rest) is None and _SEMVER_REST.fullmatch(rest) is None:
        return None
    release = tuple(int(part) for part in match.group("release").split("."))
    return Version(int(match.group("epoch") or 0), release, rest)


def kind_of_change(old: Version, new: Version) -> UpdateKind:
    """Major for a different epoch or first number, minor for a different second, else patch.

    A missing second number counts as ``0`` (``1.2`` to ``1.2.0`` is a patch). Suffixes never
    change the result (``8.4.0`` to ``8.4.1-rc.1`` is a patch, ``1.4.1.post1`` to ``1.5.0`` a
    minor).
    """
    if old.epoch != new.epoch or old.major != new.major:
        return UpdateKind.MAJOR
    if old.minor != new.minor:
        return UpdateKind.MINOR
    return UpdateKind.PATCH


def kind_of_versions(old: str, new: str) -> UpdateKind | None:
    """The kind of the update from ``old`` to ``new``; ``None`` when either is not understood."""
    old_version, new_version = parse_version(old), parse_version(new)
    if old_version is None or new_version is None:
        return None
    return kind_of_change(old_version, new_version)


def most_severe(kinds: Iterable[UpdateKind]) -> UpdateKind | None:
    """The largest of ``kinds`` (``None`` for none)."""
    return max(kinds, key=_SEVERITY.__getitem__, default=None)
