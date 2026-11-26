"""Operator web interface of the monitoring (Phase M3: secure access + shell).

A small ASGI application, dispatched by MonitoringMiddleware for /monitor
and /monitor/* only while `monitoring.web.enabled` is true. It is not part
of DocPipe's FastAPI router: with the interface disabled no monitoring route
exists, and its requests never reach the customer API stack.

Order of checks for every request (each one fails closed):

1. network access (netaccess.decide: trusted proxy, allowlist / public mode,
   HTTPS) - also for static files and the login page;
2. static files (public library files and own CSS/JS, nothing else);
3. startup/security configuration state (locked -> 503, no data);
4. authentication: server-side session (or the pending-MFA state for the
   second login step); any auth-store or users-file error -> 503.

All responses carry strict security headers (CSP without inline code,
nosniff, same-origin referrer, frame-ancestors 'none', no-store for pages/APIs).
CPU and I/O work (Argon2, SQLite) runs in the thread pool, never on the
event loop that serves the document API.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from system.monitoring.registry import AI_OPERATIONS, STIRLING_OPERATION_BY_STEP
from system.monitoring.web import netaccess, totp
from system.monitoring.web.authstore import AUTH_DB_FILENAME, AuthStore
from system.monitoring.web.dashboard import BadRequest, Dashboard, TelemetryUnavailable
from system.monitoring.web.i18n import JS_KEYS, SECTIONS, TEXTS, choose_language
from system.monitoring.web.passwords import MAX_PASSWORD_LENGTH, VerificationLimiter
from system.monitoring.web.upstreams import UpstreamViews
from system.monitoring.web.users import USERNAME, UsersFile, UsersUnavailable

logger = logging.getLogger("docpipe.monitoring.web")

BASE = Path(__file__).parent
SESSION_COOKIE = "__Host-dp_mon"
MFA_COOKIE = "__Host-dp_mon_mfa"
CSRF_COOKIE = "__Host-dp_mon_csrf"
LANG_COOKIE = "dp_mon_lang"
_TOKEN = re.compile(r"[A-Za-z0-9_\-]{20,128}")
_HOST = re.compile(r"[A-Za-z0-9.\-]{1,253}(:[0-9]{1,5})?|\[[0-9A-Fa-f:.]{2,45}\](:[0-9]{1,5})?")
CLEANUP_INTERVAL_MS = 5 * 60_000

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
        "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",  # no-referrer makes browsers send Origin: null on form POSTs
    "X-Frame-Options": "DENY",
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), "
        "payment=(), usb=(), interest-cohort=()"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
ROUTES = {
    "/monitor": {"GET"},
    "/monitor/login": {"GET", "POST"},
    "/monitor/login/mfa": {"GET", "POST"},
    "/monitor/logout": {"POST"},
    "/monitor/api/v1/session": {"GET"},
    "/monitor/api/v1/status": {"GET"},
    "/monitor/session/activity": {"POST"},
}
# Dashboard pages (Phase M4): path -> page name. A page view is user activity.
PAGES = {
    "/monitor": "overview",
    "/monitor/activity": "activity",
    "/monitor/clients": "clients",
    "/monitor/performance": "performance",
    "/monitor/server": "server",
    "/monitor/security": "security",
    "/monitor/ollama": "ollama",
    "/monitor/stirling": "stirling",
}
# Read-only data API (Phase M4): never extends the idle time.
DATA_API = {
    "/monitor/api/v1/overview": "overview",
    "/monitor/api/v1/activity": "activity",
    "/monitor/api/v1/clients": "clients",
    "/monitor/api/v1/performance": "performance",
    "/monitor/api/v1/server": "server",
    "/monitor/api/v1/security": "security",
    "/monitor/api/v1/meta": "meta",
    "/monitor/api/v1/ollama": "ollama",
    "/monitor/api/v1/stirling": "stirling",
}
ROUTES.update({path: {"GET"} for path in (*PAGES, *DATA_API)})
MAX_QUERY_BYTES = 1024
MAX_QUERY_FIELDS = 12
ACTIVITY_MIN_INTERVAL_MS = 30_000


def wall_ms() -> int:
    return time.time_ns() // 1_000_000


def _load_static() -> dict[str, tuple[bytes, str]]:
    root = BASE / "static"
    files = {}
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in STATIC_TYPES:
            files["/monitor/static/" + path.relative_to(root).as_posix()] = (
                path.read_bytes(),
                STATIC_TYPES[path.suffix],
            )
    return files


@dataclass
class Ctx:
    scope: dict
    method: str
    path: str
    headers: dict[str, str]
    cookies: dict[str, str]
    form: dict[str, str]
    decision: netaccess.AccessDecision
    lang: str
    query: dict[str, str]

    @property
    def ip(self) -> str | None:
        return str(self.decision.client_ip) if self.decision.client_ip else None

    @property
    def t(self) -> dict[str, str]:
        return TEXTS[self.lang]


class Locked(Exception):
    """Security data unavailable: deny with 503 (fail closed)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MonitorWeb:
    def __init__(
        self, settings, monitor, *, clock_ms=wall_ms, unix_time=time.time, argv=None, env=None
    ) -> None:
        self.web = settings.monitoring.web
        self.monitor = monitor
        self._clock = clock_ms
        self._unix = unix_time
        self.users = UsersFile(self.web.users_file)
        self.store = AuthStore(settings.monitoring.data_dir / AUTH_DB_FILENAME, self.web.auth_db_max_bytes)
        login = self.web.login
        self.limiter = VerificationLimiter(login.argon2_max_concurrency, login.argon2_per_minute)
        self.env = Environment(
            loader=FileSystemLoader(str(BASE / "templates")),
            autoescape=select_autoescape(["html"], default=True),
        )
        icons = json.loads((BASE / "assets" / "icons.json").read_text(encoding="utf-8"))
        self.env.globals["icon"] = lambda name: Markup(
            '<svg class="bi" viewBox="0 0 16 16" aria-hidden="true" focusable="false">'
            + icons.get(name, "")
            + "</svg>"
        )
        self.static = _load_static()
        self.dashboard = Dashboard(
            settings.monitoring, monitor, self.store, settings.clients, clock=lambda: self._clock()
        )
        self.upstreams = UpstreamViews(
            self.dashboard,
            ollama_base_url=settings.ollama.base_url,
            known_models={p.model for p in settings.models.values()},
        )
        self._api_calls: dict[str, deque] = {}
        self._api_lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._last_cleanup = 0
        self.locked_reason: str | None = None
        if netaccess.uvicorn_rewrites_proxy_headers(argv, env):
            self.locked_reason = "uvicorn_proxy_headers"
        try:
            self.store.open()
        except Exception:
            self.locked_reason = self.locked_reason or "auth_store_unavailable"
        problem = self.locked_reason
        try:
            self.users.users()
        except UsersUnavailable as exc:
            problem = problem or exc.reason
        if problem:
            logger.error("monitoring web interface is locked: %s", problem)
            self._audit_safely("security_config_error", reason=problem, dedupe=True)

    def close(self) -> None:
        self.dashboard.close()
        self.store.close()

    # --- ASGI ---------------------------------------------------------------------

    async def __call__(self, scope, receive, send) -> None:
        try:
            response = await self._handle(scope, receive)
        except Exception:
            # No detail ever reaches the client; the reason is not logged
            # with request data either.
            logger.error("monitoring web request failed")
            response = self._plain(503)
        self._secure(response, cacheable=getattr(response, "dpm_static", False))
        await response(scope, receive, send)

    @staticmethod
    def _secure(response: Response, *, cacheable: bool) -> None:
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        response.headers["Cache-Control"] = "private, max-age=3600" if cacheable else "no-store"
        if not cacheable:
            response.headers["Pragma"] = "no-cache"

    def _plain(self, status: int, lang: str = "en") -> Response:
        key = {
            403: ("denied_title", "denied_text"),
            404: ("not_found", "not_found"),
            405: ("not_found", "not_found"),
            413: ("request_rejected", "request_rejected"),
        }.get(status, ("unavailable_title", "unavailable_text"))
        t = TEXTS[lang]
        try:
            return self._render("message.html", lang, status, heading=t[key[0]], text=t[key[1]])
        except Exception:
            return Response(t[key[0]], status_code=status, media_type="text/plain; charset=utf-8")

    def _render(self, template: str, lang: str, status: int = 200, **context) -> HTMLResponse:
        html = self.env.get_template(template).render(t=TEXTS[lang], lang=lang, **context)
        return HTMLResponse(html, status_code=status)

    async def _handle(self, scope, receive) -> Response:
        if scope.get("type") != "http":
            return Response(status_code=404)
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        headers = {}
        for key, value in scope.get("headers") or ():
            name = key.decode("latin-1").lower()
            if name not in headers:
                headers[name] = value.decode("latin-1")
        cookies = _cookies(headers.get("cookie", ""))
        lang = choose_language(cookies.get(LANG_COOKIE), headers.get("accept-language"))

        decision = netaccess.decide(scope, self.web)
        if not decision.allowed:
            await run_in_threadpool(
                self._audit_safely, "access_denied", ip=_ip(decision), reason=decision.reason, dedupe=True
            )
            return self._plain(403, lang)

        if path.startswith("/monitor/static/"):
            if method not in ("GET", "HEAD"):
                return self._plain(405, lang)
            entry = self.static.get(path)
            if entry is None:
                return self._plain(404, lang)
            response = Response(entry[0], media_type=entry[1])
            response.dpm_static = True
            return response

        allowed = ROUTES.get(path)
        if allowed is None:
            return self._plain(404, lang)
        if method not in allowed:
            return self._plain(405, lang)
        if self.locked_reason:
            return self._plain(503, lang)

        form: dict[str, str] = {}
        if method == "POST":
            body = await _read_body(receive, self.web.login.max_request_bytes)
            if body is None:
                return self._plain(413, lang)
            content_type = headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type != "application/x-www-form-urlencoded":
                return self._plain(403, lang)
            try:
                parsed = parse_qs(
                    body.decode("utf-8"), keep_blank_values=True, max_num_fields=10, strict_parsing=False
                )
            except (UnicodeDecodeError, ValueError):
                return self._plain(403, lang)
            form = {k: v[0] for k, v in parsed.items() if len(v) == 1}

        query = _query(scope.get("query_string") or b"")
        if query is None:
            return JSONResponse({"ok": False, "code": "invalid_query"}, status_code=400)
        if query and path not in DATA_API:
            return self._plain(404, lang)  # pages and auth routes take no query parameters
        ctx = Ctx(scope, method, path, headers, cookies, form, decision, lang, query)
        try:
            return await run_in_threadpool(self._dispatch, ctx)
        except (Locked, UsersUnavailable) as exc:
            reason = getattr(exc, "reason", "locked")
            await run_in_threadpool(
                self._audit_safely, "security_config_error", ip=ctx.ip, reason=reason, dedupe=True
            )
            return self._plain(503, lang)

    # --- routing (thread pool) ----------------------------------------------------------

    def _dispatch(self, ctx: Ctx) -> Response:
        # Fail closed before anything else: without a valid users file no
        # page (not even the login form) is served.
        self._users()
        self._maybe_cleanup()
        key = (ctx.path, ctx.method)
        if ctx.path in PAGES:
            return self._page(ctx, PAGES[ctx.path])
        if ctx.path in DATA_API:
            return self._data_api(ctx, DATA_API[ctx.path])
        if key == ("/monitor/session/activity", "POST"):
            return self._activity(ctx)
        if key == ("/monitor/login", "GET"):
            return self._login_page(ctx)
        if key == ("/monitor/login", "POST"):
            return self._login_submit(ctx)
        if key == ("/monitor/login/mfa", "GET"):
            return self._mfa_page(ctx)
        if key == ("/monitor/login/mfa", "POST"):
            return self._mfa_submit(ctx)
        if key == ("/monitor/logout", "POST"):
            return self._logout(ctx)
        if key == ("/monitor/api/v1/session", "GET"):
            return self._api_session(ctx)
        return self._api_status(ctx)

    # --- helpers -----------------------------------------------------------------------

    def _audit_safely(self, event: str, **fields) -> None:
        try:
            self.store.audit(self._clock(), event, **fields)
        except Exception:
            logger.error("monitoring audit write failed")

    def _maybe_cleanup(self) -> None:
        now = self._clock()
        if now - self._last_cleanup < CLEANUP_INTERVAL_MS or not self._cleanup_lock.acquire(blocking=False):
            return
        try:
            self._last_cleanup = now
            self.store.cleanup(
                now,
                idle_ms=self.web.session_idle_minutes * 60_000,
                window_ms=self.web.login.window_minutes * 60_000,
                audit_days=self.web.audit_days,
                max_audit_rows=self.web.max_audit_rows,
            )
        except Exception:
            logger.error("monitoring auth cleanup failed")
        finally:
            self._cleanup_lock.release()

    def _expected_origin(self, ctx: Ctx) -> str | None:
        if self.web.public_origin:
            return self.web.public_origin
        host = ctx.headers.get("host", "")
        if not _HOST.fullmatch(host):
            return None
        return ("https://" if ctx.decision.https else "http://") + host.lower()

    def _same_origin(self, ctx: Ctx) -> bool:
        expected = self._expected_origin(ctx)
        origin = ctx.headers.get("origin")
        if origin is not None:
            return expected is not None and hmac.compare_digest(origin.lower(), expected)
        return ctx.headers.get("sec-fetch-site") == "same-origin"

    def _csrf_ok(self, ctx: Ctx, expected_token: str | None) -> bool:
        presented = ctx.form.get("csrf", "")
        token_ok = bool(expected_token) and hmac.compare_digest(presented.encode(), expected_token.encode())
        return self._same_origin(ctx) and token_ok

    def _reject_csrf(self, ctx: Ctx) -> Response:
        self._audit_safely("csrf_failed", ip=ctx.ip, dedupe=True)
        return self._plain(403, ctx.lang)

    @staticmethod
    def _session_csrf(session_token: str) -> str:
        return hmac.new(session_token.encode(), b"docpipe-monitor-csrf", hashlib.sha256).hexdigest()

    @staticmethod
    def _cookie(response: Response, name: str, value: str, max_age: int | None = None) -> None:
        response.set_cookie(
            name, value, max_age=max_age, path="/", secure=True, httponly=True, samesite="strict"
        )

    @staticmethod
    def _clear(response: Response, name: str) -> None:
        response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="strict")

    def _users(self):
        return self.users.users()  # raises UsersUnavailable -> 503 (fail closed)

    def _session(self, ctx: Ctx, *, activity: bool):
        """(session row, user, token) or None. Only page navigations count as
        activity; API requests (dashboard polling) never extend the idle time."""
        token = ctx.cookies.get(SESSION_COOKIE)
        if not token or not _TOKEN.fullmatch(token):
            return None
        row = self.store.get_session(token)
        if row is None:
            return None
        now = self._clock()
        idle_ms = self.web.session_idle_minutes * 60_000
        if now >= row.absolute_expiry_ms or now >= row.last_activity_ms + idle_ms:
            reason = "absolute" if now >= row.absolute_expiry_ms else "idle"
            self.store.end_session(
                row.id_hash, now, "session_expired", username=row.username, ip=ctx.ip, reason=reason
            )
            return None
        user = self._users().get(row.username)
        if user is None or not user.enabled or user.fingerprint != row.fingerprint:
            self.store.end_session(
                row.id_hash,
                now,
                "session_revoked",
                username=row.username,
                ip=ctx.ip,
                reason="credentials_changed",
            )
            return None
        if activity:
            self.store.touch_session(row.id_hash, now)
            row = type(row)(
                row.id_hash, row.username, row.fingerprint, row.created_ms, now, row.absolute_expiry_ms
            )
        return row, user, token

    def _pre_auth_csrf(self, ctx: Ctx) -> tuple[str, bool]:
        token = ctx.cookies.get(CSRF_COOKIE)
        if token and _TOKEN.fullmatch(token):
            return token, False
        return secrets.token_urlsafe(32), True

    def _login_form(self, ctx: Ctx, *, step: str, error: str | None = None, status: int = 200) -> Response:
        csrf, new = self._pre_auth_csrf(ctx)
        response = self._render("login.html", ctx.lang, status, step=step, csrf=csrf, error=error)
        if new:
            self._cookie(response, CSRF_COOKIE, csrf)
        return response

    def _rate_subjects(self, ctx: Ctx, username: str) -> tuple[str, str]:
        return self.store.subject("ip:" + (ctx.ip or "")), self.store.subject("user:" + username)

    def _limited(self, ip_subject: str, user_subject: str) -> bool:
        now = self._clock()
        return self.store.locked("ip", ip_subject, now) or self.store.locked("user", user_subject, now)

    def _count_failure(self, ctx: Ctx, ip_subject: str, user_subject: str, username: str | None) -> None:
        login = self.web.login
        now = self._clock()
        window, lockout = login.window_minutes * 60_000, login.lockout_minutes * 60_000
        for scope, subject, limit in (
            ("ip", ip_subject, login.ip_max_failures),
            ("user", user_subject, login.user_max_failures),
        ):
            _, newly = self.store.record_failure(
                scope, subject, now, limit=limit, window_ms=window, lockout_ms=lockout
            )
            if newly:
                self.store.audit(
                    now, "lockout", username=username if scope == "user" else None, ip=ctx.ip, reason=scope
                )

    # --- pages -------------------------------------------------------------------------------

    def _poll_seconds(self) -> dict:
        return {
            "activity": self.web.poll_activity_seconds,
            "overview": self.web.poll_overview_seconds,
            "server": self.web.poll_overview_seconds,
            "clients": max(30, self.web.poll_overview_seconds),
            "performance": max(30, self.web.poll_overview_seconds),
            "security": max(30, self.web.poll_overview_seconds),
            "ollama": self.web.poll_overview_seconds,
            "stirling": self.web.poll_overview_seconds,
        }

    def _page(self, ctx: Ctx, page: str) -> Response:
        session = self._session(ctx, activity=True)  # a page view is user activity
        if session is None:
            return RedirectResponse("/monitor/login", status_code=303)
        row, user, token = session
        t = TEXTS[ctx.lang]
        return self._render(
            f"page_{page}.html",
            ctx.lang,
            page=page,
            username=user.username,
            csrf=self._session_csrf(token),
            sections=SECTIONS,
            poll_seconds=self._poll_seconds()[page],
            js_texts=json.dumps({k: t[k] for k in JS_KEYS if k in t}, ensure_ascii=True),
            clients=sorted(self.dashboard.known_clients),
            models=sorted(self.upstreams.known_models),
            ai_operations=sorted(AI_OPERATIONS),
            stirling_operations=sorted(STIRLING_OPERATION_BY_STEP.values()),
            services=sorted(s for s in self.dashboard_services()),
            idle_expires_ms=min(
                row.last_activity_ms + self.web.session_idle_minutes * 60_000, row.absolute_expiry_ms
            ),
        )

    @staticmethod
    def dashboard_services() -> list[str]:
        from system.monitoring.registry import SERVICES

        return [s for s in SERVICES if s != "none"]

    def _rate_limited(self, id_hash: str) -> bool:
        """Per-session cap on data API requests (browser polling and filter
        changes): protects DocPipe from runaway tabs or scripts."""
        now = time.monotonic()
        with self._api_lock:
            calls = self._api_calls.setdefault(id_hash, deque())
            while calls and now - calls[0] >= 60:
                calls.popleft()
            if len(calls) >= self.web.api_requests_per_minute:
                return True
            calls.append(now)
            if len(self._api_calls) > 1000:  # bounded: drop idle entries
                for key in [k for k, v in self._api_calls.items() if not v or now - v[-1] >= 60]:
                    self._api_calls.pop(key, None)
            return False

    def _data_api(self, ctx: Ctx, name: str) -> Response:
        session = self._session(ctx, activity=False)  # polling never extends the idle time
        if session is None:
            return JSONResponse({"ok": False, "code": "unauthorized"}, status_code=401)
        row, user, token = session
        if self._rate_limited(row.id_hash):
            return JSONResponse({"ok": False, "code": "rate_limited"}, status_code=429)
        try:
            if name == "meta":
                data = self.dashboard.meta(poll=self._poll_seconds(), web_settings=self.web)
            elif name in ("ollama", "stirling"):
                data = getattr(self.upstreams, name)(ctx.query)
            elif name == "security":
                data = self.dashboard.security(
                    ctx.query,
                    username=user.username,
                    current_session=token,
                    idle_ms=self.web.session_idle_minutes * 60_000,
                )
            else:
                data = getattr(self.dashboard, name)(ctx.query)
        except BadRequest as exc:
            return JSONResponse({"ok": False, "code": exc.code}, status_code=400)
        except TelemetryUnavailable:
            return JSONResponse({"ok": False, "code": "telemetry_unavailable"}, status_code=503)
        return JSONResponse(data)

    def _activity(self, ctx: Ctx) -> Response:
        """Explicit user action (e.g. a filter change) that may extend the idle
        time. CSRF-protected, never called by timers. The server cannot prove a
        human caused it - it only accepts it from the signed-in browser page
        (session-bound token + same origin), at most every 30 s, and never
        beyond the absolute timeout (unchanged)."""
        session = self._session(ctx, activity=False)
        if session is None:
            return JSONResponse({"ok": False, "code": "unauthorized"}, status_code=401)
        row, _, token = session
        if not self._csrf_ok(ctx, self._session_csrf(token)):
            return self._reject_csrf(ctx)
        now = self._clock()
        if now - row.last_activity_ms >= ACTIVITY_MIN_INTERVAL_MS:
            self.store.touch_session(row.id_hash, now)
        return Response(status_code=204)

    def _login_page(self, ctx: Ctx) -> Response:
        if self._session(ctx, activity=False) is not None:
            return RedirectResponse("/monitor", status_code=303)
        return self._login_form(ctx, step="password")

    def _login_submit(self, ctx: Ctx) -> Response:
        csrf_cookie = ctx.cookies.get(CSRF_COOKIE)
        if not csrf_cookie or not _TOKEN.fullmatch(csrf_cookie) or not self._csrf_ok(ctx, csrf_cookie):
            return self._reject_csrf(ctx)
        username = unicodedata.normalize("NFKC", ctx.form.get("username", "")).strip().lower()[:64]
        password = ctx.form.get("password", "")[: MAX_PASSWORD_LENGTH + 1]
        failure = self._login_form(ctx, step="password", error=ctx.t["login_failed"], status=401)
        ip_subject, user_subject = self._rate_subjects(ctx, username)
        if self._limited(ip_subject, user_subject):
            self.store.audit(self._clock(), "login_throttled", ip=ctx.ip, reason="locked", dedupe=True)
            return failure
        user = self._users().get(username) if USERNAME.fullmatch(username) else None
        usable = user is not None and user.enabled
        result = self.limiter.verify(user.password_hash if usable else None, password)
        if result is None:
            self.store.audit(self._clock(), "login_throttled", ip=ctx.ip, reason="capacity", dedupe=True)
            return failure
        if not result:
            self._count_failure(ctx, ip_subject, user_subject, user.username if user else None)
            self.store.audit(
                self._clock(),
                "login_failed",
                username=user.username if user else None,
                ip=ctx.ip,
                reason="credentials",
            )
            return failure
        pending = secrets.token_urlsafe(32)
        self.store.create_pending(
            pending, user.username, user.fingerprint, self._clock(), self.web.mfa_pending_seconds * 1000
        )
        response = RedirectResponse("/monitor/login/mfa", status_code=303)
        self._cookie(response, MFA_COOKIE, pending, max_age=self.web.mfa_pending_seconds)
        return response

    def _pending(self, ctx: Ctx):
        token = ctx.cookies.get(MFA_COOKIE)
        if not token or not _TOKEN.fullmatch(token):
            return None, None
        return token, self.store.get_pending(token, self._clock())

    def _expired_login(self, ctx: Ctx) -> Response:
        response = self._login_form(ctx, step="password", error=ctx.t["login_expired"], status=401)
        self._clear(response, MFA_COOKIE)
        return response

    def _mfa_page(self, ctx: Ctx) -> Response:
        _, pending = self._pending(ctx)
        if pending is None:
            return RedirectResponse("/monitor/login", status_code=303)
        return self._login_form(ctx, step="mfa")

    def _mfa_submit(self, ctx: Ctx) -> Response:
        csrf_cookie = ctx.cookies.get(CSRF_COOKIE)
        if not csrf_cookie or not _TOKEN.fullmatch(csrf_cookie) or not self._csrf_ok(ctx, csrf_cookie):
            return self._reject_csrf(ctx)
        token, _ = self._pending(ctx)
        if token is None:
            return self._expired_login(ctx)
        now = self._clock()
        pending = self.store.pending_attempt(token, now, self.web.login.mfa_max_attempts)
        if pending is None:
            return self._expired_login(ctx)
        user = self._users().get(pending.username)
        if user is None or not user.enabled or user.fingerprint != pending.fingerprint:
            self.store.delete_pending(token)
            return self._expired_login(ctx)
        failure = self._login_form(ctx, step="mfa", error=ctx.t["mfa_failed"], status=401)
        ip_subject, user_subject = self._rate_subjects(ctx, user.username)
        if self._limited(ip_subject, user_subject):
            self.store.audit(
                now, "login_throttled", username=user.username, ip=ctx.ip, reason="locked", dedupe=True
            )
            return failure
        counter = totp.verify(
            user.totp_key, ctx.form.get("code", ""), self._unix(), self.web.totp_window_steps
        )
        if counter is None:
            self._count_failure(ctx, ip_subject, user_subject, user.username)
            self.store.audit(now, "mfa_failed", username=user.username, ip=ctx.ip)
            return failure
        session_token = secrets.token_urlsafe(32)
        accepted = self.store.complete_login(
            pending_token=token,
            session_token=session_token,
            username=user.username,
            fingerprint=user.fingerprint,
            counter=counter,
            now_ms=now,
            absolute_ms=self.web.session_absolute_hours * 3_600_000,
            max_sessions=self.web.max_sessions_per_user,
            ip=ctx.ip,
            old_session_token=ctx.cookies.get(SESSION_COOKIE),
        )
        if not accepted:  # TOTP replay - audited inside the transaction
            self._count_failure(ctx, ip_subject, user_subject, user.username)
            return failure
        self.store.reset_failures("user", user_subject, now)
        self.store.reset_failures("ip", ip_subject, now)
        response = RedirectResponse("/monitor", status_code=303)
        self._cookie(response, SESSION_COOKIE, session_token)
        self._clear(response, MFA_COOKIE)
        self._clear(response, CSRF_COOKIE)
        return response

    def _logout(self, ctx: Ctx) -> Response:
        session = self._session(ctx, activity=False)
        if session is None:
            response = RedirectResponse("/monitor/login", status_code=303)
            self._clear(response, SESSION_COOKIE)
            return response
        row, user, token = session
        if not self._csrf_ok(ctx, self._session_csrf(token)):
            return self._reject_csrf(ctx)
        self.store.end_session(row.id_hash, self._clock(), "logout", username=user.username, ip=ctx.ip)
        response = RedirectResponse("/monitor/login", status_code=303)
        self._clear(response, SESSION_COOKIE)
        return response

    # --- protected API (no idle extension) ---------------------------------------------------

    def _api_session(self, ctx: Ctx) -> Response:
        session = self._session(ctx, activity=False)
        if session is None:
            return JSONResponse({"ok": False, "code": "unauthorized"}, status_code=401)
        row, user, _ = session
        return JSONResponse(
            {
                "ok": True,
                "username": user.username,
                "server_time_ms": self._clock(),
                "idle_expires_ms": min(
                    row.last_activity_ms + self.web.session_idle_minutes * 60_000, row.absolute_expiry_ms
                ),
                "absolute_expires_ms": row.absolute_expiry_ms,
            }
        )

    def _api_status(self, ctx: Ctx) -> Response:
        if self._session(ctx, activity=False) is None:
            return JSONResponse({"ok": False, "code": "unauthorized"}, status_code=401)
        try:
            health = self.monitor.health() if self.monitor is not None else None
        except Exception:
            health = None
        if health is None:
            return JSONResponse(
                {
                    "ok": True,
                    "collector": None,
                    "store": None,
                    "last_flush_ms": None,
                    "requests_buffered": None,
                    "dropped": None,
                }
            )
        totals = health.get("totals", {})
        return JSONResponse(
            {
                "ok": True,
                "collector": health.get("collector"),
                "store": health.get("store"),
                "last_flush_ms": health.get("last_flush_ms"),
                "requests_buffered": health.get("requests_buffered"),
                "dropped": totals.get("requests_dropped_buffer", 0) + totals.get("requests_dropped_store", 0),
            }
        )


