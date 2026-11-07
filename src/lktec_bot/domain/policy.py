# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The per-repository policy file ``.github/lktec-bot.yml`` and the decisions made from it.

A repository opts in to the bot by committing this file. Everything is denied unless the file
exists, parses, and enables the module a verb belongs to. The domain understands the shape of
the file (``version``, ``modules``, ``enabled``) and nothing about individual modules: every
other key of a module entry is kept as an option for the module registry in the application
layer to validate. Unknown top-level keys are errors rather than silently ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError, field_validator

from lktec_bot.domain.permissions import Verb

POLICY_PATH = ".github/lktec-bot.yml"


class ModuleConfig(BaseModel):
    """One module entry. ``enabled`` is the only key the domain interprets.

    ``enabled`` is a strict boolean: ``yes``, ``1`` or ``"true"`` are errors, not truthy. Every
    other key is kept in :attr:`options` and validated later against the module's own options
    model; a module without such a model accepts no options at all.
    """

    model_config = ConfigDict(extra="allow")

    enabled: StrictBool = False

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class Policy(BaseModel):
    """Validated content of ``.github/lktec-bot.yml``."""

    model_config = ConfigDict(extra="forbid")

    path: ClassVar[str] = POLICY_PATH

    version: Literal[1]
    modules: dict[str, ModuleConfig] = {}

    @field_validator("modules", mode="before")
    @classmethod
    def _empty_entries(cls, raw: object) -> object:
        """``modules:`` and ``automerge:`` without a value mean an empty mapping, not ``null``."""
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return {name: {} if options is None else options for name, options in raw.items()}
        return raw

    def allows(self, module: str, verb: Verb | str) -> Decision:
        """Decide whether ``module`` is declared and enabled; the verb is carried for the reason.

        Which verbs a module may run is not a property of the file but of the module registry;
        :func:`lktec_bot.application.modules.authorize` adds that check on top of this one.
        """
        verb_name = _verb_name(verb)
        if verb_name is None:
            return Decision(False, module, str(verb), f"unknown verb {verb!s}")
        config = self.modules.get(module)
        if config is None:
            return Decision(
                False, module, verb_name, f"module {module!r} is not declared in {self.path}"
            )
        if not config.enabled:
            return Decision(
                False, module, verb_name, f"module {module!r} is disabled in {self.path}"
            )
        return Decision(True, module, verb_name, f"module {module!r} is enabled in {self.path}")


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of a policy check, always with a reason a human can read in a job summary."""

    allowed: bool
    module: str
    verb: str
    reason: str

    def __str__(self) -> str:
        state = "allowed" if self.allowed else "denied"
        return f"{self.verb} for {self.module}: {state} ({self.reason})"


class PolicyError(ValueError):
    """The policy file exists but is not valid. ``reasons`` lists every problem found."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def load_policy(text: str) -> Policy:
    """Parse and validate the shape of policy YAML. Raises :class:`PolicyError` with reasons.

    Module options are carried through unvalidated; see
    :func:`lktec_bot.application.modules.validate_policy` for the full check.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError([f"not valid YAML: {exc}"]) from exc
    if raw is None:
        raise PolicyError(["the policy file is empty"])
    if not isinstance(raw, dict):
        raise PolicyError(["the policy file must be a mapping with `version` and `modules`"])
    try:
        return Policy.model_validate(raw)
    except ValidationError as exc:
        raise PolicyError(format_validation_errors(exc)) from exc


def decide(policy: Policy | None, module: str, verb: Verb | str) -> Decision:
    """Decide for a possibly missing policy: no file means everything is denied."""
    if policy is None:
        return Decision(False, module, str(verb), f"{POLICY_PATH} is missing in the repository")
    return policy.allows(module, verb)


def format_validation_errors(exc: ValidationError, prefix: tuple[str, ...] = ()) -> list[str]:
    """Render pydantic errors as ``location: message`` lines, optionally under a prefix."""
    reasons: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in (*prefix, *error["loc"])) or "<root>"
        reasons.append(f"{location}: {error['msg']}")
    return reasons


def _verb_name(verb: Verb | str) -> str | None:
    try:
        return Verb(verb).value
    except ValueError:
        return None
