# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Mapping

import pytest

from lktec_bot.application.gate import (
    POLICY_READ_PERMISSIONS,
    TOKEN_NOT_REVOKED,
    Gate,
    VerbOutcome,
    VerbRequest,
    VerbResult,
    parse_repository,
)
from lktec_bot.application.verbs import handlers
from lktec_bot.domain.permissions import Target, Verb, as_token_request, permissions_for
from lktec_bot.domain.policy import POLICY_PATH
from lktec_bot.ports.github_api import GitHubApi
from lktec_bot.ports.token_provider import TokenError
from tests.fakes import FakeApiFactory, FakeTokenProvider

ENABLED = "version: 1\nmodules:\n  ping:\n    enabled: true\n"
DISABLED = "version: 1\nmodules:\n  ping:\n    enabled: false\n"
AUTOMERGE = "version: 1\nmodules:\n  automerge:\n    enabled: true\n"


def request(
    verb: Verb = Verb.PING, module: str = "ping", args: Mapping[str, object] | None = None
) -> VerbRequest:
    return VerbRequest(owner="acme", repo="widgets", module=module, verb=verb, args=args or {})


def make_gate(
    policy_text: str | None,
    *,
    handlers_map: Mapping[Verb, object] | None = None,
    caller_token: str | None = None,
    tokens: FakeTokenProvider | None = None,
    mask: list[str] | None = None,
) -> tuple[Gate, FakeTokenProvider, FakeApiFactory]:
    files = {POLICY_PATH: policy_text} if policy_text is not None else {}
    provider = tokens if tokens is not None else FakeTokenProvider()
    factory = FakeApiFactory(files)
    registry = handlers_map if handlers_map is not None else handlers()
    gate = Gate(
        provider,
        factory,
        registry,  # type: ignore[arg-type]
        caller_token=caller_token,
        mask=mask.append if mask is not None else None,
    )
    return gate, provider, factory


def comment(api: GitHubApi, req: VerbRequest) -> VerbResult:
    return VerbResult(VerbOutcome.OK, "commented", url="https://example.invalid/c/1")


# -- the happy path ------------------------------------------------------------------------


def test_allowed_verb_runs_with_a_token_narrowed_to_its_permissions() -> None:
    gate, tokens, factory = make_gate(ENABLED)

    result = gate.run(request())

    assert result.outcome is VerbOutcome.OK
    assert result.ok
    assert result.message == "pong from acme/widgets"
    assert result.warnings == ()
    # Exactly two tokens: one to read the policy, one for the verb.
    assert [permissions for _, _, permissions in tokens.requests] == [
        dict(POLICY_READ_PERMISSIONS),
        as_token_request(permissions_for(Verb.PING)),
    ]
    assert all(token.revoked for token in tokens.minted)
    assert all(token.revoke_calls == 1 for token in tokens.minted)
    # The verb's client is bound to the verb token, the policy read to the policy token.
    assert [api.token for api in factory.created] == ["tok-0", "tok-1"]
    assert factory.created[0].calls == [
        ("get_file_content", ("acme", "widgets", POLICY_PATH, None))
    ]
    assert factory.created[1].calls == []
    assert all(api.closed for api in factory.created)


def test_policy_is_read_from_the_default_branch_never_from_a_head() -> None:
    gate, _, factory = make_gate(ENABLED)
    req = request(args={"ref": "refs/pull/7/head", "head_sha": "deadbeef"})

    assert gate.run(req).ok
    # No ref on the read: a pull request cannot bring its own policy along.
    assert factory.created[0].calls == [
        ("get_file_content", ("acme", "widgets", POLICY_PATH, None))
    ]


def test_handler_sees_the_actor_of_the_verb_token() -> None:
    seen: list[str | None] = []

    def who(api: GitHubApi, req: VerbRequest) -> VerbResult:
        seen.append(req.actor)
        return VerbResult(VerbOutcome.OK, "")

    gate, _, _ = make_gate(ENABLED, handlers_map={Verb.PING: who})
    assert gate.run(request()).ok
    assert seen == ["fake-app[bot]"]

    # A provider that does not know the actor hands over None; the request itself has none.
    gate, _, _ = make_gate(
        ENABLED, handlers_map={Verb.PING: who}, tokens=FakeTokenProvider(actor=None)
    )
    assert gate.run(request()).ok
    assert seen == ["fake-app[bot]", None]
    assert request().actor is None


def test_close_closes_the_token_provider() -> None:
    gate, tokens, _ = make_gate(ENABLED)

    gate.close()

    assert tokens.closed


# -- decisions ---------------------------------------------------------------------------


