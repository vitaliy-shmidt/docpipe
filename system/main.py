"""DocPipe application entrypoint.

DocPipe is a generic, authenticated document processing proxy:

    Client -> DocPipe (API key check) -> Stirling -> normalized response -> Client

See README.md for what V1 does and deliberately does not do.
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
from system.config import load_settings
from system.errors import STATUS_BY_CODE, DocPipeError
from system.routes import capabilities, documents, health
from system.services.stirling import StirlingClient

logger = logging.getLogger("docpipe")
logging.basicConfig(level=logging.INFO, format="%(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings
    app.state.stirling_client = StirlingClient(settings.stirling)
    yield
    app.state.stirling_client.close()


app = FastAPI(title="DocPipe", version=VERSION, lifespan=lifespan)

app.include_router(health.router)
app.include_router(capabilities.router)
app.include_router(documents.router)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    request.state.client_id = None

    started_at = time.monotonic()
    response = await call_next(request)
    duration_ms = round((time.monotonic() - started_at) * 1000, 1)

    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_id=%s client_id=%s method=%s path=%s status=%s duration_ms=%s",
        request_id,
        request.state.client_id,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.exception_handler(DocPipeError)
async def docpipe_error_handler(request: Request, exc: DocPipeError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "code": exc.code, "message": exc.message},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=STATUS_BY_CODE["invalid_file"],
        content={"ok": False, "code": "invalid_file", "message": "The request could not be processed."},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 400:
        code, message = "invalid_file", "The request could not be processed."
    elif exc.status_code == 404:
        code, message = "internal_error", "Not found."
    else:
        code, message = "internal_error", "Request failed."
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "code": code, "message": message})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_error request_id=%s", getattr(request.state, "request_id", None))
    return JSONResponse(
        status_code=STATUS_BY_CODE["internal_error"],
        content={"ok": False, "code": "internal_error", "message": "An unexpected error occurred."},
    )
