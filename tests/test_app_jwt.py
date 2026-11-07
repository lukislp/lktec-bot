# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import jwt

from lktec_bot.adapters.app_token import (
    JWT_BACKDATE_SECONDS,
    JWT_LIFETIME_SECONDS,
    build_app_jwt,
)
from lktec_bot.ports.key_source import AppCredentials


def test_jwt_is_rs256_with_the_documented_claims(
    credentials: AppCredentials, rsa_key_pair: tuple[str, str]
) -> None:
    now = 1_700_000_000.7

    token = build_app_jwt(credentials, now=lambda: now)

    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"
    claims = jwt.decode(
        token,
        rsa_key_pair[1],
        algorithms=["RS256"],
        issuer="12345",
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims == {
        "iat": 1_700_000_000 - JWT_BACKDATE_SECONDS,
        "exp": 1_700_000_000 + JWT_LIFETIME_SECONDS,
        "iss": "12345",
    }
    # GitHub rejects anything beyond ten minutes; stay clearly inside.
    assert claims["exp"] - claims["iat"] <= 10 * 60


def test_jwt_does_not_verify_with_another_key(credentials: AppCredentials) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    other_pem = other.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )

    token = build_app_jwt(credentials)

    try:
        jwt.decode(token, other_pem, algorithms=["RS256"])
    except jwt.InvalidSignatureError:
        return
    raise AssertionError("a JWT signed with one key must not verify with another")


def test_credentials_repr_never_contains_the_key(credentials: AppCredentials) -> None:
    assert "BEGIN" not in repr(credentials)
    assert "12345" in repr(credentials)
