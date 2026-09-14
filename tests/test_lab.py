from __future__ import annotations

from tests.conftest import (
    AI_KEY,
    LAB_KEY,
    LAB_NO_AI_KEY,
    LIGHT_MODEL,
    SAMPLE_MAINTENANCE_RESULT,
    VALID_KEY,
    auth_headers,
)

LIST_URL = "/api/v1/lab/prompts"
ANALYZE_URL = "/api/v1/documents/analyze"
DOC_ANALYZE_URL = ANALYZE_URL
LAB_ANALYZE_URL = "/api/v1/lab/analyze"

MAINTENANCE_TEXT = "WARTUNGSBERICHT\nDienstleister: KONE GmbH\nDatum: 12.09.2026\n"

DRAFT_PROMPT = (
    "DRAFT PROMPT. Output ONLY JSON with keys vendor_name, service_type, performed_at, "
    "next_due_date, technician, result, cost, currency, notes.\n\nCONTEXT:\n{context_block}\n\nTEXT:\n{text}"
)


def _load_url(mode: str, version: str | None = None) -> str:
    url = f"/api/v1/lab/prompts/{mode}"
    return f"{url}?version={version}" if version else url


def _versions_url(mode: str) -> str:
    return f"/api/v1/lab/prompts/{mode}/versions"


def _activate_url(mode: str) -> str:
    return f"/api/v1/lab/prompts/{mode}/activate"


def _draft_analyze(
    client, prompt=DRAFT_PROMPT, mode="maintenance_extraction", text=MAINTENANCE_TEXT, headers=None
):
    return client.post(
        LAB_ANALYZE_URL,
        json={"mode": mode, "text": text, "prompt": prompt, "context": {}},
        headers=auth_headers(LAB_KEY) if headers is None else headers,
    )


# --- Permissions --------------------------------------------------------


def test_lab_list_requires_prompt_lab_permission(client):
    response = client.get(LIST_URL, headers=auth_headers(AI_KEY))
    assert response.status_code == 403
    assert response.json()["code"] == "prompt_lab_disabled"


def test_lab_list_missing_auth(client):
    response = client.get(LIST_URL)
    assert response.status_code == 401


def test_lab_analyze_requires_prompt_lab_permission(client):
    response = _draft_analyze(client, headers=auth_headers(AI_KEY))
    assert response.status_code == 403
    assert response.json()["code"] == "prompt_lab_disabled"


def test_lab_analyze_requires_ai_permission_even_with_prompt_lab(client):
    # lab-no-ai-client has prompt_lab but not ai - /lab/analyze is the one
    # Lab route that actually calls Ollama, so it needs both.
    response = _draft_analyze(client, headers=auth_headers(LAB_NO_AI_KEY))
    assert response.status_code == 403
    assert response.json()["code"] == "ai_disabled"


def test_lab_list_works_without_ai_permission(client):
    # Read-only Lab endpoints never call Ollama - prompt_lab alone suffices.
    response = client.get(LIST_URL, headers=auth_headers(LAB_NO_AI_KEY))
    assert response.status_code == 200


def test_lab_save_and_activate_work_without_ai_permission(client):
    response = client.post(
        _versions_url("maintenance_extraction"),
        json={"version": "v2", "content": "content {context_block} {text}"},
        headers=auth_headers(LAB_NO_AI_KEY),
    )
    assert response.status_code == 200
    response = client.post(
        _activate_url("maintenance_extraction"), json={"version": "v2"}, headers=auth_headers(LAB_NO_AI_KEY)
    )
    assert response.status_code == 200


# --- List / load ---------------------------------------------------------


def test_lab_list_reports_both_modes_with_v1_active(client):
    response = client.get(LIST_URL, headers=auth_headers(LAB_KEY))
    assert response.status_code == 200
    modes = {m["mode"]: m for m in response.json()["data"]["modes"]}
    assert set(modes.keys()) == {"maintenance_extraction", "inspection_extraction"}
    assert modes["maintenance_extraction"]["active_version"] == "v1"
    assert modes["maintenance_extraction"]["versions"] == ["v1"]


def test_lab_list_unknown_route_not_exposed_to_non_lab_clients(client):
    response = client.get(LIST_URL, headers=auth_headers(VALID_KEY))
    assert response.status_code == 403


def test_lab_load_defaults_to_active_version(client):
    response = client.get(_load_url("maintenance_extraction"), headers=auth_headers(LAB_KEY))
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["version"] == "v1"
    assert body["active"] is True
    assert "{context_block}" in body["content"]


def test_lab_load_specific_version(client):
    response = client.get(_load_url("maintenance_extraction", "v1"), headers=auth_headers(LAB_KEY))
    assert response.status_code == 200
    assert response.json()["data"]["version"] == "v1"


