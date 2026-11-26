"""TOTP implementation against RFC 6238 test vectors (Phase M3)."""

from __future__ import annotations

import pytest

from system.monitoring.web import totp

RFC_KEY = b"12345678901234567890"  # RFC 6238 Appendix B, SHA-1


@pytest.mark.parametrize(
    "unix, eight_digits",
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_rfc6238_vectors(unix, eight_digits):
    # Our codes are the 6-digit variant: the same truncated value mod 10^6.
    assert totp.code_at(RFC_KEY, totp.counter_at(unix)) == eight_digits[-6:]


def test_verify_accepts_only_the_bounded_window():
    now = 1_800_000_015.0
    counter = totp.counter_at(now)
    for offset, expected in ((-2, None), (-1, counter - 1), (0, counter), (1, counter + 1), (2, None)):
        code = totp.code_at(RFC_KEY, counter + offset)
        assert totp.verify(RFC_KEY, code, now, window_steps=1) == expected
    assert totp.verify(RFC_KEY, totp.code_at(RFC_KEY, counter - 1), now, window_steps=0) is None


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "12 34 5", None, 123456, "１２３４５６"])
def test_malformed_codes_are_rejected(code):
    assert totp.verify(RFC_KEY, code, 1_800_000_000, window_steps=1) is None


def test_secrets_are_160_bit_base32_and_round_trip():
    secret = totp.new_secret()
    assert len(totp.decode_secret(secret)) == 20
    assert totp.decode_secret(secret.lower()) == totp.decode_secret(secret)
    with pytest.raises(ValueError):
        totp.decode_secret("short")
    uri = totp.provisioning_uri("alice", secret)
    assert uri.startswith("otpauth://totp/DocPipe%20Monitoring%3Aalice?secret=")
    assert "algorithm=SHA1&digits=6&period=30" in uri
