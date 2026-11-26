"""DocPipe application entrypoint.

DocPipe is a generic, authenticated document processing proxy:

    Client -> DocPipe (API key check) -> Stirling -> normalized response -> Client
                                       -> Ollama (AI analysis, V2)   -> normalized response -> Client

See README.md for what V2 does and deliberately does not do.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from system import VERSION
from system.ai.prompt_registry import PromptRegistry
from system.config import load_settings
from system.errors import STATUS_BY_CODE, DocPipeError
from system.monitoring.context import note_error_code
from system.monitoring.middleware import MonitoringMiddleware
from system.monitoring.registry import log_safe_method, log_safe_route
from system.routes import analyze, assistant, capabilities, documents, forms, health, lab
from system.services.ollama import OllamaClient
from system.services.stirling import StirlingClient

logger = logging.getLogger("docpipe")
logging.basicConfig(level=logging.INFO, format="%(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings
    stirling_client = StirlingClient(settings.stirling)
    ollama_client = OllamaClient(settings.ollama.base_url, keep_alive=settings.ollama.keep_alive)
    monitor = None
    if settings.monitoring.enabled:
        # Imported lazily: with monitoring disabled none of the runtime
        # (store, collector, probes) is even loaded, and the routes keep
        # using exactly the unwrapped client objects.
        from system.monitoring.monitor import Monitor
        from system.monitoring.proxies import MonitoredOllamaClient, MonitoredStirlingClient

        monitor = Monitor(settings)
        stirling_client = MonitoredStirlingClient(stirling_client)
        ollama_client = MonitoredOllamaClient(ollama_client, monitor.known_models)
        monitor.start()
    app.state.monitoring = monitor
    app.state.stirling_client = stirling_client
    app.state.ollama_client = ollama_client
    app.state.prompt_registry = PromptRegistry(
        settings.prompts.base_dir, settings.prompts.runtime_dir, settings.prompts.max_content_length
    )
    try:
        yield
    finally:
        if monitor is not None:
            monitor.stop()
            app.state.monitoring = None
        app.state.stirling_client.close()
        app.state.ollama_client.close()


app = FastAPI(title="DocPipe", version=VERSION, lifespan=lifespan)

app.include_router(health.router)
app.include_router(capabilities.router)
app.include_router(documents.router)
app.include_router(forms.router)
app.include_router(analyze.router)
app.include_router(lab.router)
app.include_router(assistant.router)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    # MonitoringMiddleware (outermost, only when monitoring is enabled)
    # assigns the id first so the monitoring record correlates with this
    # log line and X-Request-ID; otherwise a fresh one, exactly as before.
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    request.state.request_id = request_id
    request.state.client_id = None

    started_at = time.monotonic()
    response = await call_next(request)
    duration_ms = round((time.monotonic() - started_at) * 1000, 1)

    response.headers["X-Request-ID"] = request_id
    # Security hardening H2: never the raw (URL-decoded, client-controlled)
    # path - it can carry control characters (log injection) or secrets.
    # `path=` is the template of the route that handled the request (e.g.
    # /api/v1/lab/prompts/{mode}), `<unmatched>` for unknown paths; the
    # query string was never logged and still is not.
    logger.info(
        "request_id=%s client_id=%s method=%s path=%s status=%s duration_ms=%s",
        request_id,
        request.state.client_id,
        log_safe_method(request.method),
        log_safe_route(request.scope),
        response.status_code,
        duration_ms,
    )
    return response


# Added after the logging middleware, so it is the outermost user middleware
# (it also measures the logging middleware and sees unhandled exceptions).
# A pure pass-through while app.state.monitoring is None.
app.add_middleware(MonitoringMiddleware)


@app.exception_handler(DocPipeError)
async def docpipe_error_handler(request: Request, exc: DocPipeError) -> JSONResponse:
    note_error_code(exc.code)
    content = {"ok": False, "code": exc.code, "message": exc.message}
    if getattr(exc, "details", None):
        content["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content=content)


# Upload endpoints whose malformed requests (e.g. missing `file`) are reported
# as invalid_file. forms/inspect (Phase 2.1a) joins extract-text; every other
# endpoint keeps invalid_request exactly as before.
_FILE_ENDPOINTS = frozenset({"/api/v1/documents/extract-text", forms.INSPECT_PATH, forms.FILL_PATH})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Same malformed-request situation (missing/wrong-typed field), but a
    # distinct code for a JSON-body endpoint (e.g. /documents/analyze) vs.
    # the file-upload endpoint - "invalid_file" is specifically about an
    # uploaded document and would be a misleading code for a bad `text`/
    # `mode`/`context` field.
    is_file_endpoint = request.url.path in _FILE_ENDPOINTS
    code = "invalid_file" if is_file_endpoint else "invalid_request"
    note_error_code(code)
    return JSONResponse(
        status_code=STATUS_BY_CODE[code],
        content={"ok": False, "code": code, "message": "The request could not be processed."},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 400:
        is_file_endpoint = request.url.path in _FILE_ENDPOINTS
        code = "invalid_file" if is_file_endpoint else "invalid_request"
        message = "The request could not be processed."
    elif exc.status_code == 404:
        code, message = "internal_error", "Not found."
    else:
        code, message = "internal_error", "Request failed."
    note_error_code(code)
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "code": code, "message": message})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_error request_id=%s", getattr(request.state, "request_id", None))
    return JSONResponse(
        status_code=STATUS_BY_CODE["internal_error"],
        content={"ok": False, "code": "internal_error", "message": "An unexpected error occurred."},
    )
