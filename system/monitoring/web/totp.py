"""TOTP (RFC 6238) with HMAC-SHA1, 6 digits, 30 s steps.

These are the parameters every common authenticator app supports. Codes are
compared in constant time; the caller gets back the matched time-step
counter, which the auth store accepts at most once per user (atomic
replay protection, system/monitoring/web/authstore.py). Codes themselves are
never stored or logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import struct
from urllib.parse import quote

DIGITS = 6
PERIOD_SECONDS = 30
SECRET_BYTES = 20  # 160 bit, RFC 4226 recommendation
ISSUER = "DocPipe Monitoring"
_CODE = re.compile(r"[0-9]{6}")
_BASE32 = re.compile(r"[A-Z2-7]+=*")


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def decode_secret(secret: str) -> bytes:
    value = secret.strip().replace(" ", "").upper()
    if not _BASE32.fullmatch(value):
        raise ValueError("TOTP secret is not base32")
    value += "=" * (-len(value) % 8)
    raw = base64.b32decode(value)
    if len(raw) < 16:
        raise ValueError("TOTP secret is shorter than 128 bit")
    return raw


def code_at(key: bytes, counter: int) -> str:
    """RFC 4226 HOTP value for one counter."""
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def counter_at(unix_seconds: float) -> int:
    return int(unix_seconds // PERIOD_SECONDS)


def verify(key: bytes, code: str, unix_seconds: float, window_steps: int) -> int | None:
    """Matched counter within ±window_steps, or None. Every candidate is
    compared (no early exit)."""
    if not isinstance(code, str):
        return None
    code = code.strip().replace(" ", "")
    if not _CODE.fullmatch(code):
        return None
    now = counter_at(unix_seconds)
    matched = None
    for counter in range(now - window_steps, now + window_steps + 1):
        if hmac.compare_digest(code_at(key, counter), code) and matched is None:
            matched = counter
    return matched


def provisioning_uri(username: str, secret: str) -> str:
    label = quote(f"{ISSUER}:{username}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}"
        f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD_SECONDS}"
    )
