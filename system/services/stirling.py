"""Thin client around the Stirling-PDF HTTP API.

This is the ONLY place in DocPipe that knows anything about Stirling
(its base URL, auth header, endpoint path, and response shape). Route
handlers never talk to Stirling directly and never see raw Stirling
errors or payloads.

Verified against the current Stirling-PDF source (Stirling-Tools/Stirling-PDF,
`ConvertPDFToOffice` controller under `/api/v1/convert`):

  POST {base_url}/api/v1/convert/pdf/text
  Header: X-API-KEY: <stirling api key>
  Multipart field: fileInput=<the pdf>
  Form field: outputFormat=txt
  Response: 200 with Content-Type text/plain, body = extracted text.
"""

from __future__ import annotations

import httpx

from system.config import StirlingSettings
from system.errors import DocPipeError

EXTRACT_TEXT_PATH = "/api/v1/convert/pdf/text"


class StirlingClient:
    def __init__(self, settings: StirlingSettings) -> None:
        self._settings = settings
        # Redirects are not followed: the upstream URL is server-configured,
        # never client-controlled, and a redirect could otherwise be used to
        # smuggle the request (and its X-API-KEY) to an unintended host.
        self._client = httpx.Client(follow_redirects=False, timeout=settings.timeout_seconds)

    def close(self) -> None:
        self._client.close()

    def extract_text(self, file_bytes: bytes, filename: str) -> str:
        url = self._settings.base_url.rstrip("/") + EXTRACT_TEXT_PATH
        headers = {"X-API-KEY": self._settings.api_key}
        files = {"fileInput": (filename, file_bytes, "application/pdf")}
        data = {"outputFormat": "txt"}

        try:
            response = self._client.post(url, headers=headers, files=files, data=data)
        except httpx.TimeoutException as exc:
            raise DocPipeError("timeout", "Document processing timed out.") from exc
        except httpx.HTTPError as exc:
            raise DocPipeError(
                "upstream_unavailable", "Document processing service is temporarily unavailable."
            ) from exc

        if response.status_code in (401, 403):
            raise DocPipeError(
                "upstream_auth_failed", "Document processing service rejected the request."
            )
        if response.status_code >= 500:
            raise DocPipeError(
                "upstream_unavailable", "Document processing service is temporarily unavailable."
            )
        if response.status_code != 200:
            raise DocPipeError("processing_failed", "Document processing failed.")

        return response.text
