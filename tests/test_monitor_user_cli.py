"""Operator CLI for monitoring users (Phase M3)."""

from __future__ import annotations

import os
import stat
import time

import pytest

from system.monitoring.web import totp
from system.monitoring.web.users import UsersFile
from system.tools import monitor_user
from tests.monitoring_web_support import code_for, full_login, use_fast_argon2, web_client

PASSWORD = "operator passphrase 2026"


class Terminal:
    def __init__(self, passwords=(), codes=()) -> None:
        self.passwords = list(passwords)
        self.codes = list(codes)
        self.lines: list[str] = []
        self.secret: str | None = None

    def out(self, line: str) -> None:
        self.lines.append(line)
        if line.strip().startswith("secret:"):
            self.secret = line.split(":", 1)[1].strip()

    def prompt(self, _label: str) -> str:
        return self.passwords.pop(0)

    def code(self, _label: str) -> str:
        if self.codes and self.codes[0] == "VALID":
            self.codes.pop(0)
            return code_for(self.secret)
        return self.codes.pop(0)


def run(args, terminal: Terminal) -> int:
    return monitor_user.run(args, out=terminal.out, prompt=terminal.prompt, ask_code=terminal.code)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    use_fast_argon2(monkeypatch)


def test_add_creates_a_valid_protected_users_file(tmp_path):
    path = tmp_path / "users.yaml"
    term = Terminal([PASSWORD, PASSWORD], ["VALID"])
    assert run(["--file", str(path), "add", "alice"], term) == 0
    users = UsersFile(path).users()
    assert set(users) == {"alice"} and users["alice"].enabled
    text = path.read_text(encoding="utf-8")
    assert PASSWORD not in text and "$argon2id$" in text
    assert term.secret and len(totp.decode_secret(term.secret)) == 20
    assert any("otpauth://totp/" in line for line in term.lines)
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_wrong_confirmation_code_saves_nothing(tmp_path):
    path = tmp_path / "users.yaml"
    assert run(["--file", str(path), "add", "alice"], Terminal([PASSWORD, PASSWORD], ["000000"])) == 1
    assert not path.exists()


@pytest.mark.parametrize(
    "passwords", [["short", "short"], [PASSWORD, PASSWORD + "x"], ["x" * 300, "x" * 300]]
)
def test_password_rules(tmp_path, passwords):
    term = Terminal(passwords, ["VALID"])
    assert run(["--file", str(tmp_path / "u.yaml"), "add", "alice"], term) == 1
    assert not (tmp_path / "u.yaml").exists()


def test_passwords_cannot_be_passed_as_arguments(tmp_path):
    with pytest.raises(SystemExit):
        monitor_user.run(
            ["--file", str(tmp_path / "u.yaml"), "--password", PASSWORD, "add", "alice"], out=lambda *_: None
        )


@pytest.mark.parametrize("name", ["A", "ab", "Bad Name", "../etc", "x" * 40])
def test_usernames_are_validated(tmp_path, name):
    assert (
        run(["--file", str(tmp_path / "u.yaml"), "add", name], Terminal([PASSWORD, PASSWORD], ["VALID"])) == 1
    )


def test_duplicate_add_is_refused(tmp_path):
    path = tmp_path / "u.yaml"
    run(["--file", str(path), "add", "alice"], Terminal([PASSWORD, PASSWORD], ["VALID"]))
    assert run(["--file", str(path), "add", "alice"], Terminal([PASSWORD, PASSWORD], ["VALID"])) == 1


def test_list_shows_no_secrets(tmp_path):
    path = tmp_path / "u.yaml"
    term = Terminal([PASSWORD, PASSWORD], ["VALID"])
    run(["--file", str(path), "add", "alice"], term)
    listing = Terminal()
    assert run(["--file", str(path), "list"], listing) == 0
    assert listing.lines == ["alice  enabled  mfa=yes"]
    assert term.secret not in " ".join(listing.lines)


def test_recovery_resets_invalidate_existing_sessions(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    path = tmp_path / "users.yaml"
    term = Terminal([PASSWORD, PASSWORD], ["VALID"])
    run(["--file", str(path), "add", "alice"], term)
    with web_client(monkeypatch, tmp_path, users=None) as client:
        assert full_login(client, term.secret, password=PASSWORD).status_code == 303
        assert client.get("/monitor/api/v1/session").status_code == 200
        # lost phone: new MFA secret via the CLI on the server
        reset = Terminal(codes=["VALID"])
        assert run(["--file", str(path), "reset-mfa", "alice"], reset) == 0
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 1))
        assert client.get("/monitor/api/v1/session").status_code == 401  # old sessions end
        assert full_login(client, reset.secret, password=PASSWORD, offset_steps=1).status_code == 303
        # forgotten password
        assert run(["--file", str(path), "reset-password", "alice"], Terminal(["new " + PASSWORD] * 2)) == 0
        os.utime(path, ns=(time.time_ns() + 5, time.time_ns() + 6))
        assert client.get("/monitor/api/v1/session").status_code == 401
        assert run(["--file", str(path), "disable", "alice"], Terminal()) == 0
        assert not UsersFile(path).users()["alice"].enabled


def test_revoke_sessions_via_the_auth_database(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    path = tmp_path / "users.yaml"
    term = Terminal([PASSWORD, PASSWORD], ["VALID"])
    run(["--file", str(path), "add", "alice"], term)
    with web_client(monkeypatch, tmp_path, users=None) as client:
        full_login(client, term.secret, password=PASSWORD)
        out = Terminal()
        assert (
            run(
                ["--file", str(path), "--auth-db", str(client.web.store.path), "revoke-sessions", "alice"],
                out,
            )
            == 0
        )
        assert out.lines == ["1 session(s) ended"]
        assert client.get("/monitor/api/v1/session").status_code == 401


def test_remove_and_unknown_user(tmp_path):
    path = tmp_path / "u.yaml"
    run(["--file", str(path), "add", "alice"], Terminal([PASSWORD, PASSWORD], ["VALID"]))
    run(["--file", str(path), "add", "bob.ops"], Terminal([PASSWORD, PASSWORD], ["VALID"]))
    assert run(["--file", str(path), "remove", "alice"], Terminal()) == 0
    assert set(UsersFile(path).users()) == {"bob.ops"}
    assert run(["--file", str(path), "reset-password", "carol"], Terminal([PASSWORD, PASSWORD])) == 1


def test_enrolment_secret_works_with_standard_totp():
    secret = totp.new_secret()
    key = totp.decode_secret(secret)
    now = time.time()
    assert totp.verify(key, totp.code_at(key, totp.counter_at(now)), now, 1) is not None
