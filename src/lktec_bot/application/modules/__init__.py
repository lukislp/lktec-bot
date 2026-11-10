# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""The module registry: which modules exist, which verbs each may run, which options it takes.

A module is the caller of a verb (``--module automerge``). The policy file enables a module;
this registry bounds what an enabled module can do. The two together make the decision the
gate acts on, in this order:

1. the module is unknown to the registry: denied;
2. the policy file is missing: denied;
3. the policy file is invalid, including an unknown module name or an option its module does
   not take: denied with every reason;
4. the module is not declared, or declared but not enabled: denied;
5. the verb is not in the module's verb set: denied;
6. otherwise allowed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from lktec_bot.application.modules.automerge.options import AutomergeOptions as AutomergeOptions
from lktec_bot.application.modules.lockfiles.options import LockfilesOptions as LockfilesOptions
from lktec_bot.application.modules.release.options import ReleaseOptions as ReleaseOptions
from lktec_bot.application.modules.sync.options import SyncOptions as SyncOptions
from lktec_bot.domain.permissions import Verb
from lktec_bot.domain.policy import (
    POLICY_PATH,
    Decision,
    Policy,
    PolicyError,
    decide,
    format_validation_errors,
    load_policy,
)


@dataclass(frozen=True, slots=True)
class ModuleSpec:
    """What the registry knows about one module."""

    name: str
    verbs: frozenset[Verb]
    options: type[BaseModel] | None = None

    def option_problems(self, options: Mapping[str, object]) -> list[str]:
        """Every reason ``options`` are not valid for this module (empty when they are)."""
        if self.options is None:
            return [f"{key}: module {self.name!r} takes no options" for key in sorted(options)]
        try:
            self.options.model_validate(dict(options))
        except ValidationError as exc:
            return format_validation_errors(exc)
        return []

    def verb_names(self) -> str:
        return ", ".join(sorted(verb.value for verb in self.verbs))


MODULES: dict[str, ModuleSpec] = {}


def register_module(
    name: str, verbs: Iterable[Verb], options: type[BaseModel] | None = None
) -> ModuleSpec:
    """Register a module. The options model, when given, must forbid unknown keys."""
    if name in MODULES:
        raise ValueError(f"module {name!r} is already registered")
    if options is not None and options.model_config.get("extra") != "forbid":
        raise ValueError(f"the options model of module {name!r} must set extra='forbid'")
    spec = ModuleSpec(name, frozenset(verbs), options)
    MODULES[name] = spec
    return spec


register_module(
    "automerge",
    {Verb.APPROVE, Verb.ENABLE_AUTOMERGE, Verb.MERGE, Verb.UPDATE_BRANCH, Verb.COMMENT},
    AutomergeOptions,
)
register_module(
    "lockfiles", {Verb.COMMIT, Verb.COMMENT, Verb.OPEN_ISSUE, Verb.CHECK}, LockfilesOptions
)
register_module("release", {Verb.RELEASE, Verb.OPEN_ISSUE, Verb.CHECK}, ReleaseOptions)
register_module("sync", {Verb.COMMIT, Verb.OPEN_ISSUE, Verb.CHECK}, SyncOptions)
# ``ping`` exercises the whole gate end to end; it is a module of its own so that no module that
# does real work has to be allowed to run it.
register_module("ping", {Verb.PING})


def policy_problems(policy: Policy, modules: Mapping[str, ModuleSpec] | None = None) -> list[str]:
    """Every reason the (structurally valid) policy is wrong for the registry."""
    registry = MODULES if modules is None else modules
    reasons: list[str] = []
    for name, config in policy.modules.items():
        spec = registry.get(name)
        if spec is None:
            reasons.append(f"modules.{name}: unknown module (known: {_known(registry)})")
            continue
        reasons.extend(
            f"modules.{name}.{reason}" for reason in spec.option_problems(config.options)
        )
    return reasons


def validate_policy(text: str, modules: Mapping[str, ModuleSpec] | None = None) -> Policy:
    """Parse the policy and check it against the registry; :class:`PolicyError` lists reasons."""
    policy = load_policy(text)
    problems = policy_problems(policy, modules)
    if problems:
        raise PolicyError(problems)
    return policy


def authorize(
    policy: Policy | None,
    module: str,
    verb: Verb,
    modules: Mapping[str, ModuleSpec] | None = None,
) -> Decision:
    """The full decision for a validated (or missing) policy, in the documented order."""
    registry = MODULES if modules is None else modules
    spec = registry.get(module)
    if spec is None:
        return Decision(
            False, module, verb.value, f"module {module!r} is not known (known: {_known(registry)})"
        )
    decision = decide(policy, module, verb)
    if not decision.allowed:
        return decision
    if verb not in spec.verbs:
        return Decision(
            False,
            module,
            verb.value,
            f"module {module!r} may not run {verb.value!r}; its verbs are: {spec.verb_names()}",
        )
    return Decision(
        True,
        module,
        verb.value,
        f"module {module!r} is enabled in {POLICY_PATH} and may run {verb.value!r}",
    )


def authorize_module(
    policy: Policy | None, module: str, modules: Mapping[str, ModuleSpec] | None = None
) -> Decision:
    """Whether ``module`` may run at all, for the reads a module command does itself.

    Same order as :func:`authorize` without a verb: unknown module, missing policy, module not
    declared, module disabled. Which verbs the module may call is still decided per verb.
    """
    registry = MODULES if modules is None else modules
    if module not in registry:
        return Decision(
            False, module, "read", f"module {module!r} is not known (known: {_known(registry)})"
        )
    if policy is None:
        return Decision(False, module, "read", f"{POLICY_PATH} is missing in the repository")
    config = policy.modules.get(module)
    if config is None:
        return Decision(
            False, module, "read", f"module {module!r} is not declared in {POLICY_PATH}"
        )
    if not config.enabled:
        return Decision(False, module, "read", f"module {module!r} is disabled in {POLICY_PATH}")
    return Decision(True, module, "read", f"module {module!r} is enabled in {POLICY_PATH}")


def _known(registry: Mapping[str, ModuleSpec]) -> str:
    return ", ".join(sorted(registry)) or "none"
