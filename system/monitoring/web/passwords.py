"""Argon2id password hashing and bounded verification.

Verification is deliberately expensive (64 MiB, 3 passes). To keep login
attempts from competing with document processing for CPU and memory, a
process-wide limiter allows at most `max_concurrency` verifications at the
same time and at most `per_minute` per rolling minute. A request that does
not get a slot is answered with the same uniform login failure - Argon2 is
not run.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# OWASP-conformant Argon2id parameters (stronger than the minimum m=19 MiB, t=2).
TIME_COST = 3
MEMORY_COST_KIB = 64 * 1024
PARALLELISM = 1
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256

HASHER = PasswordHasher(time_cost=TIME_COST, memory_cost=MEMORY_COST_KIB, parallelism=PARALLELISM)
# Verified against for unknown users, so they take the same time as known ones.
_DUMMY_HASH = HASHER.hash("docpipe-monitoring-dummy-password-never-valid")


def hash_password(password: str) -> str:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(f"password must have {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters")
    return HASHER.hash(password)


def is_argon2id_hash(value: object) -> bool:
    return isinstance(value, str) and value.startswith("$argon2id$") and len(value) < 512


class VerificationLimiter:
    def __init__(self, max_concurrency: int, per_minute: int, clock=time.monotonic) -> None:
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._per_minute = per_minute
        self._recent: deque[float] = deque()
        self._lock = threading.Lock()
        self._clock = clock

    def _take_rate_token(self) -> bool:
        with self._lock:
            now = self._clock()
            while self._recent and now - self._recent[0] >= 60:
                self._recent.popleft()
            if len(self._recent) >= self._per_minute:
                return False
            self._recent.append(now)
            return True

    def verify(self, password_hash: str | None, password: str) -> bool | None:
        """True/False = result; None = no capacity (Argon2 not run)."""
        if not self._take_rate_token():
            return None
        if not self._slots.acquire(timeout=2.0):
            return None
        try:
            if not isinstance(password, str) or len(password) > MAX_PASSWORD_LENGTH:
                password = ""
            try:
                HASHER.verify(password_hash if is_argon2id_hash(password_hash) else _DUMMY_HASH, password)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                return False
            return password_hash is not None and is_argon2id_hash(password_hash)
        finally:
            self._slots.release()
