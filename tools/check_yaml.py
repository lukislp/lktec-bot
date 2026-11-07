# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Strict YAML check for workflows, actions and the policy file.

Every file must parse with ``yaml.safe_load``, must not repeat a mapping key (PyYAML would
silently keep the last one) and must be a mapping. In workflow and action files every
``uses:`` of a remote action is pinned to a full commit SHA with a ``# vX.Y.Z`` comment.
Usage: ``python tools/check_yaml.py <file or directory>...``; exit 1 with every problem.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

_PINNED = re.compile(r"^\s*(?:-\s+)?uses:\s*([^\s#]+)\s*(#\s*v?\d+(?:\.\d+)*.*)?$")
_LOCAL = ("./", "docker://")


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys instead of keeping the last one."""

    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise yaml.constructor.ConstructorError(
                None, None, f"expected a mapping node, got {type(node).__name__}", node.start_mark
            )
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def problems_in(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    problems: list[str] = []
    try:
        document = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as exc:
        return [f"{path}: {exc}"]
    if not isinstance(document, dict):
        problems.append(f"{path}: top level is not a mapping")
    if path.parent.name == "workflows" or path.name == "action.yml":
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = _PINNED.match(line)
            if match is None:
                continue
            reference, comment = match.groups()
            if reference.startswith(_LOCAL):
                continue
            sha = reference.rpartition("@")[2]
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                problems.append(f"{path}:{line_number}: {reference} is not pinned to a commit SHA")
            elif comment is None:
                problems.append(f"{path}:{line_number}: {reference} has no version comment")
    return problems


def main(arguments: list[str]) -> int:
    files: list[Path] = []
    for argument in arguments:
        target = Path(argument)
        if target.is_dir():
            files.extend(sorted(p for p in target.rglob("*.yml") if p.is_file()))
            files.extend(sorted(p for p in target.rglob("*.yaml") if p.is_file()))
        else:
            files.append(target)
    problems = [problem for file in files for problem in problems_in(file)]
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"checked {len(files)} YAML files, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
