# SPDX-FileCopyrightText: 2026 Lukas Koerber
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from lktec_bot.ports.key_source import AppCredentials


@pytest.fixture(autouse=True)
def verb_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The waits of ``enable-automerge``'s retry, recorded instead of slept, for every test."""
    sleeps: list[float] = []
    monkeypatch.setattr("lktec_bot.application.verbs.enable_automerge.sleep", sleeps.append)
    return sleeps


@pytest.fixture(autouse=True)
def policy_read_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The gate's policy-read waits, recorded instead of slept, for every test."""
    sleeps: list[float] = []
    monkeypatch.setattr("lktec_bot.application.gate.time.sleep", sleeps.append)
    return sleeps


@pytest.fixture(scope="session")
def rsa_key_pair() -> tuple[str, str]:
    """(private PEM, public PEM) generated once per test session."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture
def credentials(rsa_key_pair: tuple[str, str]) -> AppCredentials:
    return AppCredentials(app_id="12345", private_key_pem=rsa_key_pair[0])


class StaticKeySource:
    def __init__(self, credentials: AppCredentials) -> None:
        self._credentials = credentials

    def load(self) -> AppCredentials:
        return self._credentials


@pytest.fixture
def key_source(credentials: AppCredentials) -> StaticKeySource:
    return StaticKeySource(credentials)
