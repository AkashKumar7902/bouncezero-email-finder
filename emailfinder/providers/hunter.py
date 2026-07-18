"""Hunter.io adapter — secondary finder + verifier + pattern-into-KB (dossier 6.1).

Interface-complete over three Hunter v2 endpoints:

    GET /v2/email-finder    -> ProviderFindResult (score 0-100 + sources[])
    GET /v2/email-verifier  -> ProviderVerifyResult (valid|invalid|accept_all|
                               webmail|disposable|unknown; HTTP 202 => pending)
    GET /v2/domain-search   -> (template, separator) pattern for a KB upsert

Key mapping nuance (dossier 6.3): a ``webmail`` verify result maps to
``Status.UNKNOWN`` AND sets the ``webmail`` flag — a webmail address may still be
deliverable, so it must NOT be called undeliverable.

Both documented rate windows are enforced client-side (15 rps + 500 rpm finder /
10 rps + 300 rpm verifier); a local overrun raises :class:`ErrRateLimited`.
"""
from __future__ import annotations

import threading
import time

from emailfinder.errors import ErrRateLimited
from emailfinder.models import Status
from emailfinder.providers.base import (
    FOUND_CATCH_ALL,
    FOUND_UNVERIFIED,
    FOUND_VERIFIED,
    NOT_FOUND,
    EmailFinder,
    EmailVerifier,
    FindRequest,
    ProviderFindResult,
    ProviderVerifyResult,
    _http_request,
    _raise_for_common_status,
)

_BASE = "https://api.hunter.io/v2"
_NAME = "hunter"

# Documented Hunter rate windows (enforce BOTH per endpoint).
_FINDER_RPS = 15
_FINDER_RPM = 500
_VERIFIER_RPS = 10
_VERIFIER_RPM = 300


