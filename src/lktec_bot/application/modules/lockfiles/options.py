# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Options of the ``lockfiles`` module, as they appear under ``modules.lockfiles`` in
``.github/lktec-bot.yml``. Unknown keys are errors, so a typo never changes behaviour."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lktec_bot.application.modules.lockfiles.ecosystems import LOCKFILE_NAMES
from lktec_bot.domain.paths import repo_path_problem

DEFAULT_COMMIT_MESSAGE = "chore(deps): refresh lockfiles after Dependabot bump"
MAX_HEADLINE = 200


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
    """

    model_config = ConfigDict(extra="forbid")

    ecosystems: list[str] = []
    paths: list[str] = []
    commit_message: str = Field(default=DEFAULT_COMMIT_MESSAGE, alias="commit-message")

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

    def headline(self, number: int) -> str:
        return self.commit_message.replace("{number}", str(number))
