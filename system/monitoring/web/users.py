"""Monitoring users from a separate, protected YAML file.

Format (written by the operator CLI system/tools/monitor_user.py, never by the web
interface):

    version: 1
    users:
      - username: alice
        password_hash: "$argon2id$v=19$m=65536,t=3,p=1$..."
        totp_secret: "BASE32SECRET..."
        enabled: true

Rules (fail closed): the file must exist, be a regular file, be readable,
on POSIX be owned by the process user and have no group/other permissions
(at most 0600), and every user must have an Argon2id hash and a TOTP secret
(MFA is mandatory). Any violation locks the web interface - no login, and
existing sessions are rejected - until the file is fixed. The file is
re-read when it changes (mtime/size/inode), so password or MFA resets with
the CLI take effect without a restart; a changed user invalidates that
user's sessions (fingerprint).

Windows (development only): POSIX permissions cannot be checked; the check
is skipped and `permissions_checked` is False so the interface can state it.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

from system.monitoring.web.passwords import is_argon2id_hash
from system.monitoring.web.totp import decode_secret

USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{2,31}")
MAX_USERS = 50
MAX_FILE_BYTES = 256 * 1024


class UsersUnavailable(Exception):
    """The users file cannot be used; `reason` is a fixed code for the audit log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class User:
    username: str
    password_hash: str
    totp_key: bytes
    enabled: bool

    @property
    def fingerprint(self) -> str:
        """Changes whenever password, MFA secret or status change."""
        material = f"{self.username}\0{self.password_hash}\0{self.totp_key.hex()}\0{self.enabled}"
        return hashlib.sha256(material.encode()).hexdigest()[:32]


def check_permissions(path: Path, st: os.stat_result) -> None:
    if not stat.S_ISREG(st.st_mode):
        raise UsersUnavailable("users_file_not_regular")
    if os.name != "posix":
        return
    # At most 0600: no execute bit, nothing for group or others.
    if stat.S_IMODE(st.st_mode) & 0o177:
        raise UsersUnavailable("users_file_permissions")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise UsersUnavailable("users_file_owner")


def parse_users(text: str) -> dict[str, User]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise UsersUnavailable("users_file_invalid") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("users"), list):
        raise UsersUnavailable("users_file_invalid")
    entries = data["users"]
    if not entries or len(entries) > MAX_USERS:
        raise UsersUnavailable("users_file_no_users" if not entries else "users_file_invalid")
    users: dict[str, User] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {
            "username",
            "password_hash",
            "totp_secret",
            "enabled",
        }:
            raise UsersUnavailable("users_file_invalid")
        username = entry.get("username")
        if not isinstance(username, str) or not USERNAME.fullmatch(username) or username in users:
            raise UsersUnavailable("users_file_invalid")
        if not is_argon2id_hash(entry.get("password_hash")):
            raise UsersUnavailable("users_file_invalid")
        secret = entry.get("totp_secret")
        if not isinstance(secret, str) or not secret:
            raise UsersUnavailable("mfa_not_configured")
        try:
            key = decode_secret(secret)
        except ValueError as exc:
            raise UsersUnavailable("mfa_not_configured") from exc
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise UsersUnavailable("users_file_invalid")
        users[username] = User(username, entry["password_hash"], key, enabled)
    return users


class UsersFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._signature: tuple | None = None
        self._users: dict[str, User] | None = None
        self._error: str | None = "users_file_not_loaded"
        self.permissions_checked = os.name == "posix"

    def _load_locked(self) -> None:
        try:
            st = os.stat(self.path)
        except OSError as exc:
            self._signature, self._users, self._error = None, None, "users_file_missing"
            raise UsersUnavailable("users_file_missing") from exc
        signature = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_mode, st.st_uid)
        if signature == self._signature:
            if self._error:
                raise UsersUnavailable(self._error)
            return
        self._signature = signature
        try:
            check_permissions(self.path, st)
            if st.st_size > MAX_FILE_BYTES:
                raise UsersUnavailable("users_file_invalid")
            text = self.path.read_text(encoding="utf-8")
            users = parse_users(text)
        except UsersUnavailable as exc:
            self._users, self._error = None, exc.reason
            raise
        except (OSError, UnicodeDecodeError) as exc:
            self._users, self._error = None, "users_file_unreadable"
            raise UsersUnavailable("users_file_unreadable") from exc
        self._users, self._error = users, None

    def users(self) -> dict[str, User]:
        """All users; raises UsersUnavailable (fail closed) on any problem."""
        with self._lock:
            self._load_locked()
            assert self._users is not None
            return self._users

    def get(self, username: str) -> User | None:
        return self.users().get(username)