def test_unknown_module_is_denied_before_any_token_is_minted() -> None:
    gate, tokens, _ = make_gate(ENABLED)

    result = gate.run(request(module="mystery"))

    assert result.outcome is VerbOutcome.DENIED
    assert "module 'mystery' is not known" in result.message
    assert tokens.requests == []


def test_disabled_module_is_denied_and_no_verb_token_is_minted() -> None:
    gate, tokens, _ = make_gate(DISABLED)

    result = gate.run(request())

    assert result.outcome is VerbOutcome.DENIED
    assert "disabled" in result.message
    assert [permissions for _, _, permissions in tokens.requests] == [dict(POLICY_READ_PERMISSIONS)]
    assert tokens.minted[0].revoked


def test_missing_policy_file_is_denied() -> None:
    gate, tokens, _ = make_gate(None)

    result = gate.run(request())

    assert result.outcome is VerbOutcome.DENIED
    assert "missing" in result.message
    assert len(tokens.minted) == 1
    assert tokens.minted[0].revoked


def test_undeclared_module_is_denied() -> None:
    gate, _, _ = make_gate(AUTOMERGE)

    result = gate.run(request(module="ping"))

    assert result.outcome is VerbOutcome.DENIED
    assert "not declared" in result.message


def test_enabled_module_cannot_run_a_verb_outside_its_set() -> None:
    gate, tokens, _ = make_gate(AUTOMERGE)

    result = gate.run(request(verb=Verb.PING, module="automerge"))

    assert result.outcome is VerbOutcome.DENIED
    assert "module 'automerge' may not run 'ping'" in result.message
    assert "approve" in result.message
    assert len(tokens.minted) == 1  # policy read only


def test_invalid_policy_file_is_denied_with_reasons() -> None:
    gate, tokens, _ = make_gate("version: 1\nmodules:\n  automerge:\n    enabled: true\n    x: 1\n")

    result = gate.run(request())

    assert result.outcome is VerbOutcome.DENIED
    assert "invalid" in result.message
    assert "modules.automerge.x: Extra inputs are not permitted" in result.message
    assert len(tokens.minted) == 1


def test_unknown_module_in_the_policy_file_denies_everything() -> None:
    gate, _, _ = make_gate(ENABLED + "  mystery:\n    enabled: true\n")

    result = gate.run(request())

    assert result.outcome is VerbOutcome.DENIED
    assert "modules.mystery: unknown module" in result.message


def test_invalid_module_options_deny_everything() -> None:
    gate, _, _ = make_gate(ENABLED + "  automerge:\n    enabled: true\n    majors: sometimes\n")

    result = gate.run(request())

    assert result.outcome is VerbOutcome.DENIED
    assert "modules.automerge.majors: Input should be 'manual' or 'auto'" in result.message


def test_unregistered_verb_is_an_error_before_any_token() -> None:
    gate, tokens, _ = make_gate(ENABLED, handlers_map={})

    result = gate.run(request())

    assert result.outcome is VerbOutcome.ERROR
    assert "no handler" in result.message
    assert tokens.requests == []


def test_comment_without_target_is_an_error_after_policy() -> None:
    gate, tokens, _ = make_gate(AUTOMERGE, handlers_map={Verb.COMMENT: comment})

    result = gate.run(request(verb=Verb.COMMENT, module="automerge"))

    assert result.outcome is VerbOutcome.ERROR
    assert "target" in result.message
    assert len(tokens.minted) == 1  # policy read only


def test_comment_target_selects_the_permission() -> None:
    gate, tokens, _ = make_gate(AUTOMERGE, handlers_map={Verb.COMMENT: comment})
    req = VerbRequest("acme", "widgets", "automerge", Verb.COMMENT, target=Target.ISSUE)

    result = gate.run(req)

    assert result.ok
    assert tokens.requests[-1][2] == {"issues": "write"}
    assert result.url == "https://example.invalid/c/1"


# -- failures become results ---------------------------------------------------------------


def test_handler_exception_becomes_error_and_token_is_still_revoked() -> None:
    def boom(api: GitHubApi, req: VerbRequest) -> VerbResult:
        raise RuntimeError("kaboom")

    gate, tokens, factory = make_gate(ENABLED, handlers_map={Verb.PING: boom})

    result = gate.run(request())

    assert result.outcome is VerbOutcome.ERROR
    assert "kaboom" in result.message
    assert len(tokens.minted) == 2
    assert all(token.revoked for token in tokens.minted)
    assert all(api.closed for api in factory.created)


