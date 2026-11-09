# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Mapping

import pytest

from lktec_bot.application import verbs
from lktec_bot.application.gate import VerbOutcome, VerbRequest, VerbResult
from lktec_bot.application.verbs import REGISTRY, Option, handlers, load_verbs, register
from lktec_bot.application.verbs.ping import ping
from lktec_bot.domain.permissions import (
    Grant,
    Permission,
    Target,
    Verb,
    as_token_request,
    permissions_for,
)
from lktec_bot.ports.github_api import GitHubApi


def handler(api: GitHubApi, request: VerbRequest) -> VerbResult:
    return VerbResult(VerbOutcome.OK, "")


def test_load_verbs_discovers_every_module_once() -> None:
    first = load_verbs()
    second = load_verbs()

    assert first is second is REGISTRY
    assert Verb.PING in first
    assert first[Verb.PING].handler is ping


def test_the_phase_one_verbs_ship_in_this_release() -> None:
    # The next release that adds a verb updates this line on purpose.
    assert set(load_verbs()) == {
        Verb.PING,
        Verb.COMMENT,
        Verb.APPROVE,
        Verb.ENABLE_AUTOMERGE,
        Verb.UPDATE_BRANCH,
        Verb.COMMIT,
        Verb.RELEASE,
        Verb.OPEN_PR,
        Verb.OPEN_ISSUE,
        Verb.CHECK,
    }


def test_every_shipped_verb_declares_its_options() -> None:
    registry = load_verbs()

    assert registry[Verb.PING].options == ()
    assert [option.name for option in registry[Verb.COMMENT].options] == [
        "number",
        "body",
        "marker",
    ]
    assert [option.name for option in registry[Verb.APPROVE].options] == ["number", "body"]
    assert [option.name for option in registry[Verb.ENABLE_AUTOMERGE].options] == [
        "number",
        "method",
        "attempts",
    ]
    assert [option.name for option in registry[Verb.UPDATE_BRANCH].options] == ["number"]
    assert [option.name for option in registry[Verb.RELEASE].options] == [
        "tag",
        "sha",
        "name",
        "body",
        "latest",
        "prerelease",
    ]
    assert [option.name for option in registry[Verb.OPEN_PR].options] == [
        "head",
        "base",
        "title",
        "body",
        "draft",
    ]
    assert [option.name for option in registry[Verb.OPEN_ISSUE].options] == [
        "title",
        "body",
        "marker",
        "label",
        "reopen",
    ]
    assert [option.name for option in registry[Verb.CHECK].options] == [
        "sha",
        "name",
        "title",
        "summary",
        "text",
        "status",
        "conclusion",
        "details_url",
        "external_id",
        "annotations_file",
    ]
    assert registry[Verb.COMMENT].takes_target
    assert not registry[Verb.APPROVE].takes_target


def test_option_validates_its_shape() -> None:
    assert Option("body_file", "x").flag == "--body-file"
    assert Option("method", "x", kind="choice", choices=("a", "b")).choices == ("a", "b")
    with pytest.raises(ValueError, match="snake_case"):
        Option("Body-File", "x")
    with pytest.raises(ValueError, match="choices go with kind='choice'"):
        Option("m", "x", kind="str", choices=("a",))
    with pytest.raises(ValueError, match="choices go with kind='choice'"):
        Option("m", "x", kind="choice")
    assert Option("from_branch", "x", cli_flag="--from").flag == "--from"
    assert Option("latest", "x", kind="flag", default=True).default is True
    with pytest.raises(ValueError, match="default goes with kind='flag'"):
        Option("m", "x", default=True)


def test_register_refuses_duplicate_option_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verbs, "REGISTRY", dict(REGISTRY))
    options = (Option("number", "x"), Option("number", "y"))

    with pytest.raises(ValueError, match="declares an option twice"):
        register(Verb.DEPLOYMENT, grants=permissions_for(Verb.DEPLOYMENT), options=options)(handler)


def test_every_registered_verb_has_one_handler_whose_grants_match_the_table() -> None:
    for verb, spec in load_verbs().items():
        assert spec.verb is verb
        assert callable(spec.handler)
        if verb is Verb.COMMENT:
            assert spec.grants == {target: permissions_for(verb, target) for target in Target}
        else:
            assert spec.grants == permissions_for(verb)


def test_handlers_map_is_what_the_gate_takes() -> None:
    assert handlers() == {verb: spec.handler for verb, spec in load_verbs().items()}
    assert handlers()[Verb.PING] is ping


def test_workflows_permission_is_registered_for_update_branch_only() -> None:
    for verb, spec in load_verbs().items():
        grant_sets = (
            list(spec.grants.values()) if isinstance(spec.grants, Mapping) else [spec.grants]
        )
        for grant_set in grant_sets:
            has_workflows = "workflows" in as_token_request(grant_set)
            assert has_workflows is (verb is Verb.UPDATE_BRANCH), verb
    assert as_token_request(load_verbs()[Verb.UPDATE_BRANCH].grants) == {  # type: ignore[arg-type]
        "contents": "write",
        "pull_requests": "write",
        "workflows": "write",
    }
    assert Permission.WORKFLOWS.value == "workflows"


def test_register_refuses_duplicates() -> None:
    with pytest.raises(ValueError, match="already registered"):
        register(Verb.PING, grants=permissions_for(Verb.PING))(handler)


def test_register_refuses_grants_that_differ_from_the_table() -> None:
    with pytest.raises(ValueError, match="declares grants pull_requests:write but the table"):
        register(Verb.DEPLOYMENT, grants=frozenset({Grant(Permission.PULL_REQUESTS, "write")}))(
            handler
        )
    assert Verb.DEPLOYMENT not in REGISTRY


def test_register_checks_per_target_grants_for_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = dict(REGISTRY)
    del scratch[Verb.COMMENT]
    monkeypatch.setattr(verbs, "REGISTRY", scratch)

    with pytest.raises(ValueError, match="issue: issues:write; pull_request: pull_requests:write"):
        register(Verb.COMMENT, grants=frozenset({Grant(Permission.ISSUES, "write")}))(handler)
    with pytest.raises(ValueError, match="declares grants issue: issues:write but the table"):
        register(Verb.COMMENT, grants={Target.ISSUE: permissions_for(Verb.COMMENT, Target.ISSUE)})(
            handler
        )

    per_target = {target: permissions_for(Verb.COMMENT, target) for target in Target}
    register(Verb.COMMENT, grants=per_target)(handler)

    assert verbs.REGISTRY[Verb.COMMENT].grants == per_target
    assert REGISTRY[Verb.COMMENT].handler is not handler  # the real registry is untouched
