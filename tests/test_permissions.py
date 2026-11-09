# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from lktec_bot.domain.permissions import (
    Grant,
    Permission,
    Target,
    Verb,
    as_token_request,
    permissions_for,
)


@pytest.mark.parametrize("verb", [verb for verb in Verb if verb is not Verb.COMMENT])
def test_every_verb_has_a_permission_entry(verb: Verb) -> None:
    grants = permissions_for(verb)

    assert grants, f"{verb} must request at least one permission"
    assert all(isinstance(grant, Grant) for grant in grants)


def test_table_matches_the_design() -> None:
    assert as_token_request(permissions_for(Verb.PING)) == {"metadata": "read"}
    assert as_token_request(permissions_for(Verb.APPROVE)) == {"pull_requests": "write"}
    # Verified against a real repository: the mutation needs contents:write too.
    assert as_token_request(permissions_for(Verb.ENABLE_AUTOMERGE)) == {
        "contents": "write",
        "pull_requests": "write",
    }
    assert as_token_request(permissions_for(Verb.UPDATE_BRANCH)) == {
        "contents": "write",
        "pull_requests": "write",
        "workflows": "write",
    }
    assert as_token_request(permissions_for(Verb.COMMIT)) == {"contents": "write"}
    assert as_token_request(permissions_for(Verb.OPEN_PR)) == {"pull_requests": "write"}
    assert as_token_request(permissions_for(Verb.RELEASE)) == {"contents": "write"}
    assert as_token_request(permissions_for(Verb.CHECK)) == {"checks": "write"}
    assert as_token_request(permissions_for(Verb.OPEN_ISSUE)) == {"issues": "write"}
    assert as_token_request(permissions_for(Verb.DEPLOYMENT)) == {"deployments": "write"}


def test_comment_permission_depends_on_target() -> None:
    assert as_token_request(permissions_for(Verb.COMMENT, Target.PULL_REQUEST)) == {
        "pull_requests": "write"
    }
    assert as_token_request(permissions_for(Verb.COMMENT, Target.ISSUE)) == {"issues": "write"}


def test_comment_without_target_is_an_error() -> None:
    with pytest.raises(ValueError, match="needs a target"):
        permissions_for(Verb.COMMENT)


def test_target_is_ignored_for_other_verbs() -> None:
    assert permissions_for(Verb.APPROVE, Target.ISSUE) == permissions_for(Verb.APPROVE)


def test_workflows_permission_is_granted_to_update_branch_alone() -> None:
    workflows = Grant(Permission.WORKFLOWS, "write")

    assert Permission.WORKFLOWS.value == "workflows"
    assert workflows in permissions_for(Verb.UPDATE_BRANCH)
    for verb in Verb:
        if verb is Verb.UPDATE_BRANCH:
            continue
        targets = list(Target) if verb is Verb.COMMENT else [None]
        for target in targets:
            granted = {grant.permission for grant in permissions_for(verb, target)}
            assert Permission.WORKFLOWS not in granted, f"{verb} must not get workflows"
            assert "workflows" not in as_token_request(permissions_for(verb, target))


def test_grant_renders_as_name_colon_access() -> None:
    assert str(Grant(Permission.CONTENTS, "write")) == "contents:write"
