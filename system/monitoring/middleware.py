"""Pure ASGI middleware measuring every HTTP request.

Always installed (system/main.py), but a straight pass-through while
`app.state.monitoring` is None - i.e. whenever monitoring is disabled.

It never changes a request or a response: messages are forwarded as they
are; the wrappers only count body bytes and read the response status. All
monitoring work is wrapped so that an error in it cannot fail the request.
Unhandled application exceptions are recorded (status 500,
internal_error) and re-raised unchanged to Starlette's error handling.
"""

from __future__ import annotations

from system.monitoring import context


class MonitoringMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        app = scope.get("app")
        monitor = getattr(getattr(app, "state", None), "monitoring", None) if app is not None else None
        if monitor is None:
            await self.app(scope, receive, send)
            return
        try:
            metrics = monitor.begin(scope)
        except Exception:
            monitor.hook_failed()
            await self.app(scope, receive, send)
            return

        async def counting_receive():
            message = await receive()
            try:
                if message.get("type") == "http.request":
                    metrics.bytes_in += len(message.get("body") or b"")
            except Exception:
                metrics.hook_failures += 1
            return message

        async def observing_send(message) -> None:
            try:
                kind = message.get("type")
                if kind == "http.response.start":
                    metrics.status_code = int(message.get("status", 0))
                elif kind == "http.response.body":
                    metrics.bytes_out += len(message.get("body") or b"")
            except Exception:
                metrics.hook_failures += 1
            await send(message)

        token = context.activate(metrics)
        try:
            await self.app(scope, counting_receive, observing_send)
        except Exception:
            metrics.unhandled = True
            raise
        finally:
            context.deactivate(token)
            try:
                monitor.end(metrics, scope)
            except Exception:
                monitor.hook_failed()
