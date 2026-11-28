"""Load generator for DocPipe (Phase M7). Synthetic requests only.

Runs inside the DocPipe image (it uses system.monitoring.web.totp for the
dashboard logins) in the network namespace of the DocPipe container, so the
monitoring interface is reached over loopback as in development mode.

    python loadgen.py --concurrency 50 --duration 60 --mix health=4,extract=3,analyze=3 \
        --dashboards 3 --users-file /work/users.json --out /work/result.json

Latencies are measured client-side (perf_counter around each request,
connection reuse per worker). The warm-up phase is not counted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import statistics
import time

import httpx

PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 44>>stream\nBT /F1 12 Tf 20 100 Td (Synthetic text) Tj ET\nendstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)
CSRF = re.compile(r'name="csrf" value="([^"]+)"')
PAGES = ("overview", "activity", "clients", "performance", "server", "ollama", "stirling", "security")


def pct(values: list[float], p: float):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, min(len(ordered) - 1, int(round(p * len(ordered) + 0.5)) - 1))], 2)


def summary(lat: list[float], seconds: float) -> dict:
    return {
        "count": len(lat),
        "throughput_rps": round(len(lat) / seconds, 1) if seconds else None,
        "mean_ms": round(statistics.fmean(lat), 2) if lat else None,
        "p50_ms": pct(lat, 0.50),
        "p95_ms": pct(lat, 0.95),
        "p99_ms": pct(lat, 0.99),
        "max_ms": round(max(lat), 2) if lat else None,
    }


async def api_worker(client, kinds, weights, keys, deadline, warm_until, out, statuses):
    while time.monotonic() < deadline:
        kind = random.choices(kinds, weights)[0]
        started = time.perf_counter()
        try:
            if kind == "health":
                r = await client.get("/api/v1/health")
            elif kind == "extract":
                r = await client.post(
                    "/api/v1/documents/extract-text",
                    headers={"Authorization": f"Bearer {keys['documents']}"},
                    files={"file": ("synthetic.pdf", PDF, "application/pdf")},
                )
            else:
                r = await client.post(
                    "/api/v1/documents/analyze",
                    headers={"Authorization": f"Bearer {keys['ai']}"},
                    json={
                        "mode": "maintenance_extraction",
                        "text": "Synthetic maintenance text " * 20,
                        "context": {},
                    },
                )
            status = r.status_code
        except httpx.HTTPError as exc:
            status = type(exc).__name__
        elapsed = (time.perf_counter() - started) * 1000
        statuses["_sent_total"] = statuses.get("_sent_total", 0) + 1  # incl. warm-up, for completeness
        if time.monotonic() >= warm_until:
            out.setdefault(kind, []).append(elapsed)
            statuses[f"{kind}:{status}"] = statuses.get(f"{kind}:{status}", 0) + 1


class Jar(httpx.AsyncClient):
    """The monitoring cookies are Secure (__Host- prefix). Browsers treat
    127.0.0.1 as a secure context; httpx does not send Secure cookies over
    plain HTTP, so the load generator carries them explicitly."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.jar: dict[str, str] = {}

    async def send(self, request, **kwargs):
        if self.jar:
            request.headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.jar.items())
        response = await super().send(request, **kwargs)
        for header in response.headers.get_list("set-cookie"):
            name, _, rest = header.partition("=")
            value = rest.split(";", 1)[0]
            if "max-age=0" in header.lower() or not value:
                self.jar.pop(name.strip(), None)
            else:
                self.jar[name.strip()] = value
        return response


