"""FastAPI public web app over the pure ``emailfinder`` core.

Additive surface (see ``webapp/CONTRACT.md``): a single self-contained page plus
JSON endpoints wrapping :class:`webapp.service.HostedFinder`. Verification is OFF
here — results are pattern + provider-aware only (provider is still classified
from a live DNS MX lookup, which drives the M365 / catch-all scoring caps).

Store selection at startup:
  * ``DATABASE_URL`` set  -> :class:`webapp.store_pg.PgStore` + schema init;
  * otherwise             -> in-memory :class:`webapp.store.MemoryStore` (a
    logged WARNING; dev only — state is lost on restart).

Per-IP sliding-window rate limiting (default 30/min + 300/day) and a blanket
error wrapper (no stack trace ever reaches a client) are applied as middleware.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from webapp.page import PAGE
from webapp.service import HostedFinder
from webapp.store import MemoryStore

logger = logging.getLogger("webapp")

# --------------------------------------------------------------------------- #
# Rate-limit knobs (env-tunable; in-memory, resets on restart / per instance)
# --------------------------------------------------------------------------- #
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


RATE_PER_MIN = _int_env("RATE_LIMIT_PER_MIN", 30)
RATE_PER_DAY = _int_env("RATE_LIMIT_PER_DAY", 300)
_WINDOW_MIN = 60.0
_WINDOW_DAY = 86400.0


# --------------------------------------------------------------------------- #
# Store / finder wiring (ONE HostedFinder for the whole process)
# --------------------------------------------------------------------------- #
def _build_finder() -> HostedFinder:
    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if dsn:
        # Imported lazily so the module imports fine even without psycopg.
        from webapp.store_pg import PgStore, init_schema

        store = PgStore(dsn)
        _init_pg_schema(store, init_schema)
        logger.info("using Postgres store (DATABASE_URL configured)")
        return HostedFinder(store)
    logger.warning("ephemeral in-memory store — dev only (set DATABASE_URL for persistence)")
    return HostedFinder(MemoryStore())


def _init_pg_schema(store, init_schema) -> None:
    """Create tables idempotently, tolerating either store convention.

    The contract exposes a module-level ``init_schema(conn)``; a PgStore may also
    surface its own ``init_schema()`` method and/or a live connection attribute.
    Try the method first, then the module function against a connection.
    """
    method = getattr(store, "init_schema", None)
    if callable(method):
        method()
        return
    conn = None
    for attr in ("conn", "_conn", "connection"):
        conn = getattr(store, attr, None)
        if conn is not None:
            break
    if conn is None:
        connect = getattr(store, "connect", None) or getattr(store, "_connect", None)
        if callable(connect):
            conn = connect()
    init_schema(conn)


app = FastAPI(title="BounceZero hosted email finder", docs_url=None, redoc_url=None)
_finder = _build_finder()


# --------------------------------------------------------------------------- #
# Middleware: per-IP sliding-window rate limit + error wrapping
# --------------------------------------------------------------------------- #
_hits: dict[str, deque] = {}
_hits_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        # First hop is the original client (Render sets X-Forwarded-For).
        return fwd.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    with _hits_lock:
        dq = _hits.get(ip)
        if dq is None:
            dq = deque()
            _hits[ip] = dq
        cutoff = now - _WINDOW_DAY
        while dq and dq[0] < cutoff:
            dq.popleft()
        minute_start = now - _WINDOW_MIN
        in_minute = sum(1 for t in dq if t >= minute_start)
        if len(dq) >= RATE_PER_DAY or in_minute >= RATE_PER_MIN:
            return True
        dq.append(now)
        return False


@app.middleware("http")
async def _guard(request: Request, call_next):
    path = request.url.path
    if path != "/healthz":
        if _rate_limited(_client_ip(request)):
            return JSONResponse(
                {"error": "rate_limited",
                 "detail": f"rate limit exceeded ({RATE_PER_MIN}/min, {RATE_PER_DAY}/day)"},
                status_code=429,
            )
    try:
        return await call_next(request)
    except Exception:  # noqa: BLE001 - never leak a stack trace to a client
        logger.exception("unhandled error on %s %s", request.method, path)
        return JSONResponse({"error": "internal_error"}, status_code=500)


# --------------------------------------------------------------------------- #
# body helper
# --------------------------------------------------------------------------- #
async def _json_body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _clean(data: dict, key: str):
    val = data.get(key)
    if val is None:
        return None
    val = str(val).strip()
    return val or None


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(PAGE)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/api/find")
async def api_find(request: Request):
    data = await _json_body(request)
    try:
        result = _finder.find(
            name=_clean(data, "name"),
            domain=_clean(data, "domain"),
            company=_clean(data, "company"),
            linkedin_url=_clean(data, "linkedin_url"),
        )
    except Exception as exc:  # noqa: BLE001 - client-facing, no trace
        logger.exception("find failed")
        return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
    return JSONResponse(result)


@app.post("/api/optout")
async def api_optout(request: Request):
    data = await _json_body(request)
    email = _clean(data, "email")
    name = _clean(data, "name")
    domain = _clean(data, "domain")
    if not email and not (name and domain):
        return JSONResponse(
            {"error": "bad_request", "detail": "provide an email or a name + domain"},
            status_code=400,
        )
    try:
        _finder.optout(email=email, name=name, domain=domain)
    except Exception as exc:  # noqa: BLE001
        logger.exception("optout failed")
        return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
    return Response(status_code=204)


@app.get("/api/kb/{domain}")
async def api_kb(domain: str):
    domain = (domain or "").strip().lower()
    try:
        entry = _finder.kb_entry(domain)
    except Exception as exc:  # noqa: BLE001
        logger.exception("kb lookup failed")
        return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
    if entry is None:
        return JSONResponse({"found": False, "domain": domain})
    return JSONResponse({"found": True, "domain": domain, **_jsonable(entry)})


def _jsonable(obj):
    """Recursively convert sets to sorted lists so the KB entry is JSON-safe."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj
