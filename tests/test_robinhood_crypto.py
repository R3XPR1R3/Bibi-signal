"""Unit tests for Robinhood Crypto client.

Pure auth/signing tests — they don't hit the network. We verify that the
Ed25519 signature can be verified by the corresponding public key, which
is the same check Robinhood does server-side.
"""
from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bibi_signal.config import AppSettings
from bibi_signal.robinhood_crypto import (
    RobinhoodCryptoClient,
    RobinhoodCryptoNotConfigured,
    _load_private_key,
)


def _gen_seed_b64() -> str:
    pk = Ed25519PrivateKey.generate()
    raw = pk.private_bytes_raw()
    return base64.b64encode(raw).decode()


def test_missing_creds_raises(monkeypatch):
    monkeypatch.delenv("ROBINHOOD_CRYPTO_API_KEY", raising=False)
    monkeypatch.delenv("ROBINHOOD_CRYPTO_PRIVATE_KEY_B64", raising=False)
    s = AppSettings(_env_file=None)
    with pytest.raises(RobinhoodCryptoNotConfigured):
        RobinhoodCryptoClient(s)


def test_load_private_key_rejects_wrong_length():
    bad = base64.b64encode(b"too_short").decode()
    with pytest.raises(Exception):
        _load_private_key(bad)


def test_load_private_key_accepts_32_byte_seed():
    seed_b64 = _gen_seed_b64()
    pk = _load_private_key(seed_b64)
    assert pk is not None


def test_signature_verifies_with_public_key():
    seed_b64 = _gen_seed_b64()
    settings = AppSettings(
        _env_file=None,
        robinhood_crypto_api_key="apikey-xyz",
        robinhood_crypto_private_key_b64=seed_b64,
    )
    client = RobinhoodCryptoClient(settings)
    # Reach into the signing helper directly.
    timestamp = "1700000000"
    method = "GET"
    path = "/api/v1/crypto/trading/accounts/"
    body = ""
    sig_b64 = client._sign(timestamp, method, path, body)
    sig = base64.b64decode(sig_b64)

    pub = client._private_key.public_key()
    expected_msg = f"{settings.robinhood_crypto_api_key}{timestamp}{path}{method.upper()}{body}".encode()
    # Raises if invalid:
    pub.verify(sig, expected_msg)
    client.close()


def test_headers_contain_required_fields():
    seed_b64 = _gen_seed_b64()
    settings = AppSettings(
        _env_file=None,
        robinhood_crypto_api_key="apikey-xyz",
        robinhood_crypto_private_key_b64=seed_b64,
    )
    client = RobinhoodCryptoClient(settings)
    h = client._headers("GET", "/api/v1/crypto/trading/accounts/", "")
    assert h["x-api-key"] == "apikey-xyz"
    assert "x-signature" in h
    assert h["x-timestamp"].isdigit()
    client.close()