class _RateWindow:
    """A tiny thread-safe sliding-window rate guard for one endpoint.

    Tracks call timestamps and raises :class:`ErrRateLimited` when either the
    per-second or per-minute allowance would be exceeded, so we honor Hunter's
    published limits without silently blocking a batch.
    """

    def __init__(self, per_second: int, per_minute: int):
        self._per_second = per_second
        self._per_minute = per_minute
        self._calls: list[float] = []
        self._lock = threading.Lock()

    def check(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._calls = [t for t in self._calls if now - t < 60.0]
            last_second = sum(1 for t in self._calls if now - t < 1.0)
            if last_second >= self._per_second:
                raise ErrRateLimited(
                    f"{_NAME}: exceeded {self._per_second} rps", retry_after=1.0
                )
            if len(self._calls) >= self._per_minute:
                oldest = min(self._calls)
                raise ErrRateLimited(
                    f"{_NAME}: exceeded {self._per_minute} rpm",
                    retry_after=max(1.0, 60.0 - (now - oldest)),
                )
            self._calls.append(now)


class HunterAdapter(EmailFinder, EmailVerifier):
    """Concrete Hunter.io adapter (finder + verifier + domain-search)."""

    def __init__(self, api_key: str, session=None):
        self._api_key = api_key
        self._session = session
        self._finder_window = _RateWindow(_FINDER_RPS, _FINDER_RPM)
        self._verifier_window = _RateWindow(_VERIFIER_RPS, _VERIFIER_RPM)

    def name(self) -> str:
        return _NAME

    def healthy(self) -> bool:
        return bool(self._api_key)

    def estimated_cost_credits(self, req: FindRequest) -> float:
        return 1.0

    # --- finder -------------------------------------------------------------
    def find(self, req: FindRequest) -> ProviderFindResult:
        """GET /v2/email-finder; maps Hunter's score/verification into the
        unified finder result."""
        req.validate()
        norm = req.normalized()
        self._finder_window.check()

        params = {
            "api_key": self._api_key,
            "domain": norm.domain,
            "company": norm.company_name,
        }
        if norm.first_name:
            params["first_name"] = norm.first_name
        if norm.last_name:
            params["last_name"] = norm.last_name
        if norm.full_name and not (norm.first_name and norm.last_name):
            params["full_name"] = norm.full_name

        start = time.monotonic()
        resp = _http_request(
            "GET", f"{_BASE}/email-finder", params=params,
            timeout_ms=norm.timeout_ms, session=self._session, provider_name=_NAME,
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code == 202:
            # Long-running: treat as unverified-pending, no charge.
            return ProviderFindResult(
                email=None, status=NOT_FOUND, confidence=0, provider=_NAME,
                credits_charged=0.0, latency_ms=latency_ms, raw=resp.json(),
            )
        if resp.status_code != 200:
            _raise_for_common_status(resp, _NAME)

        return _map_find(resp.json(), latency_ms)

    # --- verifier -----------------------------------------------------------
    def verify(
        self, email: str, *, timeout_ms: int = 15000, deep: bool = False
    ) -> ProviderVerifyResult:
        """GET /v2/email-verifier; webmail -> UNKNOWN + webmail flag."""
        self._verifier_window.check()
        params = {"api_key": self._api_key, "email": email}
        resp = _http_request(
            "GET", f"{_BASE}/email-verifier", params=params,
            timeout_ms=timeout_ms, session=self._session, provider_name=_NAME,
        )
        if resp.status_code == 202:
            # >20s pending -> UNKNOWN, retry later.
            return ProviderVerifyResult(
                email=email, status=Status.UNKNOWN, reason="greylisted",
                provider=_NAME, credits_charged=0.0, raw=resp.json(),
            )
        if resp.status_code != 200:
            _raise_for_common_status(resp, _NAME)
        return _map_verify(email, resp.json())

    # --- domain-search (pattern into KB) ------------------------------------
    def domain_pattern(self, domain: str) -> tuple[str, str] | None:
        """GET /v2/domain-search; return (template, separator) for a KB upsert.

        Hunter reports e.g. ``{first}.{last}`` -> ('first.last', '.') and
        ``{f}{last}`` -> ('flast', ''). Returns None when no pattern is known.
        """
        self._finder_window.check()
        params = {"api_key": self._api_key, "domain": domain.strip().lower()}
        resp = _http_request(
            "GET", f"{_BASE}/domain-search", params=params,
            session=self._session, provider_name=_NAME,
        )
        if resp.status_code != 200:
            _raise_for_common_status(resp, _NAME)
        data = resp.json()
        pattern = (data.get("data") or {}).get("pattern")
        return _pattern_to_template(pattern)


# --- module-private helpers -------------------------------------------------
def _map_find(data: dict, latency_ms: int) -> ProviderFindResult:
    """Map a /email-finder body onto the unified finder result."""
    d = data.get("data") or {}
    email = d.get("email")
    score = int(d.get("score") or 0)
    verification = ((d.get("verification") or {}).get("status") or "").lower()
    pattern_hint = None  # email-finder does not itself return a domain pattern

    if not email:
        return ProviderFindResult(
            email=None, status=NOT_FOUND, confidence=0, provider=_NAME,
            credits_charged=0.0, latency_ms=latency_ms, raw=data,
        )
    if verification == "valid":
        status = FOUND_VERIFIED
    elif verification == "accept_all":
        status = FOUND_CATCH_ALL
    else:
        status = FOUND_UNVERIFIED
    return ProviderFindResult(
        email=email, status=status, confidence=score, pattern_hint=pattern_hint,
        provider=_NAME, credits_charged=1.0, latency_ms=latency_ms, raw=data,
    )


def _map_verify(email: str, data: dict) -> ProviderVerifyResult:
    """Map a /email-verifier body per the dossier-6.3 status table."""
    d = data.get("data") or {}
    result = (d.get("result") or d.get("status") or "unknown").lower()
    is_disposable = bool(d.get("disposable"))
    is_webmail = bool(d.get("webmail"))
    is_role = bool(d.get("gibberish") is False and d.get("role"))  # tolerant
    is_role = bool(d.get("role", is_role))
    score = d.get("score")
    score = int(score) if score is not None else None

    if result == "valid":
        status, reason, catch_all = Status.DELIVERABLE, "other", False
    elif result == "invalid":
        status, reason, catch_all = Status.UNDELIVERABLE, "mailbox_not_found", False
    elif result == "accept_all":
        status, reason, catch_all = Status.RISKY, "catch_all", True
    elif result == "disposable":
        status, reason, catch_all = Status.RISKY, "disposable", False
        is_disposable = True
    elif result == "webmail":
        # webmail -> UNKNOWN + webmail flag (may still be deliverable).
        status, reason, catch_all = Status.UNKNOWN, "other", False
        is_webmail = True
    else:  # unknown / anything else
        status, reason, catch_all = Status.UNKNOWN, "other", False

    return ProviderVerifyResult(
        email=email, status=status, reason=reason, is_catch_all=catch_all,
        is_disposable=is_disposable, is_role=is_role, webmail=is_webmail,
        score=score, provider=_NAME, credits_charged=1.0, raw=data,
    )


def _pattern_to_template(pattern: str | None) -> tuple[str, str] | None:
    """Translate a Hunter ``{first}.{last}`` style pattern to (template, sep).

    Recognizes '.', '_', '-' separators between two placeholders and the common
    single-token forms; returns None when the pattern is empty/unknown.
    """
    if not pattern:
        return None
    p = pattern.strip().lower()
    for sep in (".", "_", "-"):
        if sep in p:
            left, _, right = p.partition(sep)
            left_t = _placeholder(left)
            right_t = _placeholder(right)
            if left_t and right_t:
                return (f"{left_t}{sep}{right_t}", sep)
    # No separator: contiguous placeholders like {f}{last} -> flast.
    tokens = _split_placeholders(p)
    if tokens:
        return ("".join(tokens), "")
    return None


def _placeholder(part: str) -> str | None:
    """Map one Hunter placeholder token to our template vocabulary."""
    part = part.strip("{}")
    return {
        "first": "first",
        "f": "f",
        "last": "last",
        "l": "l",
    }.get(part)


def _split_placeholders(pattern: str) -> list[str]:
    """Split a no-separator pattern like ``{f}{last}`` into ['f','last']."""
    out: list[str] = []
    buf = ""
    depth = 0
    for ch in pattern:
        if ch == "{":
            depth += 1
            buf = ""
        elif ch == "}":
            depth -= 1
            mapped = _placeholder(buf)
            if mapped is None:
                return []
            out.append(mapped)
        elif depth:
            buf += ch
    return out
