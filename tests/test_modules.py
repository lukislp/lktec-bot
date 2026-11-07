# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from lktec_bot.application.modules import (
    MODULES,
    AutomergeOptions,
    LockfilesOptions,
    ModuleSpec,
    ReleaseOptions,
    authorize,
    authorize_module,
    policy_problems,
    register_module,
    validate_policy,
)
from lktec_bot.domain.permissions import Verb
from lktec_bot.domain.policy import POLICY_PATH, PolicyError, load_policy

AUTOMERGE = "version: 1\nmodules:\n  automerge:\n    enabled: true\n"
PING = "version: 1\nmodules:\n  ping:\n    enabled: true\n"


def test_registry_declares_the_shipped_modules() -> None:
    assert set(MODULES) == {"automerge", "lockfiles", "ping", "release", "sync"}
    assert MODULES["release"].verbs == {Verb.RELEASE, Verb.CHECK}
    assert MODULES["release"].options is ReleaseOptions
    assert MODULES["lockfiles"].verbs == {Verb.COMMIT, Verb.COMMENT, Verb.CHECK}
    assert MODULES["lockfiles"].options is LockfilesOptions
    assert MODULES["automerge"].verbs == {
        Verb.APPROVE,
        Verb.ENABLE_AUTOMERGE,
        Verb.UPDATE_BRANCH,
        Verb.COMMENT,
    }
    assert MODULES["automerge"].options is AutomergeOptions
    assert MODULES["ping"].verbs == {Verb.PING}
    assert MODULES["ping"].options is None
    assert MODULES["automerge"].verb_names() == "approve, comment, enable-automerge, update-branch"


def test_register_module_refuses_duplicates() -> None:
    with pytest.raises(ValueError, match="already registered"):
        register_module("ping", {Verb.PING})


def test_register_module_refuses_an_options_model_that_allows_extra_keys() -> None:
    class Loose(BaseModel):
        model_config = ConfigDict(extra="ignore")

    with pytest.raises(ValueError, match="extra='forbid'"):
        register_module("loose", {Verb.PING}, Loose)
    assert "loose" not in MODULES


def test_option_problems_for_a_module_with_options() -> None:
    spec = MODULES["automerge"]

    assert spec.option_problems({}) == []
    assert spec.option_problems({"exclude": ["a"], "majors": "auto"}) == []
    assert spec.option_problems({"majors": "sometimes"}) == [
        "majors: Input should be 'manual' or 'auto'"
    ]
    assert spec.option_problems({"typo": 1}) == ["typo: Extra inputs are not permitted"]


def test_option_problems_for_a_module_without_options() -> None:
    spec = MODULES["ping"]

    assert spec.option_problems({}) == []
    assert spec.option_problems({"b": 1, "a": 2}) == [
        "a: module 'ping' takes no options",
        "b: module 'ping' takes no options",
    ]


def test_validate_policy_accepts_a_good_file() -> None:
    policy = validate_policy(AUTOMERGE + "    exclude: [x]\n    majors: auto\n  ping: {}\n")

    assert policy.modules["automerge"].enabled is True
    assert policy.modules["automerge"].options == {"exclude": ["x"], "majors": "auto"}
    assert policy.modules["ping"].enabled is False


def test_validate_policy_rejects_an_unknown_module() -> None:
    with pytest.raises(PolicyError) as excinfo:
        validate_policy(PING + "  mystery:\n    enabled: true\n")

    assert excinfo.value.reasons == [
        "modules.mystery: unknown module (known: automerge, lockfiles, ping, release, sync)"
    ]


def test_validate_policy_rejects_bad_options_with_the_module_in_the_path() -> None:
    with pytest.raises(PolicyError) as excinfo:
        validate_policy(AUTOMERGE + "    majors: sometimes\n    typo: 1\n  ping:\n    x: 1\n")

    assert excinfo.value.reasons == [
        "modules.automerge.majors: Input should be 'manual' or 'auto'",
        "modules.automerge.typo: Extra inputs are not permitted",
        "modules.ping.x: module 'ping' takes no options",
    ]


def test_validate_policy_passes_structural_errors_through() -> None:
    with pytest.raises(PolicyError, match="version: Field required"):
        validate_policy("modules: {}\n")


def test_policy_problems_with_a_custom_registry() -> None:
    registry = {"mine": ModuleSpec("mine", frozenset({Verb.PING}))}
    policy = load_policy("version: 1\nmodules:\n  mine: {}\n  automerge: {}\n")

    assert policy_problems(policy, registry) == ["modules.automerge: unknown module (known: mine)"]
    assert policy_problems(load_policy("version: 1\n"), {}) == []


