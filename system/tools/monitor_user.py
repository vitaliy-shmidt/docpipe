"""Set up and recover monitoring users (local operator tool, Phase M3).

    python -m system.tools.monitor_user [--file PATH] [--auth-db PATH] COMMAND [USERNAME]

Commands:
    add USERNAME            new user: password prompt + new TOTP secret
    reset-password USERNAME new password (all sessions of the user end)
    reset-mfa USERNAME      new TOTP secret, e.g. lost phone (all sessions end)
    disable USERNAME        user can no longer sign in (sessions end)
    enable USERNAME
    remove USERNAME
    revoke-sessions USERNAME  end all sessions now (needs --auth-db)
    list                    usernames and status, never secrets

Passwords are read with getpass (no echo) and are never accepted as
arguments, so they never appear in shell history or process lists. TOTP
secrets are generated here, shown once on this terminal for enrolment, and
stored only after the operator confirmed a valid code from the app. The
file is written atomically with permissions 0600 (POSIX). There is no other
way to create users: the web interface has no registration or user
management.

Recovery: an operator who lost the password or the authenticator runs
reset-password / reset-mfa on the server; changing a user invalidates all of
that user's sessions automatically (fingerprint check).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import tempfile
import time
from pathlib import Path

import yaml

from system.monitoring.web import totp
from system.monitoring.web.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, hash_password
from system.monitoring.web.users import MAX_USERS, USERNAME, UsersUnavailable, parse_users


class ToolError(Exception):
    pass


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, list):
        raise ToolError(f"{path} is not a monitoring users file")
    return users


def save(path: Path, users: list[dict]) -> None:
    document = {"version": 1, "users": users}
    text = yaml.safe_dump(document, sort_keys=False, allow_unicode=False)
    if users:
        try:
            parse_users(text)  # never write a file the server would reject
        except UsersUnavailable as exc:
            raise ToolError(f"refusing to write an invalid users file ({exc.reason})") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".monitor-users-", dir=str(path.parent))
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(
                "# DocPipe monitoring users - written by system.tools.monitor_user. Keep this file 0600.\n"
            )
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _find(users: list[dict], username: str) -> dict:
    for entry in users:
        if entry.get("username") == username:
            return entry
    raise ToolError(f"user {username!r} does not exist")


def ask_password(prompt=getpass.getpass) -> str:
    first = prompt("New password: ")
    if not MIN_PASSWORD_LENGTH <= len(first) <= MAX_PASSWORD_LENGTH:
        raise ToolError(f"the password must have {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters")
    if prompt("Repeat password: ") != first:
        raise ToolError("the passwords do not match")
    return first


def enrol_totp(username: str, out, ask_code, now=time.time) -> str:
    secret = totp.new_secret()
    out("Add this account to your authenticator app (shown only now):")
    out(f"  secret:  {secret}")
    out(f"  otpauth: {totp.provisioning_uri(username, secret)}")
    code = ask_code("Enter the current 6-digit code from the app to confirm: ")
    if totp.verify(totp.decode_secret(secret), code, now(), window_steps=1) is None:
        raise ToolError("the code did not match - nothing was saved, run the command again")
    return secret


def run(argv: list[str], *, out=print, prompt=getpass.getpass, ask_code=None, now=time.time) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m system.tools.monitor_user", description=__doc__.split("\n")[0]
    )
    parser.add_argument(
        "--file", default="monitoring-users.yaml", help="users file (monitoring.web.users_file)"
    )
    parser.add_argument("--auth-db", help="auth database, for revoke-sessions (<data_dir>/monitor-auth.db)")
    parser.add_argument(
        "command",
        choices=[
            "add",
            "reset-password",
            "reset-mfa",
            "disable",
            "enable",
            "remove",
            "revoke-sessions",
            "list",
        ],
    )
    parser.add_argument("username", nargs="?")
    args = parser.parse_args(argv)
    ask_code = ask_code or prompt
    path = Path(args.file)
    try:
        users = load(path)
        if args.command == "list":
            for entry in users:
                state = "enabled" if entry.get("enabled", True) else "disabled"
                out(f"{entry.get('username')}  {state}  mfa={'yes' if entry.get('totp_secret') else 'no'}")
            return 0
        username = (args.username or "").strip().lower()
        if not USERNAME.fullmatch(username):
            raise ToolError(
                "username: 3-32 characters a-z, 0-9, '.', '_', '-' (starting with a letter or digit)"
            )
        if args.command == "add":
            if any(e.get("username") == username for e in users):
                raise ToolError(f"user {username!r} already exists")
            if len(users) >= MAX_USERS:
                raise ToolError("too many users")
            password_hash = hash_password(ask_password(prompt))
            secret = enrol_totp(username, out, ask_code, now)
            users.append(
                {"username": username, "password_hash": password_hash, "totp_secret": secret, "enabled": True}
            )
        elif args.command == "reset-password":
            _find(users, username)["password_hash"] = hash_password(ask_password(prompt))
        elif args.command == "reset-mfa":
            entry = _find(users, username)
            entry["totp_secret"] = enrol_totp(username, out, ask_code, now)
        elif args.command in ("disable", "enable"):
            _find(users, username)["enabled"] = args.command == "enable"
        elif args.command == "remove":
            users.remove(_find(users, username))
        elif args.command == "revoke-sessions":
            if not args.auth_db:
                raise ToolError("revoke-sessions needs --auth-db <data_dir>/monitor-auth.db")
            from system.monitoring.web.authstore import AuthStore

            store = AuthStore(Path(args.auth_db), 64 * 1024 * 1024)
            store.open()
            try:
                out(f"{store.revoke_user_sessions(username)} session(s) ended")
            finally:
                store.close()
            return 0
        save(path, users)
        out(f"{args.command}: {username} - saved to {path}")
        if os.name != "posix":
            out("note: file permissions cannot be restricted on this platform (development only)")
        return 0
    except (ToolError, ValueError, OSError, yaml.YAMLError) as exc:
        out(f"error: {exc}")
        return 1


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
