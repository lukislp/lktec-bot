# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""SPDX header check for the source files.

Every Python and shell file starts, after an optional shebang, with a copyright line and the
license identifier in the `REUSE <https://reuse.software/spec/>`_ form::

    # SPDX-FileCopyrightText: 2026 Lukas Koerber
    # SPDX-License-Identifier: Apache-2.0

Usage: ``python tools/check_headers.py [--fix] <file or directory>...``; exit 1 with every file
that lacks the header. ``--fix`` inserts the header instead (the year is the current one).
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

LICENSE = "Apache-2.0"
HOLDER = "Lukas Koerber"
SUFFIXES = frozenset({".py", ".sh"})
SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__", "dist", "build"})
SKIP_FILES = frozenset({"_version.py"})  # written by hatch-vcs at build time

_COPYRIGHT = re.compile(r"^# SPDX-FileCopyrightText: \d{4}(-\d{4})? .+\S$")
_LICENSE = f"# SPDX-License-Identifier: {LICENSE}"


def _files(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        candidates = [path] if path.is_file() else sorted(path.rglob("*"))
        for file in candidates:
            if file.suffix not in SUFFIXES or file.name in SKIP_FILES:
                continue
            if any(part in SKIP_DIRS for part in file.parts):
                continue
            found.append(file)
    return found


def _split(text: str) -> tuple[str, list[str]]:
    """Return the shebang (with newline, or empty) and the remaining lines."""
    lines = text.splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        return lines[0], lines[1:]
    return "", lines


def has_header(text: str) -> bool:
    _, lines = _split(text)
    if len(lines) < 2:
        return False
    return bool(_COPYRIGHT.match(lines[0].rstrip("\n"))) and lines[1].rstrip("\n") == _LICENSE


def with_header(text: str, year: int) -> str:
    shebang, lines = _split(text)
    header = f"# SPDX-FileCopyrightText: {year} {HOLDER}\n{_LICENSE}\n"
    rest = "".join(lines)
    if rest and not rest.startswith("\n"):
        header += "\n"
    return shebang + header + rest


def main(argv: list[str]) -> int:
    fix = "--fix" in argv
    paths = [a for a in argv if a != "--fix"]
    if not paths:
        print(__doc__, file=sys.stderr)
        return 2
    year = dt.datetime.now(tz=dt.UTC).year
    missing: list[Path] = []
    for file in _files(paths):
        text = file.read_text(encoding="utf-8")
        if has_header(text):
            continue
        if fix:
            file.write_text(with_header(text, year), encoding="utf-8", newline="\n")
            print(f"added header: {file.as_posix()}")
        else:
            missing.append(file)
    for file in missing:
        print(f"{file.as_posix()}: missing SPDX copyright and license header")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
