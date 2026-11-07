# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lktec_bot.domain.permissions import Verb
from lktec_bot.domain.policy import (
    POLICY_PATH,
    ModuleConfig,
    Policy,
    PolicyError,
    decide,
    format_validation_errors,
    load_policy,
)

VALID = """
version: 1
modules:
  automerge:
    enabled: true
    exclude: ["some-package"]
    majors: auto
  release:
    enabled: false
"""


def test_valid_policy_parses_and_keeps_options_for_later() -> None:
    policy = load_policy(VALID)

    assert policy.version == 1
    automerge = policy.modules["automerge"]
    assert automerge.enabled is True
    assert automerge.options == {"exclude": ["some-package"], "majors": "auto"}
    release = policy.modules["release"]
    assert release.enabled is False
    assert release.options == {}


def test_defaults_are_closed() -> None:
    policy = load_policy("version: 1\nmodules:\n  automerge: {}\n")

    assert policy.modules["automerge"].enabled is False
    assert policy.modules["automerge"].options == {}


def test_module_without_a_value_is_an_empty_entry() -> None:
    policy = load_policy("version: 1\nmodules:\n  automerge:\n")

    assert policy.modules["automerge"].enabled is False


def test_missing_or_empty_modules_key_means_no_modules() -> None:
    assert load_policy("version: 1\n").modules == {}
    assert load_policy("version: 1\nmodules:\n").modules == {}


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("version: 1\nunknown: true\n", "unknown: Extra inputs are not permitted"),
        ("version: 2\n", "version: Input should be 1"),
        ("modules: {}\n", "version: Field required"),
        ("version: 1\nmodules: [a]\n", "modules: Input should be a valid dictionary"),
        ("version: 1\nmodules:\n  automerge: 1\n", "modules.automerge: Input should be"),
        ("version: 1\nmodules:\n  automerge:\n    enabled: maybe\n", "enabled: Input should be"),
    ],
)
def test_invalid_policy_reports_readable_reasons(text: str, fragment: str) -> None:
    with pytest.raises(PolicyError) as excinfo:
        load_policy(text)

    assert any(fragment in reason for reason in excinfo.value.reasons), excinfo.value.reasons
    assert str(excinfo.value)


# ``yes``/``on`` are not in the list: YAML 1.1 (PyYAML) already parses them as booleans.
@pytest.mark.parametrize("value", ["1", "'true'", "0", "''", "[]", "null"])
def test_enabled_is_a_strict_boolean(value: str) -> None:
    with pytest.raises(PolicyError, match=r"modules\.automerge\.enabled: Input should be"):
        load_policy(f"version: 1\nmodules:\n  automerge:\n    enabled: {value}\n")


def test_module_options_are_not_validated_by_the_domain() -> None:
    policy = load_policy("version: 1\nmodules:\n  anything:\n    enabled: true\n    typo: 1\n")

    assert policy.modules["anything"].options == {"typo": 1}


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "empty"),
        ("- just\n- a list\n", "must be a mapping"),
        ("version: [1\n", "not valid YAML"),
    ],
)
def test_structurally_broken_files(text: str, fragment: str) -> None:
    with pytest.raises(PolicyError) as excinfo:
        load_policy(text)

    assert fragment in excinfo.value.reasons[0]


def test_enabled_module_is_allowed_by_the_domain() -> None:
    policy = load_policy(VALID)

    decision = policy.allows("automerge", Verb.ENABLE_AUTOMERGE)

    assert decision.allowed is True
    assert decision.module == "automerge"
    assert decision.verb == "enable-automerge"
    assert "enabled" in decision.reason
    assert "allowed" in str(decision)


def test_disabled_module_is_denied() -> None:
    decision = load_policy(VALID).allows("release", "release")

    assert decision.allowed is False
    assert "disabled" in decision.reason
    assert POLICY_PATH in decision.reason


def test_undeclared_module_is_denied() -> None:
    decision = load_policy(VALID).allows("deploy", Verb.DEPLOYMENT)

    assert decision.allowed is False
    assert "not declared" in decision.reason


def test_unknown_verb_is_denied() -> None:
    decision = load_policy(VALID).allows("automerge", "merge")

    assert decision.allowed is False
    assert "unknown verb" in decision.reason


def test_missing_file_denies_everything() -> None:
    decision = decide(None, "automerge", Verb.APPROVE)

    assert decision.allowed is False
    assert "missing" in decision.reason
    assert POLICY_PATH in decision.reason


def test_decide_delegates_to_policy() -> None:
    automerge = ModuleConfig.model_validate({"enabled": True, "majors": "auto"})
    policy = Policy(version=1, modules={"automerge": automerge})

    assert decide(policy, "automerge", Verb.APPROVE).allowed is True
    assert policy.modules["automerge"].options == {"majors": "auto"}


def test_format_validation_errors_prefixes_locations() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ModuleConfig.model_validate({"enabled": "no"})

    assert format_validation_errors(excinfo.value, ("modules", "x")) == [
        "modules.x.enabled: Input should be a valid boolean"
    ]
