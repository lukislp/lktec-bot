"""Options of the ``release`` module, as they appear under ``modules.release`` in
``.github/lktec-bot.yml``. Unknown keys are errors, so a typo never changes behaviour."""

from __future__ import annotations

import re
from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SEMVER_CORE = r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
_SEMVER = re.compile(rf"^{SEMVER_CORE}\Z")
# What may stand around ``{version}`` in a tag: the characters of a plain git tag name.
_TAG_TEXT = re.compile(r"^[A-Za-z0-9._/@-]*\Z")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*\Z")

DEFAULT_SECTIONS: dict[str, str] = {
    "feat": "Features",
    "fix": "Bug Fixes",
    "perf": "Performance Improvements",
    "revert": "Reverts",
}
OTHER_SECTION = "Other changes"


class Bump(IntEnum):
    """How far a commit moves the version; ordered so that ``max`` picks the largest."""

    NONE = 0
    PATCH = 1
    MINOR = 2
    MAJOR = 3


class Rule(BaseModel):
    """One release rule: when every given condition matches a commit, ``release`` applies."""

    model_config = ConfigDict(extra="forbid")

    type: str | None = None
    scope: str | None = None
    breaking: bool | None = None
    release: Literal["major", "minor", "patch", "none"]

    @model_validator(mode="after")
    def _has_a_condition(self) -> Rule:
        if self.type is None and self.scope is None and self.breaking is None:
            raise ValueError("a rule needs at least one of type, scope and breaking")
        return self

    @property
    def bump(self) -> Bump:
        return Bump[self.release.upper()]


class ReleaseOptions(BaseModel):
    """Options of the ``release`` module.

    ``mode``
        ``tag`` (the only mode so far): a push to the branch is released as a tag and a GitHub
        release. ``pr`` (a release pull request with changelog and version files) is planned.
    ``tag-format``
        The tag name with ``{version}`` exactly once, e.g. ``v{version}``.
    ``branch``
        The branch releases are cut from.
    ``first-version``
        The version of the first release, when no tag matches ``tag-format`` yet.
    ``rules``
        Release rules evaluated in order before the defaults; the first rule that matches a
        commit decides. ``release: none`` makes the commit contribute nothing.
    ``sections``
        Commit type to heading in the release notes; merged over the defaults.
    """

    model_config = ConfigDict(extra="forbid")

    mode: str = "tag"
    tag_format: str = Field(default="v{version}", alias="tag-format")
    branch: str = "main"
    first_version: str = Field(default="1.0.0", alias="first-version")
    rules: list[Rule] = []
    sections: dict[str, str] = {}

    @field_validator("mode")
    @classmethod
    def _only_tag_mode(cls, value: str) -> str:
        if value == "pr":
            raise ValueError("mode 'pr' is not available yet; only 'tag' is supported")
        if value != "tag":
            raise ValueError(f"unknown mode {value!r}; only 'tag' is supported")
        return value

    @field_validator("tag_format")
    @classmethod
    def _version_once(cls, value: str) -> str:
        if value.count("{version}") != 1:
            raise ValueError("must contain '{version}' exactly once")
        before, _, after = value.partition("{version}")
        if not _TAG_TEXT.fullmatch(before) or not _TAG_TEXT.fullmatch(after):
            raise ValueError("may only contain letters, digits and . _ / @ - around '{version}'")
        return value

    @field_validator("branch")
    @classmethod
    def _plain_branch(cls, value: str) -> str:
        if not _BRANCH.fullmatch(value) or ".." in value or value.endswith(("/", ".lock")):
            raise ValueError("must be a plain branch name")
        return value

    @field_validator("first_version")
    @classmethod
    def _semver(cls, value: str) -> str:
        if not _SEMVER.fullmatch(value):
            raise ValueError("must be a version like 1.0.0 (no prerelease, no build metadata)")
        return value

    @field_validator("sections")
    @classmethod
    def _headings(cls, value: dict[str, str]) -> dict[str, str]:
        for commit_type, heading in value.items():
            if not re.fullmatch(r"\w+", commit_type):
                raise ValueError(f"{commit_type!r} is not a commit type")
            if not heading.strip() or "\n" in heading or "\r" in heading:
                raise ValueError(f"the heading of {commit_type!r} must be one non-empty line")
        return value

    @property
    def prefix(self) -> str:
        return self.tag_format.partition("{version}")[0]

    @property
    def suffix(self) -> str:
        return self.tag_format.partition("{version}")[2]

    def tag_for(self, version: str) -> str:
        return self.tag_format.replace("{version}", version)

    def headings(self) -> dict[str, str]:
        """The note headings: the defaults, then the policy's, in that order."""
        return {**DEFAULT_SECTIONS, **self.sections}