def test_authorize_denies_an_unknown_module_with_or_without_a_policy() -> None:
    for policy in (None, load_policy(PING)):
        decision = authorize(policy, "mystery", Verb.PING)

        assert decision.allowed is False
        assert (
            decision.reason
            == "module 'mystery' is not known (known: automerge, lockfiles, ping, release, sync)"
        )
        assert decision.module == "mystery"
        assert decision.verb == "ping"


def test_authorize_denies_a_missing_policy() -> None:
    decision = authorize(None, "ping", Verb.PING)

    assert decision.allowed is False
    assert "missing" in decision.reason


def test_authorize_denies_an_undeclared_module() -> None:
    decision = authorize(load_policy(AUTOMERGE), "ping", Verb.PING)

    assert decision.allowed is False
    assert "not declared" in decision.reason


def test_authorize_denies_a_disabled_module() -> None:
    decision = authorize(load_policy("version: 1\nmodules:\n  ping: {}\n"), "ping", Verb.PING)

    assert decision.allowed is False
    assert "disabled" in decision.reason


def test_authorize_denies_a_verb_outside_the_module_set() -> None:
    decision = authorize(load_policy(AUTOMERGE), "automerge", Verb.RELEASE)

    assert decision.allowed is False
    assert decision.reason == (
        "module 'automerge' may not run 'release'; its verbs are: "
        "approve, comment, enable-automerge, update-branch"
    )


def test_authorize_allows_a_listed_verb_of_an_enabled_module() -> None:
    decision = authorize(load_policy(AUTOMERGE), "automerge", Verb.ENABLE_AUTOMERGE)

    assert decision.allowed is True
    assert decision.reason == (
        f"module 'automerge' is enabled in {POLICY_PATH} and may run 'enable-automerge'"
    )
    assert "allowed" in str(decision)


def test_authorize_with_a_custom_registry() -> None:
    registry = {"mine": ModuleSpec("mine", frozenset({Verb.RELEASE}))}
    policy = load_policy("version: 1\nmodules:\n  mine:\n    enabled: true\n")

    assert authorize(policy, "mine", Verb.RELEASE, registry).allowed is True
    assert authorize(policy, "mine", Verb.PING, registry).allowed is False
    assert "known: none" in authorize(policy, "mine", Verb.PING, {}).reason


# -- automerge options ------------------------------------------------------------------------


def test_automerge_options_defaults() -> None:
    options = AutomergeOptions.model_validate({})

    assert options.majors == "manual"
    assert options.exclude == []
    assert options.workflow_files == "auto"
    assert options.method == "squash"


def test_automerge_options_accept_every_documented_value() -> None:
    options = AutomergeOptions.model_validate(
        {"majors": "auto", "exclude": ["a", "B"], "workflow-files": "manual", "method": "rebase"}
    )

    assert options.majors == "auto"
    assert options.exclude == ["a", "B"]
    assert options.workflow_files == "manual"
    assert options.method == "rebase"


def test_workflow_files_is_spelled_with_a_hyphen_only() -> None:
    spec = MODULES["automerge"]

    assert spec.option_problems({"workflow-files": "manual"}) == []
    assert spec.option_problems({"workflow_files": "manual"}) == [
        "workflow_files: Extra inputs are not permitted"
    ]
    assert spec.option_problems({"workflow-files": "sometimes"}) == [
        "workflow-files: Input should be 'auto' or 'manual'"
    ]


def test_method_and_exclude_are_validated() -> None:
    spec = MODULES["automerge"]

    assert spec.option_problems({"method": "fast-forward"}) == [
        "method: Input should be 'squash', 'merge' or 'rebase'"
    ]
    assert spec.option_problems({"exclude": "pillow"}) == ["exclude: Input should be a valid list"]


def test_authorize_module_follows_the_documented_order() -> None:
    enabled = load_policy(AUTOMERGE)

    assert authorize_module(enabled, "automerge").allowed is True
    assert "may" not in authorize_module(enabled, "automerge").reason
    assert "not known" in authorize_module(enabled, "mystery").reason
    assert "missing" in authorize_module(None, "automerge").reason
    assert "not declared" in authorize_module(load_policy(PING), "automerge").reason
    off = load_policy("version: 1\nmodules:\n  automerge: {}\n")
    assert "disabled" in authorize_module(off, "automerge").reason
    assert (
        authorize_module(enabled, "mine", {"mine": ModuleSpec("mine", frozenset())}).allowed
        is False
    )
