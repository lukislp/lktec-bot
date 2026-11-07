# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

"""Installation tokens minted from the App's private key, narrowed to one repo and one verb."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import jwt

from lktec_bot.adapters._http_common import DEFAULT_BASE_URL, github_headers
from lktec_bot.ports.key_source import AppCredentials, KeySource
from lktec_bot.ports.token_provider import TokenError

# GitHub accepts at most 10 minutes; 9 minutes plus a 60 s backdated iat absorbs clock skew.
JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 9 * 60


def build_app_jwt(credentials: AppCredentials, now: Callable[[], float] = time.time) -> str:
    """Build the RS256 JWT that authenticates as the App itself (not as an installation)."""
    issued_at = int(now())
    claims = {
        "iat": issued_at - JWT_BACKDATE_SECONDS,
        "exp": issued_at + JWT_LIFETIME_SECONDS,
        "iss": credentials.app_id,
    }
    return jwt.encode(claims, credentials.private_key_pem, algorithm="RS256")


class InstallationToken:
    """A minted token; ``revoke`` is idempotent so the gate can always call it in ``finally``.

    ``permissions`` and ``repositories`` are what GitHub reported in the mint response, or
    ``None`` when it reported nothing: the gate treats unknown scope as too broad.
    """

    def __init__(
        self,
        value: str,
        permissions: Mapping[str, str] | None,
        repositories: frozenset[str] | None,
        expires_at: str,
        revoker: Callable[[str], None],
        actor: str | None = None,
    ) -> None:
        self._value = value
        self._permissions = None if permissions is None else dict(permissions)
        self._repositories = repositories
        self._actor = actor
        self.expires_at = expires_at
        self._revoker = revoker
        self.revoked = False

    @property
    def value(self) -> str:
        return self._value

    @property
    def permissions(self) -> Mapping[str, str] | None:
        return self._permissions

    @property
    def repositories(self) -> frozenset[str] | None:
        return self._repositories

    @property
    def actor(self) -> str | None:
        return self._actor

    def revoke(self) -> None:
        if self.revoked:
            return
        self._revoker(self._value)
        self.revoked = True

    def __repr__(self) -> str:
        return (
            f"InstallationToken(permissions={self._permissions!r}, "
            f"repositories={self._repositories!r}, revoked={self.revoked})"
        )


class AppTokenProvider:
    """:class:`~lktec_bot.ports.token_provider.TokenProvider` backed by the GitHub App API."""

    def __init__(
        self,
        key_source: KeySource,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.Client | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._key_source = key_source
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30.0)
        self._now = now

    @property
    def base_url(self) -> str:
        return self._base_url

    def close(self) -> None:
        self._client.close()

    def token_for(self, owner: str, repo: str, permissions: Mapping[str, str]) -> InstallationToken:
        app_jwt = build_app_jwt(self._key_source.load(), self._now)
        installation_id, app_slug = self._installation(app_jwt, owner, repo)
        response = self._client.post(
            f"{self._base_url}/app/installations/{installation_id}/access_tokens",
            headers=github_headers(f"Bearer {app_jwt}"),
            json={"repositories": [repo], "permissions": dict(permissions)},
        )
        if response.status_code != 201:
            raise TokenError(
                f"minting a token for {owner}/{repo} failed: "
                f"{response.status_code} {response.text.strip()[:200]}"
            )
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("token"), str):
            raise TokenError(f"minting a token for {owner}/{repo} returned no token")
        return InstallationToken(
            value=payload["token"],
            permissions=_permissions(payload.get("permissions")),
            repositories=_repositories(payload.get("repositories")),
            expires_at=str(payload.get("expires_at", "")),
            revoker=self._revoke,
            actor=f"{app_slug}[bot]" if app_slug else None,
        )

    def _installation(self, app_jwt: str, owner: str, repo: str) -> tuple[int, str]:
        """The installation id on ``owner/repo`` and the App's slug (empty when not reported)."""
        response = self._client.get(
            f"{self._base_url}/repos/{owner}/{repo}/installation",
            headers=github_headers(f"Bearer {app_jwt}"),
        )
        if response.status_code == 404:
            raise TokenError(f"the App is not installed on {owner}/{repo}")
        if not response.is_success:
            raise TokenError(
                f"resolving the installation on {owner}/{repo} failed: {response.status_code}"
            )
        payload = response.json()
        slug = payload.get("app_slug")
        return int(payload["id"]), slug if isinstance(slug, str) else ""

    def _revoke(self, token: str) -> None:
        response = self._client.delete(
            f"{self._base_url}/installation/token", headers=github_headers(f"token {token}")
        )
        if response.status_code not in (204, 401):  # 401: already expired or revoked
            raise TokenError(f"revoking the token failed: {response.status_code}")


def _permissions(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    return {str(name): str(access) for name, access in raw.items()}


def _repositories(raw: Any) -> frozenset[str] | None:
    if not isinstance(raw, list):
        return None
    names = {str(entry["name"]) for entry in raw if isinstance(entry, dict) and "name" in entry}
    return frozenset(names)