def test_policy_read_failure_is_an_error_not_a_crash() -> None:
    class Broken:
        last_rate_limit = None

        def __init__(self, token: str) -> None:
            self.token = token

        def get_file_content(self, *args: object, **kwargs: object) -> str | None:
            raise ConnectionError("api down")

        def close(self) -> None:
            pass

    tokens = FakeTokenProvider()
    gate = Gate(tokens, Broken, handlers())  # type: ignore[arg-type]

    result = gate.run(request())

    assert result.outcome is VerbOutcome.ERROR
    assert "could not read" in result.message
    assert "ConnectionError: api down" in result.message
    assert tokens.minted[0].revoked


def test_provider_error_while_minting_is_reported_as_such() -> None:
    gate, tokens, _ = make_gate(ENABLED, caller_token="caller")
    tokens.fail_with = TokenError("the App is not installed on acme/widgets")

    result = gate.run(request())

    assert result.outcome is VerbOutcome.ERROR
    assert result.message == "could not mint a token: the App is not installed on acme/widgets"


def test_provider_bug_while_minting_is_distinguished_from_a_provider_error() -> None:
    gate, tokens, _ = make_gate(ENABLED, caller_token="caller")
    tokens.fail_with = KeyError("token")

    result = gate.run(request())

    assert result.outcome is VerbOutcome.ERROR
    assert result.message.startswith("unexpected error while minting a token (KeyError)")


def test_policy_token_minting_failure_is_an_error() -> None:
    gate, tokens, _ = make_gate(ENABLED)
    tokens.fail_with = TokenError("no installation")

    result = gate.run(request())

    assert result.outcome is VerbOutcome.ERROR
    assert result.message == "could not mint the policy token: no installation"


def test_caller_token_reads_the_policy_without_minting() -> None:
    gate, tokens, factory = make_gate(ENABLED, caller_token="ghs_caller")

    result = gate.run(request())

    assert result.ok
    assert [permissions for _, _, permissions in tokens.requests] == [
        as_token_request(permissions_for(Verb.PING))
    ]
    assert factory.created[0].token == "ghs_caller"
    assert factory.created[0].closed
    assert factory.created[1].token == "tok-0"


# -- revoke never replaces the result -------------------------------------------------------


def test_revoke_is_retried_once_and_succeeds_silently() -> None:
    gate, tokens, _ = make_gate(ENABLED, tokens=FakeTokenProvider(failing_revokes=1))

    result = gate.run(request())

    assert result.ok
    assert result.warnings == ()
    assert [token.revoke_calls for token in tokens.minted] == [2, 2]
    assert all(token.revoked for token in tokens.minted)


def test_revoke_failure_on_the_verb_token_becomes_a_warning() -> None:
    tokens = FakeTokenProvider(failing_revokes=2)
    gate, _, _ = make_gate(ENABLED, caller_token="caller", tokens=tokens)

    result = gate.run(request())

    assert result.outcome is VerbOutcome.OK
    assert result.message == "pong from acme/widgets"
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith(TOKEN_NOT_REVOKED)
    assert "attempt 2" in result.warnings[0]
    assert tokens.minted[0].revoke_calls == 2
    assert not tokens.minted[0].revoked


def test_revoke_failure_on_the_policy_token_keeps_the_denial() -> None:
    tokens = FakeTokenProvider(failing_revokes=2)
    gate, _, _ = make_gate(DISABLED, tokens=tokens)

    result = gate.run(request())

    assert result.outcome is VerbOutcome.DENIED
    assert "disabled" in result.message
    assert len(result.warnings) == 1
    assert TOKEN_NOT_REVOKED in result.warnings[0]


def test_revoke_failures_on_both_tokens_are_both_reported() -> None:
    tokens = FakeTokenProvider(failing_revokes=5)
    gate, _, _ = make_gate(ENABLED, tokens=tokens)

    result = gate.run(request())

    assert result.ok
    assert len(result.warnings) == 2
    assert [token.revoke_calls for token in tokens.minted] == [2, 2]


# -- the token must not be broader than requested -------------------------------------------


def test_verb_token_with_extra_permissions_is_refused_and_revoked() -> None:
    tokens = FakeTokenProvider(
        permissions_by_mint={1: {"metadata": "read", "contents": "write"}},
    )
    gate, _, factory = make_gate(ENABLED, tokens=tokens)

    result = gate.run(request())

    assert result.outcome is VerbOutcome.ERROR
    assert "verb token refused" in result.message
    assert "contents:write" in result.message
    assert tokens.minted[1].revoked
    assert len(factory.created) == 1  # no client was ever bound to the refused token


def test_verb_token_with_a_higher_access_level_is_refused() -> None:
    tokens = FakeTokenProvider(permissions_by_mint={1: {"metadata": "write"}})
    gate, _, _ = make_gate(ENABLED, tokens=tokens)

    result = gate.run(request())

    assert result.outcome is VerbOutcome.ERROR
    assert "metadata:write" in result.message