def _query(raw: bytes) -> dict[str, str] | None:
    """Strict query parsing: bounded size and field count, every name at
    most once, valid UTF-8. None = reject the request."""
    if len(raw) > MAX_QUERY_BYTES:
        return None
    try:
        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=False, max_num_fields=MAX_QUERY_FIELDS)
    except (UnicodeDecodeError, ValueError):
        return None
    if any(len(v) != 1 for v in parsed.values()):
        return None
    return {k: v[0] for k, v in parsed.items()}


def _ip(decision: netaccess.AccessDecision) -> str | None:
    return str(decision.client_ip) if decision.client_ip else None


def _cookies(header: str) -> dict[str, str]:
    """name -> value; the first occurrence of a name wins (a later, injected
    duplicate cannot override it). Malformed pairs are skipped individually,
    so another application's unusual cookie on the same host cannot break
    the monitoring login."""
    cookies: dict[str, str] = {}
    if len(header) > 8192:
        return cookies
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name and name not in cookies:
            cookies[name] = value.strip().strip('"')
    return cookies


async def _read_body(receive, limit: int) -> bytes | None:
    body = bytearray()
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return None
        body += message.get("body") or b""
        if len(body) > limit:
            return None
        if not message.get("more_body"):
            return bytes(body)


def build_web(settings, monitor) -> MonitorWeb | None:
    """The interface, or None (requests then get the normal 404) if it could
    not even be constructed - logged, never raised into DocPipe's startup."""
    try:
        return MonitorWeb(settings, monitor)
    except Exception:
        logger.error("monitoring web interface could not be started")
        return None
