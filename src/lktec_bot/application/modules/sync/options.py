"""Options of the ``sync`` module, as they appear under ``modules.sync`` in
``.github/lktec-bot.yml``. Unknown keys are errors, so a typo never changes behaviour."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lktec_bot.domain.paths import repo_path_problem

DEFAULT_BRANCH = "main"
DEFAULT_COMMIT_MESSAGE = "chore(deploy): image {version} [skip ci]"
MAX_HEADLINE = 200
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")


class SyncOptions(BaseModel):
    """Options of the ``sync`` module.

    ``paths``
        Required, non-empty. Only files under these directories (or exactly these files) may
        be committed. A path under ``.github/workflows/`` is rejected.
    ``branch``
        The branch the commit goes onto (default ``main``).
    ``commit-message``
        The commit headline, one line. ``{version}`` is replaced by the ``--version`` argument
        (an empty string when it is not given).
    """

    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(min_length=1)
    branch: str = DEFAULT_BRANCH
    commit_message: str = Field(default=DEFAULT_COMMIT_MESSAGE, alias="commit-message")

    @field_validator("paths")
    @classmethod
    def _plain_paths(cls, value: list[str]) -> list[str]:
        for path in value:
            problem = repo_path_problem(path)
            if problem is not None:
                raise ValueError(f"{path!r}: {problem}")
        return value

    @field_validator("branch")
    @classmethod
    def _branch_name(cls, value: str) -> str:
        if not _BRANCH.match(value) or value.startswith("refs/") or ".." in value:
            raise ValueError("must be a plain branch name, not a ref")
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

    def headline(self, version: str) -> str:
        return self.commit_message.replace("{version}", version)
