"""The lock files the module knows, and how a path is told to belong to one.

The regenerating job runs a pull request's code, so what it hands over is untrusted. The module
accepts a file only when its name is one of the names below; anything else is refused. The
commands that regenerate each lock file live in the reusable workflow
(``.github/workflows/lockfiles.yml``), not here.
"""

from __future__ import annotations

from lktec_bot.domain.paths import within as within  # re-exported: the containment rule

# Ecosystem name (as used in the ``ecosystems`` option) -> the lock file's name.
LOCKFILE_NAMES: dict[str, str] = {
    "uv": "uv.lock",
    "dotnet": "packages.lock.json",
}


def ecosystem_of(path: str) -> str | None:
    """The ecosystem whose lock file ``path`` is (by file name), or ``None`` when unknown."""
    name = path.rpartition("/")[2]
    for ecosystem, lockfile in LOCKFILE_NAMES.items():
        if name == lockfile:
            return ecosystem
    return None