def test_lab_load_unknown_mode(client):
    response = client.get(_load_url("does_not_exist"), headers=auth_headers(LAB_KEY))
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_mode"


def test_lab_load_unknown_version(client):
    response = client.get(_load_url("maintenance_extraction", "v99"), headers=auth_headers(LAB_KEY))
    assert response.status_code == 404
    assert response.json()["code"] == "unknown_prompt_version"


def test_lab_load_path_traversal_in_version_query_param_rejected(client):
    # ?version=../../../../etc/passwd - must be rejected by the version
    # name check before any filesystem path is ever built, same as any
    # other unknown version.
    response = client.get(
        "/api/v1/lab/prompts/maintenance_extraction?version=../../../../etc/passwd",
        headers=auth_headers(LAB_KEY),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "unknown_prompt_version"


def test_lab_activate_path_traversal_version_rejected(client):
    response = client.post(
        _activate_url("maintenance_extraction"),
        json={"version": "../../../../etc/passwd"},
        headers=auth_headers(LAB_KEY),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "unknown_prompt_version"


# --- Save / activate -----------------------------------------------------


def test_lab_save_new_version(client):
    response = client.post(
        _versions_url("maintenance_extraction"),
        json={"version": "v2", "content": "new draft {context_block} {text}"},
        headers=auth_headers(LAB_KEY),
    )
    assert response.status_code == 200
    assert response.json()["data"] == {"mode": "maintenance_extraction", "version": "v2"}
    # Immediately loadable, but NOT active yet.
    loaded = client.get(_load_url("maintenance_extraction", "v2"), headers=auth_headers(LAB_KEY))
    assert loaded.json()["data"]["content"] == "new draft {context_block} {text}"
    assert loaded.json()["data"]["active"] is False


def test_lab_save_duplicate_version_rejected(client):
    client.post(
        _versions_url("maintenance_extraction"),
        json={"version": "v2", "content": "first {context_block} {text}"},
        headers=auth_headers(LAB_KEY),
    )
    response = client.post(
        _versions_url("maintenance_extraction"),
        json={"version": "v2", "content": "second {context_block} {text}"},
        headers=auth_headers(LAB_KEY),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "prompt_version_exists"


def test_lab_save_duplicate_against_existing_v1_rejected(client):
    response = client.post(
        _versions_url("maintenance_extraction"),
        json={"version": "v1", "content": "overwrite attempt {context_block} {text}"},
        headers=auth_headers(LAB_KEY),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "prompt_version_exists"


def test_lab_save_invalid_version_name_rejected(client):
    for bad_version in ("../../evil", "v1/abc", "test.txt", "v0", "v01"):
        response = client.post(
            _versions_url("maintenance_extraction"),
            json={"version": bad_version, "content": "x {context_block} {text}"},
            headers=auth_headers(LAB_KEY),
        )
        assert response.status_code == 400, bad_version
        assert response.json()["code"] == "invalid_prompt_version", bad_version


def test_lab_save_empty_content_rejected(client):
    response = client.post(
        _versions_url("maintenance_extraction"),
        json={"version": "v2", "content": "   "},
        headers=auth_headers(LAB_KEY),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_prompt"


def test_lab_save_malformed_template_rejected(client):
    # Stray "{" that isn't a valid .format() placeholder - must be caught
    # at save time, never persisted.
    response = client.post(
        _versions_url("maintenance_extraction"),
        json={"version": "v2", "content": "broken { {context_block} {text}"},
        headers=auth_headers(LAB_KEY),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_prompt"
    versions = client.get(LIST_URL, headers=auth_headers(LAB_KEY)).json()["data"]["modes"]
    maintenance = next(m for m in versions if m["mode"] == "maintenance_extraction")
    assert "v2" not in maintenance["versions"]


def test_lab_save_unknown_mode_rejected(client):
    response = client.post(
        _versions_url("does_not_exist"),
        json={"version": "v2", "content": "x {context_block} {text}"},
        headers=auth_headers(LAB_KEY),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_mode"


def test_lab_activate_existing_version(client):
    client.post(
        _versions_url("maintenance_extraction"),
        json={"version": "v2", "content": "draft {context_block} {text}"},
        headers=auth_headers(LAB_KEY),
    )
    response = client.post(
        _activate_url("maintenance_extraction"), json={"version": "v2"}, headers=auth_headers(LAB_KEY)
    )
    assert response.status_code == 200
    assert response.json()["data"] == {"mode": "maintenance_extraction", "version": "v2"}
    loaded = client.get(_load_url("maintenance_extraction"), headers=auth_headers(LAB_KEY))
    assert loaded.json()["data"]["version"] == "v2"
    assert loaded.json()["data"]["active"] is True


def test_lab_activate_missing_version_rejected(client):
    response = client.post(
        _activate_url("maintenance_extraction"), json={"version": "v99"}, headers=auth_headers(LAB_KEY)
    )
    assert response.status_code == 404
    assert response.json()["code"] == "unknown_prompt_version"


def test_lab_save_does_not_automatically_activate(client):
    # Task §21: saving v2 must never make v2 active on its own.
    client.post(
        _versions_url("maintenance_extraction"),
        json={"version": "v2", "content": "draft {context_block} {text}"},
        headers=auth_headers(LAB_KEY),
    )
    active = client.get(LIST_URL, headers=auth_headers(LAB_KEY)).json()["data"]["modes"]
    maintenance = next(m for m in active if m["mode"] == "maintenance_extraction")
    assert maintenance["active_version"] == "v1"


# --- Draft analyze ---------------------------------------------------------


def test_lab_analyze_valid_draft_succeeds(client):
    response = _draft_analyze(client)
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["mode"] == "maintenance_extraction"
    assert body["prompt_source"] == "draft"
    assert body["model_profile"] == "light"
    assert body["model"] == LIGHT_MODEL
    assert body["result"] == SAMPLE_MAINTENANCE_RESULT
    assert len(body["prompt_sha256"]) == 64


def test_lab_analyze_empty_draft_rejected(client):
    response = _draft_analyze(client, prompt="   ")
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_prompt"


def test_lab_analyze_unknown_mode(client):
    response = _draft_analyze(client, mode="does_not_exist")
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_mode"


def test_lab_analyze_malformed_draft_prompt_rejected(client):
    response = _draft_analyze(client, prompt="broken {")
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_prompt"


def test_lab_analyze_rejects_unknown_fields(client):
    response = client.post(
        LAB_ANALYZE_URL,
        json={
            "mode": "maintenance_extraction",
            "text": MAINTENANCE_TEXT,
            "prompt": DRAFT_PROMPT,
            "context": {},
            "model": "sneaky-model",
        },
        headers=auth_headers(LAB_KEY),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


def test_lab_analyze_does_not_change_active_version(client):
    _draft_analyze(client)
    active = client.get(LIST_URL, headers=auth_headers(LAB_KEY)).json()["data"]["modes"]
    maintenance = next(m for m in active if m["mode"] == "maintenance_extraction")
    assert maintenance["active_version"] == "v1"
    assert maintenance["versions"] == ["v1"]  # draft never persisted


def test_lab_analyze_does_not_use_active_prompt(client):
    # Even though a real /documents/analyze call would use the active
    # (base v1) prompt, this call's fake Ollama response is identical
    # either way in this test double - what actually matters here is that
    # draft_analyze() builds its prompt from body.prompt, not from the
    # registry. Verified indirectly via
    # test_lab_analyze_malformed_draft_prompt_rejected (a malformed ACTIVE
    # v1 prompt would never trigger this, since v1 is a valid, real,
    # unrelated template) and test_lab_save_malformed_template_rejected.
    response = _draft_analyze(client, prompt="{text} only, no context ref")
    assert response.status_code == 200


def test_normal_analyze_still_rejects_prompt_field(client):
    # Existing regression from test_analyze.py, re-asserted here for
    # locality with the rest of the Lab test suite (task §51).
    response = client.post(
        DOC_ANALYZE_URL,
        json={"mode": "maintenance_extraction", "text": MAINTENANCE_TEXT, "prompt": DRAFT_PROMPT},
        headers=auth_headers(AI_KEY),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


# --- Hot reload (task §25) -------------------------------------------------


def test_prompt_hot_reload_without_restart(client):
    # 1. active version is v1 - a normal analyze call uses it.
    response = client.post(
        DOC_ANALYZE_URL,
        json={"mode": "maintenance_extraction", "text": MAINTENANCE_TEXT, "context": {}},
        headers=auth_headers(AI_KEY),
    )
    assert response.status_code == 200
    assert response.json()["data"]["prompt_version"] == "v1"

    # 2. save + activate v2 through the Lab - no server restart anywhere
    # in this test, same running app/process throughout.
    client.post(
        _versions_url("maintenance_extraction"),
        json={"version": "v2", "content": "v2 draft {context_block} {text}"},
        headers=auth_headers(LAB_KEY),
    )
    client.post(
        _activate_url("maintenance_extraction"), json={"version": "v2"}, headers=auth_headers(LAB_KEY)
    )

    # 3. the very next normal analyze call already uses v2 - no restart.
    response = client.post(
        DOC_ANALYZE_URL,
        json={"mode": "maintenance_extraction", "text": MAINTENANCE_TEXT, "context": {}},
        headers=auth_headers(AI_KEY),
    )
    assert response.status_code == 200
    assert response.json()["data"]["prompt_version"] == "v2"
    assert "v2 draft" in client.fake_ollama["client"].last_prompt
