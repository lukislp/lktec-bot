# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import httpx
import jwt
import pytest
import respx

from lktec_bot.adapters.app_token import AppTokenProvider
from lktec_bot.ports.token_provider import TokenError
from tests.conftest import StaticKeySource

BASE = "https://api.github.test"


@pytest.fixture
def provider(key_source: StaticKeySource) -> AppTokenProvider:
    return AppTokenProvider(key_source, base_url=BASE, now=lambda: 1_700_000_000.0)


@respx.mock(base_url=BASE, assert_all_called=True)
def test_mints_a_token_narrowed_to_repo_and_permissions(
    provider: AppTokenProvider, rsa_key_pair: tuple[str, str], respx_mock: respx.MockRouter
) -> None:
    installation = respx_mock.get("/repos/acme/widgets/installation").respond(
        200, json={"id": 777, "app_slug": "my-bot"}
    )
    mint = respx_mock.post("/app/installations/777/access_tokens").respond(
        201,
        json={
            "token": "ghs_narrow",
            "expires_at": "2024-01-01T00:00:00Z",
            "permissions": {"pull_requests": "write", "metadata": "read"},
            "repositories": [{"name": "widgets", "full_name": "acme/widgets"}],
        },
    )
    revoke = respx_mock.delete("/installation/token").respond(204)

    token = provider.token_for("acme", "widgets", {"pull_requests": "write"})

    assert token.value == "ghs_narrow"
    assert token.expires_at == "2024-01-01T00:00:00Z"
    assert token.permissions == {"pull_requests": "write", "metadata": "read"}
    assert token.repositories == frozenset({"widgets"})
    assert token.actor == "my-bot[bot]"
    assert "ghs_narrow" not in repr(token)
    assert "widgets" in repr(token)

    body = json.loads(mint.calls[0].request.content)
    assert body == {"repositories": ["widgets"], "permissions": {"pull_requests": "write"}}

    # Both App calls carry the RS256 JWT, not a token.
    for route in (installation, mint):
        auth = route.calls[0].request.headers["authorization"]
        assert auth.startswith("Bearer ")
        claims = jwt.decode(
            auth.removeprefix("Bearer "),
            rsa_key_pair[1],
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims["iss"] == "12345"
        assert route.calls[0].request.headers["user-agent"].startswith("lktec-bot/")
        assert route.calls[0].request.headers["x-github-api-version"] == "2022-11-28"

    token.revoke()
    token.revoke()  # idempotent

    assert revoke.call_count == 1
    assert revoke.calls[0].request.headers["authorization"] == "token ghs_narrow"
    assert token.revoked


@respx.mock(base_url=BASE)
def test_scope_the_api_does_not_report_is_unknown_not_assumed(
    provider: AppTokenProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("/repos/acme/widgets/installation").respond(200, json={"id": 1})
    respx_mock.post("/app/installations/1/access_tokens").respond(201, json={"token": "t"})

    token = provider.token_for("acme", "widgets", {"metadata": "read"})

    assert token.permissions is None
    assert token.repositories is None
    assert token.actor is None  # no app_slug in the installation answer


@respx.mock(base_url=BASE)
def test_odd_scope_shapes_are_unknown_or_ignored(
    provider: AppTokenProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("/repos/acme/widgets/installation").respond(200, json={"id": 1})
    respx_mock.post("/app/installations/1/access_tokens").respond(
        201,
        json={"token": "t", "permissions": ["not", "a", "dict"], "repositories": [{"x": 1}, 3]},
    )

    token = provider.token_for("acme", "widgets", {"metadata": "read"})

    assert token.permissions is None
    assert token.repositories == frozenset()


@respx.mock(base_url=BASE)
def test_response_without_a_token_is_a_token_error(
    provider: AppTokenProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("/repos/acme/widgets/installation").respond(200, json={"id": 1})
    respx_mock.post("/app/installations/1/access_tokens").respond(201, json={"expires_at": "x"})

    with pytest.raises(TokenError, match="returned no token"):
        provider.token_for("acme", "widgets", {"metadata": "read"})


@respx.mock(base_url=BASE)
def test_app_not_installed_is_a_token_error(
    provider: AppTokenProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("/repos/acme/widgets/installation").respond(404, json={"message": "Not Found"})

    with pytest.raises(TokenError, match="not installed"):
        provider.token_for("acme", "widgets", {"metadata": "read"})


@respx.mock(base_url=BASE)
def test_installation_lookup_failure_is_a_token_error(
    provider: AppTokenProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("/repos/acme/widgets/installation").respond(500)

    with pytest.raises(TokenError, match="resolving the installation"):
        provider.token_for("acme", "widgets", {"metadata": "read"})


@respx.mock(base_url=BASE)
def test_mint_failure_is_a_token_error(
    provider: AppTokenProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("/repos/acme/widgets/installation").respond(200, json={"id": 1})
    respx_mock.post("/app/installations/1/access_tokens").respond(
        422, json={"message": "permission not granted"}
    )

    with pytest.raises(TokenError, match="422"):
        provider.token_for("acme", "widgets", {"workflows": "write"})


@respx.mock(base_url=BASE)
def test_revoke_tolerates_an_already_expired_token(
    provider: AppTokenProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("/repos/acme/widgets/installation").respond(200, json={"id": 1})
    respx_mock.post("/app/installations/1/access_tokens").respond(201, json={"token": "t"})
    respx_mock.delete("/installation/token").respond(401)

    token = provider.token_for("acme", "widgets", {"metadata": "read"})
    token.revoke()

    assert token.revoked


@respx.mock(base_url=BASE)
def test_revoke_failure_is_a_token_error(
    provider: AppTokenProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("/repos/acme/widgets/installation").respond(200, json={"id": 1})
    respx_mock.post("/app/installations/1/access_tokens").respond(201, json={"token": "t"})
    respx_mock.delete("/installation/token").respond(500)

    token = provider.token_for("acme", "widgets", {"metadata": "read"})

    with pytest.raises(TokenError, match="revoking"):
        token.revoke()
    assert not token.revoked


def test_default_client_is_created_when_none_is_given_and_closed(
    key_source: StaticKeySource,
) -> None:
    provider = AppTokenProvider(key_source)

    assert isinstance(provider._client, httpx.Client)
    assert provider.base_url == "https://api.github.com"
    provider.close()
    assert provider._client.is_closed
