"""Security hardening H1: constant-time API key lookup.

Behaviour must be exactly as before (same keys valid, same clients found);
only the comparison method changes.
"""

from __future__ import annotations

import hmac

import pytest

from system import config as config_module
from system.config import (
    ClientConfig,
    ClientServices,
    OllamaSettings,
    ServerSettings,
    Settings,
    StirlingSettings,
)
from tests.conftest import AI_KEY, DISABLED_KEY, VALID_KEY, auth_headers


def settings_with(**keys: str) -> Settings:
    return Settings(
        server=ServerSettings(),
        stirling=StirlingSettings(base_url="", api_key="", timeout_seconds=5),
        ollama=OllamaSettings(base_url=""),
        clients={
            name: ClientConfig(
                client_id=name, enabled=True, api_key=key, services=ClientServices(documents=True)
            )
            for name, key in keys.items()
        },
    )


def test_existing_keys_resolve_to_the_same_clients():
    s = settings_with(a="key-a", b="key-b", c="k€y-ä 🔑 \"'\\")
    assert s.find_client_by_api_key("key-a").client_id == "a"
    assert s.find_client_by_api_key("key-b").client_id == "b"
    assert s.find_client_by_api_key("k€y-ä 🔑 \"'\\").client_id == "c"


@pytest.mark.parametrize(
    "presented",
    [
        "",
        "key",
        "key-a ",
        " key-a",
        "KEY-A",
        "key-a\x00",
        "key-a\n",
        "key-ab",
        "k" * 10_000,
        "\udcff",
        "key-ä",
        None,
        123,
    ],
)
def test_wrong_or_malformed_keys_never_match(presented):
    s = settings_with(a="key-a", b="key-b")
    assert s.find_client_by_api_key(presented) is None


def test_a_client_with_an_empty_configured_key_never_matches():
    s = settings_with(empty="", a="key-a")
    assert s.find_client_by_api_key("") is None
    assert s.find_client_by_api_key("key-a").client_id == "a"


def test_duplicate_keys_keep_returning_the_first_client():
    s = settings_with(first="same", second="same")
    assert s.find_client_by_api_key("same").client_id == "first"


def test_every_client_is_compared_even_after_a_match(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def counting(a, b):
        calls.append((len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(config_module.hmac, "compare_digest", counting)
    s = settings_with(a="key-a", b="much-longer-key-b", c="c")
    s.find_client_by_api_key("key-a")
    s.find_client_by_api_key("nothing")
    # 3 comparisons per lookup, always between two 32-byte digests.
    assert calls == [(32, 32)] * 6


def test_http_authentication_behaves_as_before(client):
    pdf = {"file": ("x.pdf", b"%PDF-1.4\n" + b"0" * 100, "application/pdf")}
    assert client.get("/api/v1/capabilities", headers=auth_headers(VALID_KEY)).status_code == 200
    assert client.get("/api/v1/capabilities", headers=auth_headers(AI_KEY)).json()["services"]["ai"] is True
    for headers in (
        auth_headers(VALID_KEY + "x"),
        auth_headers(VALID_KEY.upper()),
        auth_headers(DISABLED_KEY),
        {"Authorization": "Bearer"},
        {"Authorization": f"Basic {VALID_KEY}"},
        {},
        {"Authorization": b"Bearer " + bytes([0xFF, 0xFE, 0x01])},  # raw latin-1 bytes
    ):
        response = client.post("/api/v1/documents/extract-text", files=pdf, headers=headers)
        assert response.status_code == 401
        assert VALID_KEY not in response.text