async def login(client, user: dict, base: str) -> bool:
    from system.monitoring.web import totp

    headers = {"Origin": base, "Content-Type": "application/x-www-form-urlencoded"}
    page = await client.get("/monitor/login")
    match = CSRF.search(page.text)
    if match is None:  # interface locked or unavailable on this worker
        return False
    token = match.group(1)
    first = await client.post(
        "/monitor/login",
        data={"csrf": token, "username": user["username"], "password": user["password"]},
        headers=headers,
    )
    if first.status_code != 303:
        return False
    mfa = await client.get("/monitor/login/mfa")
    code = totp.code_at(totp.decode_secret(user["totp_secret"]), totp.counter_at(time.time()))
    match = CSRF.search(mfa.text)
    if match is None:
        return False
    done = await client.post(
        "/monitor/login/mfa", data={"csrf": match.group(1), "code": code}, headers=headers
    )
    return done.status_code == 303 and done.headers.get("location") == "/monitor"


async def dashboard_worker(base, user, interval, deadline, warm_until, out, statuses):
    async with Jar(base_url=base, timeout=30) as client:
        if not await login(client, user, base):
            statuses["dashboard:login_failed"] = statuses.get("dashboard:login_failed", 0) + 1
            return
        i = 0
        while time.monotonic() < deadline:
            page = PAGES[i % len(PAGES)]
            i += 1
            rng = random.choice(("1h", "24h", "7d"))
            started = time.perf_counter()
            r = await client.get(
                f"/monitor/api/v1/{page}",
                params={"range": rng} if page != "activity" else {},
                headers={"X-Monitor-Poll": "1"},
            )
            elapsed = (time.perf_counter() - started) * 1000
            if time.monotonic() >= warm_until:
                out.setdefault(f"dash_{page}", []).append(elapsed)
                statuses[f"dashboard:{r.status_code}"] = statuses.get(f"dashboard:{r.status_code}", 0) + 1
            await asyncio.sleep(interval)


async def main(args) -> dict:
    mix = dict(item.split("=") for item in args.mix.split(","))
    kinds, weights = list(mix), [float(v) for v in mix.values()]
    keys = {"documents": args.documents_key, "ai": args.ai_key}
    start = time.monotonic()
    warm_until = start + args.warmup
    deadline = warm_until + args.duration
    lat: dict[str, list[float]] = {}
    statuses: dict[str, int] = {}
    users = json.load(open(args.users_file)) if args.dashboards else []
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(base_url=args.base, timeout=60, limits=limits) as client:
        tasks = [
            api_worker(client, kinds, weights, keys, deadline, warm_until, lat, statuses)
            for _ in range(args.concurrency)
        ]
        tasks += [
            dashboard_worker(
                args.base, users[i % len(users)], args.dashboard_interval, deadline, warm_until, lat, statuses
            )
            for i in range(args.dashboards)
        ]
        await asyncio.gather(*tasks)
    seconds = args.duration
    api = [v for k, vals in lat.items() if not k.startswith("dash_") for v in vals]
    dash = [v for k, vals in lat.items() if k.startswith("dash_") for v in vals]
    return {
        "concurrency": args.concurrency,
        "duration_s": seconds,
        "api": summary(api, seconds),
        "by_kind": {k: summary(v, seconds) for k, v in sorted(lat.items()) if not k.startswith("dash_")},
        "dashboard": summary(dash, seconds),
        "dashboard_by_page": {
            k[5:]: summary(v, seconds) for k, v in sorted(lat.items()) if k.startswith("dash_")
        },
        "statuses": dict(sorted(statuses.items())),
        "sent_after_warmup": sum(len(v) for k, v in lat.items() if not k.startswith("dash_")),
        "sent_total": statuses.pop("_sent_total", 0),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--warmup", type=float, default=5)
    parser.add_argument("--mix", default="health=4,extract=3,analyze=3")
    parser.add_argument("--dashboards", type=int, default=0)
    parser.add_argument("--dashboard-interval", type=float, default=1.0)
    parser.add_argument("--users-file", default="/work/users.json")
    parser.add_argument("--documents-key", default="load-documents-key")
    parser.add_argument("--ai-key", default="load-ai-key")
    parser.add_argument("--out", default="")
    a = parser.parse_args()
    result = asyncio.run(main(a))
    text = json.dumps(result, indent=1)
    if a.out:
        open(a.out, "w").write(text)
    print(text)
