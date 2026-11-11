# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The lock files the module knows, and how a path is told to belong to one.

The regenerating job runs a pull request's code, so what it hands over is untrusted. The module
accepts a lock file only when its name is one of the names below; the only other files it
accepts are the derived files the default branch's policy names exactly (``allowlist.py``
decides both). The commands that regenerate each lock file live in the reusable workflow
(``.github/workflows/lockfiles.yml``), not here.
"""

from __future__ import annotations

from lktec_bot.domain.paths import within as within  # re-exported: the containment rule

# Ecosystem name (as used in the ``ecosystems`` option) -> the lock file's name.
LOCKFILE_NAMES: dict[str, str] = {
    "uv": "uv.lock",
    "dotnet": "packages.lock.json",
}


# The files a dependency update changes: manifests and lock files, by file name. When derived
# files are generated, only these are taken from the pull request; every other file comes from
# the default branch's history (see ``derive.py``). A name missing here makes a generator see
# the old dependency graph, which leaves the derived file stale and the repository's own check
# red; it never makes the bot run or commit more.
DEPENDENCY_FILE_NAMES: frozenset[str] = frozenset(
    {
        # NuGet
        "packages.lock.json",
        "packages.config",
        "global.json",
        "dotnet-tools.json",
        # Python
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "Pipfile",
        "Pipfile.lock",
        # npm, pnpm, yarn
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "yarn.lock",
        # Go, Rust, Ruby, Dart, PHP
        "go.mod",
        "go.sum",
        "Cargo.toml",
        "Cargo.lock",
        "Gemfile",
        "Gemfile.lock",
        "pubspec.yaml",
        "pubspec.lock",
        "composer.json",
        "composer.lock",
    }
)
# MSBuild project and property files (``Directory.Packages.props``, ``App.csproj``).
DEPENDENCY_FILE_SUFFIXES: tuple[str, ...] = (".csproj", ".fsproj", ".vbproj", ".props", ".targets")
_REQUIREMENTS = ("requirements", ".txt")


def ecosystem_of(path: str) -> str | None:
    """The ecosystem whose lock file ``path`` is (by file name), or ``None`` when unknown."""
    name = path.rpartition("/")[2]
    for ecosystem, lockfile in LOCKFILE_NAMES.items():
        if name == lockfile:
            return ecosystem
    return None


def is_dependency_file(path: str) -> bool:
    """Whether ``path`` is a dependency manifest or lock file, judged by its file name."""
    name = path.rpartition("/")[2]
    if name in DEPENDENCY_FILE_NAMES or name.endswith(DEPENDENCY_FILE_SUFFIXES):
        return True
    return name.startswith(_REQUIREMENTS[0]) and name.endswith(_REQUIREMENTS[1])
