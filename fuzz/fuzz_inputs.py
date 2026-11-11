# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Atheris fuzz harness for the parsers of text the bot does not control.

Contract under test: ``load_policy`` turns any YAML text into a ``Policy`` or raises
``PolicyError`` (never a raw YAML or validation error); ``parse_commit`` turns any commit
message into a Conventional Commit or ``None``, with a breaking commit always carrying its
notes; ``parse_version`` accepts only the canonical ``X.Y.Z`` form, which ``format_version``
reproduces exactly, and ``version_of_tag`` never raises; ``validate_policy`` (the registry
check on top, which validates the ``derived`` generators of the lock file module) raises
nothing but ``PolicyError``; a ``derived`` entry built from fuzzed text is accepted or refused
with a ``ValidationError``, an accepted path is a plain repository path outside ``.github/``,
and the allow-list admits no path that is neither a lock file name nor a declared one.
Anything else is a finding.

Run locally (Linux, needs the atheris wheel):
    uv sync --locked --group fuzz
    uv run python fuzz/fuzz_inputs.py -max_total_time=60
CI runs the same harness for a short, fixed time budget (see .github/workflows/ci.yml).
"""

from __future__ import annotations

import contextlib
import sys

import atheris
from pydantic import ValidationError

from lktec_bot.application.modules import validate_policy
from lktec_bot.application.modules.lockfiles.allowlist import admit
from lktec_bot.application.modules.lockfiles.ecosystems import ecosystem_of
from lktec_bot.application.modules.lockfiles.options import LockfilesOptions
from lktec_bot.application.modules.release.commits import (
    format_version,
    parse_commit,
    parse_version,
    version_of_tag,
)
from lktec_bot.domain.paths import repo_path_problem
from lktec_bot.domain.policy import PolicyError, load_policy
from lktec_bot.ports.github_api import CommitInfo


def test_one_input(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)

    with contextlib.suppress(PolicyError):
        load_policy(fdp.ConsumeUnicodeNoSurrogates(256))

    with contextlib.suppress(PolicyError):
        validate_policy(fdp.ConsumeUnicodeNoSurrogates(256))

    declared = fdp.ConsumeUnicodeNoSurrogates(48)
    entry = {
        "name": fdp.ConsumeUnicodeNoSurrogates(16),
        "run": fdp.ConsumeUnicodeNoSurrogates(32),
        "paths": [declared],
    }
    try:
        options = LockfilesOptions.model_validate({"derived": [entry]})
    except ValidationError:
        options = LockfilesOptions()
    else:
        lowered = declared.lower()
        if (
            repo_path_problem(declared) is not None
            or lowered == ".github"
            or lowered.startswith(".github/")
            or ecosystem_of(declared) is not None
        ):
            raise AssertionError(f"derived path {declared!r} was accepted")
    path = fdp.ConsumeUnicodeNoSurrogates(48) if fdp.ConsumeBool() else declared
    verdict = admit(path, fdp.ConsumeIntInRange(0, 1 << 30), options)
    if not verdict.refused and (
        repo_path_problem(path) is not None
        or (ecosystem_of(path) is None and path not in options.derived_paths())
    ):
        raise AssertionError(f"admit({path!r}) = {verdict!r}")

    message = fdp.ConsumeUnicodeNoSurrogates(256)
    commit = parse_commit(CommitInfo("a" * 40, message))
    if commit is not None and (
        not commit.type or not commit.subject or (commit.breaking and not commit.notes)
    ):
        raise AssertionError(f"parse_commit({message!r}) produced {commit!r}")

    text = fdp.ConsumeUnicodeNoSurrogates(24)
    version = parse_version(text)
    if version is not None and format_version(version) != text:
        raise AssertionError(f"parse_version({text!r}) = {version!r} is not canonical")

    prefix = fdp.ConsumeUnicodeNoSurrogates(4)
    suffix = fdp.ConsumeUnicodeNoSurrogates(4)
    tag = fdp.ConsumeUnicodeNoSurrogates(32)
    version_of_tag(tag, prefix, suffix)
    # A tag built from a version always reads back as that version.
    parts = (
        fdp.ConsumeIntInRange(0, 999),
        fdp.ConsumeIntInRange(0, 999),
        fdp.ConsumeIntInRange(0, 999),
    )
    if version_of_tag(f"{prefix}{format_version(parts)}{suffix}", prefix, suffix) != parts:
        raise AssertionError(f"version {parts!r} does not round-trip through a tag")


if __name__ == "__main__":
    # instrument_all() instead of instrument_imports(): the package is loaded through uv's
    # editable-install loader, which the import hook does not see (no coverage feedback).
    atheris.instrument_all()
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
