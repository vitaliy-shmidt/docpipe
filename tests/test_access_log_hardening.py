"""Security hardening H2: the access log never contains raw paths.

The log line keeps its fields (request_id, client_id, method, path, status,
duration_ms) and its request-id correlation; `path=` now carries the route
template, `<unmatched>` for unknown paths.
"""

from __future__ import annotations

import logging
import re

from tests.conftest import LAB_KEY, VALID_KEY, auth_headers, pdf_bytes

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
LINE = re.compile(
    r"^request_id=(?P<rid>[0-9a-f-]{36}) client_id=(?P<client>\S+) method=(?P<method>[A-Z]+) "
    r"path=(?P<path>\S+) status=(?P<status>\d{3}) duration_ms=[0-9.]+$"
)


def access_lines(caplog) -> list[re.Match]:
    lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == "docpipe" and r.getMessage().startswith("request_id=")
    ]
    matches = [LINE.match(line) for line in lines]
    assert all(matches), lines
    return matches


def test_access_log_uses_route_templates_and_keeps_correlation(client, caplog):
    caplog.set_level(logging.INFO)
    response = client.post(
        "/api/v1/documents/extract-text",
        headers=auth_headers(VALID_KEY),
        files={"file": ("secret-name.pdf", pdf_bytes(), "application/pdf")},
    )
    client.get("/api/v1/lab/prompts/some-mode-value", headers=auth_headers(LAB_KEY))
    [first, second] = access_lines(caplog)
    assert first["rid"] == response.headers["x-request-id"]
    assert (first["client"], first["method"], first["path"], first["status"]) == (
        "enabled-client",
        "POST",
        "/api/v1/documents/extract-text",
        "200",
    )
    assert second["path"] == "/api/v1/lab/prompts/{mode}"
    # Only DocPipe's own loggers (the test client logs its request URLs itself).
    docpipe_text = " | ".join(r.getMessage() for r in caplog.records if r.name.startswith("docpipe"))
    assert "some-mode-value" not in docpipe_text and "secret-name" not in docpipe_text


def test_unknown_paths_and_injection_attempts_are_not_logged(client, caplog):
    caplog.set_level(logging.INFO)
    attempts = [
        "/x%0d%0arequest_id=forged client_id=admin",
        "/%1b[31mred%1b[0m",
        "/api/v1/health/%00",
        "/" + "A" * 5000,
        "/wp-login.php?user=admin&password=hunter2",
        "/api/v1/health?token=SECRET-TOKEN",
    ]
    for path in attempts:
        client.get(path, headers={"User-Agent": "evil\tagent"})
    lines = access_lines(caplog)
    assert len(lines) == len(attempts)
    assert [m["path"] for m in lines] == ["<unmatched>"] * 4 + ["<unmatched>", "/api/v1/health"]
    for record in caplog.records:
        if record.getMessage().startswith("request_id="):
            assert not _CONTROL.search(record.getMessage())
    for leaked in ("forged", "hunter2", "SECRET-TOKEN", "AAAAAAAAAA", "red", "evil"):
        assert leaked not in "\n".join(m.string for m in lines)


def test_unusual_methods_are_normalized(client, caplog):
    caplog.set_level(logging.INFO)
    client.request("PROPFIND", "/api/v1/health")
    [line] = access_lines(caplog)
    assert line["method"] == "OTHER"


def test_query_strings_and_credentials_never_appear(client, caplog):
    caplog.set_level(logging.INFO)
    client.get("/api/v1/capabilities?api_key=" + VALID_KEY, headers=auth_headers(VALID_KEY))
    text = "\n".join(m.string for m in access_lines(caplog))
    assert VALID_KEY not in text and "api_key" not in text