def test_policy_token_with_extra_permissions_is_refused_and_revoked() -> None:
    tokens = FakeTokenProvider(
        permissions_by_mint={0: {"metadata": "read", "contents": "read", "issues": "write"}},
    )
    gate, _, factory = make_gate(ENABLED, tokens=tokens)

    result = gate.run(request())

    assert result.outcome is VerbOutcome.ERROR
    assert result.message.startswith("could not mint the policy token: policy token refused")
    assert "issues:write" in result.message
    assert tokens.minted[0].revoked
    assert factory.created == []


def test_token_for_another_repository_is_refused() -> None:
    tokens = FakeTokenProvider(repositories_by_mint={1: frozenset({"widgets", "other"})})
    gate, _, _ = make_gate(ENABLED, tokens=tokens)

    result = gate.run(request())

    assert result.outcome is VerbOutcome.ERROR
    assert "scoped to ['other', 'widgets'] instead of 'widgets'" in result.message
    assert tokens.minted[1].revoked


def test_unknown_permissions_fail_closed() -> None:
    tokens = FakeTokenProvider(permissions_by_mint={1: None})
    gate, _, _ = make_gate(ENABLED, tokens=tokens)

    result = gate.run(request())

    assert result.outcome is VerbOutcome.ERROR
    assert "did not report the token's permissions" in result.message


def test_unknown_repositories_fail_closed() -> None:
    tokens = FakeTokenProvider(repositories_by_mint={1: None})
    gate, _, _ = make_gate(ENABLED, tokens=tokens)

    result = gate.run(request())

    assert result.outcome is VerbOutcome.ERROR
    assert "did not report the token's repositories" in result.message


def test_narrower_token_and_case_insensitive_repository_are_fine() -> None:
    tokens = FakeTokenProvider(
        permissions_by_mint={1: {"metadata": "read"}},
        repositories_by_mint={1: frozenset({"Widgets"})},
    )
    gate, _, _ = make_gate(ENABLED, tokens=tokens)

    assert gate.run(request()).ok


def test_every_minted_token_is_masked_before_use() -> None:
    masked: list[str] = []
    gate, _, _ = make_gate(ENABLED, mask=masked)

    assert gate.run(request()).ok
    assert masked == ["tok-0", "tok-1"]


# -- request validation --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("a/../b", "exactly one slash"),
        ("a/b/c", "exactly one slash"),
        ("", "exactly one slash"),
        ("no-slash", "exactly one slash"),
        ("/b", "owner must not be empty"),
        ("a/", "repository name must not be empty"),
        ("a/..", "must not be '..'"),
        ("./b", "must not be '.'"),
        ("a b/c", "may only contain"),
        ("a/b%2Fc", "may only contain"),
    ],
)
def test_parse_repository_rejects_bad_input(text: str, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        parse_repository(text)


def test_parse_repository_accepts_github_names() -> None:
    assert parse_repository("acme-inc/widgets.js") == ("acme-inc", "widgets.js")
    assert parse_repository("acme/.github") == ("acme", ".github")


@pytest.mark.parametrize(("owner", "repo"), [("..", "b"), ("a", "b/c"), ("", "b"), ("a", "")])
def test_verb_request_validates_owner_and_repo(owner: str, repo: str) -> None:
    with pytest.raises(ValueError):
        VerbRequest(owner=owner, repo=repo, module="ping", verb=Verb.PING)


# -- rendering -----------------------------------------------------------------------------


def test_result_markdown_mentions_verb_repo_url_and_warnings() -> None:
    req = request()
    result = VerbResult(VerbOutcome.OK, "pong", url="https://example.invalid").with_warnings(
        "token not revoked"
    )

    text = result.to_markdown(req)

    assert "**OK** `ping` on `acme/widgets` (ping): pong" in text
    assert "<https://example.invalid>" in text
    assert "Warning: token not revoked" in text
    assert text.endswith("\n")
    assert "DENIED" in VerbResult(VerbOutcome.DENIED, "no").to_markdown(req)
    assert "ERROR" in VerbResult(VerbOutcome.ERROR, "no").to_markdown(req)


def test_result_markdown_escapes_free_text() -> None:
    req = request(module="evil_module")
    result = VerbResult(
        VerbOutcome.ERROR,
        "`code` *bold* [link](x) <b>\n::set-env name=A::b",
        url="https://example.invalid/<x>\n",
    )

    text = result.to_markdown(req)

    assert "(evil\\_module)" in text
    assert "\\`code\\` \\*bold\\* \\[link\\](x) \\<b\\> : :set-env name=A: :b" in text
    assert "<https://example.invalid/x>" in text
    assert "\n::" not in text
