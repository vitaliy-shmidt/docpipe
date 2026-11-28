"""Synthetic Ollama + Stirling PDF stand-in for local load tests (Phase M7).

Never used in production. Serves both upstreams on one port:

  Ollama:   POST /api/generate (instance generated from the `format` schema),
            GET /api/version, /api/tags, /api/ps
  Stirling: POST /api/v1/convert/pdf/text, /api/v1/misc/ocr-pdf,
            GET /api/v1/info/status

Behaviour of the status endpoints (what DocPipe's probes call) is switched at
runtime with POST /__mode/<mode>:
  normal   - answer immediately
  trickle  - 200 OK, then one byte every 0.25 s (never completes)
  hang     - accept, never answer
  down     - close the connection without an answer
AI and PDF endpoints keep working in every mode, with fixed synthetic delays
(AI_DELAY_MS, PDF_DELAY_MS), so the probe scenarios isolate the collector.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AI_DELAY_MS = float(os.environ.get("AI_DELAY_MS", "150"))
PDF_DELAY_MS = float(os.environ.get("PDF_DELAY_MS", "40"))
PORT = int(os.environ.get("PORT", "9000"))
STATE = {"mode": "normal"}
PROBE_PATHS = {"/api/version", "/api/tags", "/api/ps", "/api/v1/info/status"}
TEXT = ("Synthetic maintenance report. Device inspected, filters replaced, next service due. " * 8).encode()


def instance(schema: dict):
    """Smallest value that validates against the (simple) schemas DocPipe sends."""
    if not isinstance(schema, dict):
        return None
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [s for s in schema[key] if s.get("type") != "null"] or schema[key]
            return instance(options[0])
    types = schema.get("type", "object")
    types = types if isinstance(types, list) else [types]
    if "pattern" in schema and "null" in types:
        return None
    kind = next((t for t in types if t != "null"), "null")
    if kind == "object":
        props = schema.get("properties", {})
        return {name: instance(props.get(name, {})) for name in schema.get("required", list(props))}
    if kind == "array":
        return [instance(schema.get("items", {})) for _ in range(schema.get("minItems", 0))]
    if kind == "string":
        return "x" * max(1, schema.get("minLength", 1))
    if kind == "integer":
        return int(schema.get("minimum", 1))
    if kind == "number":
        return float(schema.get("minimum", 1))
    if kind == "boolean":
        return False
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    def _json(self, body: dict, status: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _probe_misbehaviour(self) -> bool:
        mode = STATE["mode"]
        if mode == "normal":
            return False
        if mode == "down":
            self.close_connection = True
            self.connection.close()
            return True
        if mode == "hang":
            time.sleep(60)
            self.close_connection = True
            return True
        self.send_response(200)  # trickle
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "1000000")
        self.end_headers()
        try:
            for _ in range(240):
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.25)
        except OSError:
            pass
        self.close_connection = True
        return True

    def do_GET(self):  # noqa: N802
        if self.path in PROBE_PATHS and self._probe_misbehaviour():
            return
        if self.path == "/api/version":
            return self._json({"version": "0.0.0-synthetic"})
        if self.path in ("/api/tags", "/api/ps"):
            model = {
                "name": "synthetic-model",
                "model": "synthetic-model",
                "size": 1_000_000,
                "details": {"family": "synthetic", "parameter_size": "1B", "quantization_level": "Q4_0"},
            }
            if self.path == "/api/ps":
                model.update(size_vram=500_000, expires_at="2099-01-01T00:00:00Z", context_length=4096)
            return self._json({"models": [model]})
        if self.path == "/api/v1/info/status":
            return self._json({"status": "UP", "version": "0.0.0-synthetic"})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        if self.path.startswith("/__mode/"):
            STATE["mode"] = self.path.rsplit("/", 1)[1]
            return self._json({"mode": STATE["mode"]})
        body = self._body()
        if self.path == "/api/generate":
            payload = json.loads(body or b"{}")
            delay = AI_DELAY_MS * random.uniform(0.8, 1.2)
            time.sleep(delay / 1000)
            fmt = payload.get("format")
            response = json.dumps(instance(fmt)) if isinstance(fmt, dict) else "ok"
            ns = int(delay * 1_000_000)
            return self._json(
                {
                    "response": response,
                    "done": True,
                    "total_duration": ns,
                    "load_duration": 0,
                    "prompt_eval_count": 600,
                    "prompt_eval_duration": ns // 3,
                    "eval_count": 60,
                    "eval_duration": ns // 2,
                }
            )
        if self.path in ("/api/v1/convert/pdf/text", "/api/v1/misc/ocr-pdf"):
            time.sleep(PDF_DELAY_MS * random.uniform(0.8, 1.2) / 1000)
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/plain" if self.path.endswith("text") else "application/pdf"
            )
            self.send_header("Content-Length", str(len(TEXT)))
            self.end_headers()
            self.wfile.write(TEXT)
            return None
        return self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"fake upstream on :{PORT}", flush=True)
    while True:
        time.sleep(3600)
