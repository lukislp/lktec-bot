# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Options of the ``lockfiles`` module, as they appear under ``modules.lockfiles`` in
``.github/lktec-bot.yml``. Unknown keys are errors, so a typo never changes behaviour."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from lktec_bot.application.modules.lockfiles.ecosystems import LOCKFILE_NAMES, ecosystem_of
from lktec_bot.domain.paths import generated_path_problem, repo_path_problem

DEFAULT_COMMIT_MESSAGE = "chore(deps): refresh lockfiles after Dependabot bump"
MAX_HEADLINE = 200

# Bounds of the ``derived`` option. They are small on purpose: every generator is a command the
# regenerating job runs, and every path one more file the bot may write.
MAX_GENERATORS = 4
MAX_GENERATOR_PATHS = 8
MAX_GENERATOR_NAME = 60
MAX_RUN_CHARS = 2000
DEFAULT_TIMEOUT_MINUTES = 5
MAX_TIMEOUT_MINUTES = 10
# All generators of a repository together; the regenerating job has to finish the lock files too.
MAX_TOTAL_TIMEOUT_MINUTES = 15

_GENERATOR_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*\Z")
# A command may span lines (a YAML block scalar); nothing else below a space is allowed.
_RUN_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class DerivedGenerator(BaseModel):
    """One entry of ``derived``: a command and the exact files it may produce.

    ``name``
        What the job summary calls the generator. Letters, digits, space, ``.``, ``_``, ``-``.
    ``run``
        The command, run with ``bash -euo pipefail -c`` in the regenerating job, which holds no
        secret. It is read from the default branch, like the whole policy.
    ``paths``
        The files the command may write, each named exactly: no pattern, no directory, nothing
        under ``.github/``, not a lock file (those have their own rule). Anything else the
        command changes in the tree fails the generator.
    ``timeout-minutes``
        After this the command is killed and counts as failed.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    run: str
    paths: list[str]
    timeout_minutes: StrictInt = Field(
        default=DEFAULT_TIMEOUT_MINUTES, alias="timeout-minutes", ge=1, le=MAX_TIMEOUT_MINUTES
    )

    @field_validator("name")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        if len(value) > MAX_GENERATOR_NAME:
            raise ValueError(f"must be at most {MAX_GENERATOR_NAME} characters")
        if not _GENERATOR_NAME.match(value) or value != value.strip():
            raise ValueError(
                "must start with a letter or digit and use only letters, digits, space, '.', "
                "'_' and '-'"
            )
        return value

    @field_validator("run")
    @classmethod
    def _a_command(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        if len(value) > MAX_RUN_CHARS:
            raise ValueError(f"must be at most {MAX_RUN_CHARS} characters")
        if _RUN_CONTROL.search(value):
            raise ValueError("must not contain control characters")
        return value

    @field_validator("paths")
    @classmethod
    def _exact_files(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("must name at least one file the command produces")
        if len(value) > MAX_GENERATOR_PATHS:
            raise ValueError(f"must name at most {MAX_GENERATOR_PATHS} files")
        for path in value:
            problem = generated_path_problem(path)
            if problem is not None:
                raise ValueError(f"{path!r}: {problem}")
            if ecosystem_of(path) is not None:
                raise ValueError(
                    f"{path!r}: a lock file is refreshed by the module itself, not by a generator"
                )
        if len(set(value)) != len(value):
            raise ValueError("a path is listed twice")
        return value


class LockfilesOptions(BaseModel):
    """Options of the ``lockfiles`` module.

    ``ecosystems``
        Which lock files the bot may commit: any of ``uv`` (``uv.lock``) and ``dotnet``
        (``packages.lock.json``). Default (empty): every supported ecosystem whose lock file
        the regenerating job found changed.
    ``paths``
        Restrict the lock files to these repository paths: a file, or a directory (everything
        below it). Default (empty): the whole repository.
    ``commit-message``
        The commit headline, one line. ``{number}`` is replaced by the pull request number.
    ``derived``
        Generators of files derived from the dependency graph (third-party notices, ...), see
        :class:`DerivedGenerator`. Default (empty): none, the module only handles lock files.
    """

    model_config = ConfigDict(extra="forbid")

    ecosystems: list[str] = []
    paths: list[str] = []
    commit_message: str = Field(default=DEFAULT_COMMIT_MESSAGE, alias="commit-message")
    derived: list[DerivedGenerator] = []

    @field_validator("ecosystems")
    @classmethod
    def _known_ecosystems(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(LOCKFILE_NAMES))
        if unknown:
            raise ValueError(
                f"unknown ecosystem {', '.join(unknown)} (supported: {', '.join(LOCKFILE_NAMES)})"
            )
        if len(set(value)) != len(value):
            raise ValueError("an ecosystem is listed twice")
        return value

    @field_validator("paths")
    @classmethod
    def _plain_paths(cls, value: list[str]) -> list[str]:
        for path in value:
            problem = repo_path_problem(path)
            if problem is not None:
                raise ValueError(f"{path!r}: {problem}")
        return value

    @field_validator("commit_message")
    @classmethod
    def _one_line(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        if "\n" in value or "\r" in value:
            raise ValueError("must be a single line")
        if len(value) > MAX_HEADLINE:
            raise ValueError(f"must be at most {MAX_HEADLINE} characters")
        return value

    @field_validator("derived")
    @classmethod
    def _bounded_generators(cls, value: list[DerivedGenerator]) -> list[DerivedGenerator]:
        if len(value) > MAX_GENERATORS:
            raise ValueError(f"at most {MAX_GENERATORS} generators")
        names = [generator.name.lower() for generator in value]
        if len(set(names)) != len(names):
            raise ValueError("a generator name is used twice")
        paths = [path for generator in value for path in generator.paths]
        if len(set(paths)) != len(paths):
            raise ValueError("a path belongs to more than one generator")
        total = sum(generator.timeout_minutes for generator in value)
        if total > MAX_TOTAL_TIMEOUT_MINUTES:
            raise ValueError(
                f"the timeouts add up to {total} minutes, more than {MAX_TOTAL_TIMEOUT_MINUTES}"
            )
        return value

    def headline(self, number: int) -> str:
        return self.commit_message.replace("{number}", str(number))

    def derived_paths(self) -> dict[str, str]:
        """Every declared derived path, mapped to the name of the generator that owns it."""
        return {path: generator.name for generator in self.derived for path in generator.paths}
