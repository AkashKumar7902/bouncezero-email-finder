"""Abstract paid-provider interfaces + request/result dataclasses (dossier 6.3).

Paid providers are OPTIONAL and OFF by default. When enabled they are routed
ONLY to the domains where local pattern-guessing + SMTP probing cannot help:
Microsoft 365, confirmed catch-all, and domains with no learned KB pattern.

Nothing here performs I/O; concrete adapters (anymailfinder/hunter/millionverifier)
subclass these ABCs and do the HTTP work, mapping each vendor's documented status
enum onto the unified shapes below.
"""
from __future__ import annotations

import json as _json
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from emailfinder.errors import (
    ErrAuth,
    ErrBadInput,
    ErrProviderDown,
    ErrQuotaExhausted,
    ErrRateLimited,
    ErrTimeout,
)
from emailfinder.models import Status

# --- ProviderFindResult.status values (dossier 6.3) -------------------------
FOUND_VERIFIED = "FOUND_VERIFIED"
FOUND_UNVERIFIED = "FOUND_UNVERIFIED"
FOUND_CATCH_ALL = "FOUND_CATCH_ALL"
NOT_FOUND = "NOT_FOUND"


@dataclass
class FindRequest:
    """A validated finder payload.

    Providers accept EITHER a ``linkedin_url`` alone OR a name (full, or
    first+last) together with a company identifier (``domain`` or
    ``company_name``). Casing/diacritics are normalized before dispatch via
    :meth:`normalized`.
    """

    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    domain: str | None = None
    company_name: str | None = None
    linkedin_url: str | None = None
    timeout_ms: int = 30000

    def has_name(self) -> bool:
        """True if a usable name is present (full name or both first+last)."""
        if self.full_name and self.full_name.strip():
            return True
        return bool(
            self.first_name and self.first_name.strip()
            and self.last_name and self.last_name.strip()
        )

    def has_company(self) -> bool:
        """True if a company identifier (domain or company name) is present."""
        return bool(
            (self.domain and self.domain.strip())
            or (self.company_name and self.company_name.strip())
        )

    def validate(self) -> None:
        """Enforce the dossier-6.3 rule: linkedin_url alone OR name+company.

        Raises :class:`ErrBadInput` (non-retryable) when neither shape holds.
        """
        if self.linkedin_url and self.linkedin_url.strip():
            return
        if self.has_name() and self.has_company():
            return
        raise ErrBadInput(
            "FindRequest requires a linkedin_url alone, or a name plus a "
            "company/domain"
        )

    def normalized(self) -> "FindRequest":
        """Return a copy with casing/diacritics folded on the name/company/domain.

        The linkedin_url is preserved verbatim (it is a pass-through identifier,
        never fetched here).
        """
        return FindRequest(
            first_name=_fold(self.first_name),
            last_name=_fold(self.last_name),
            full_name=_fold(self.full_name),
            domain=self.domain.strip().lower() if self.domain else None,
            company_name=_fold_keep_spaces(self.company_name),
            linkedin_url=self.linkedin_url,
            timeout_ms=self.timeout_ms,
        )

    def cache_input(self) -> str:
        """A stable, order-independent string used to build the sha256 cache key.

        Two requests that a provider would answer identically must map to the
        same string so the mandatory cache-before-call never double-charges.
        """
        n = self.normalized()
        parts = [
            f"ln={n.linkedin_url or ''}",
            f"fn={n.first_name or ''}",
            f"ls={n.last_name or ''}",
            f"full={n.full_name or ''}",
            f"dom={n.domain or ''}",
            f"co={n.company_name or ''}",
        ]
        return "|".join(parts)


@dataclass
class ProviderFindResult:
    """Unified finder output.

    ``status`` in {FOUND_VERIFIED, FOUND_UNVERIFIED, FOUND_CATCH_ALL, NOT_FOUND}.
    ``credits_charged`` is 0.0 for anything the vendor bills for free
    (risky/not_found on Anymail Finder, etc.).
    """

    email: str | None
    status: str
    confidence: int = 0
    pattern_hint: str | None = None
    provider: str = ""
    credits_charged: float = 0.0
    latency_ms: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class ProviderVerifyResult:
    """Unified verifier output, mapped from each vendor per the dossier-6.3 table."""

    email: str
    status: Status
    reason: str = "other"
    is_catch_all: bool = False
    is_disposable: bool = False
    is_role: bool = False
    webmail: bool = False
    score: int | None = None
    provider: str = ""
    credits_charged: float = 0.0
    raw: dict = field(default_factory=dict)


class EmailFinder(ABC):
    """Finder interface (dossier 6.3)."""

    @abstractmethod
    def name(self) -> str:
        """Stable provider identifier (equals the cache-key namespace)."""

    @abstractmethod
    def find(self, req: FindRequest) -> ProviderFindResult:
        """Resolve a person to an email; validates + normalizes ``req`` first."""

    def estimated_cost_credits(self, req: FindRequest) -> float:
        """Best-effort estimate of credits a single find would cost."""
        return 1.0

    def healthy(self) -> bool:
        """Cheap liveness/credentials check; default True."""
        return True


class EmailVerifier(ABC):
    """Verifier interface (dossier 6.3)."""

    @abstractmethod
    def name(self) -> str:
        """Stable provider identifier (equals the cache-key namespace)."""

    @abstractmethod
    def verify(
        self, email: str, *, timeout_ms: int = 15000, deep: bool = False
    ) -> ProviderVerifyResult:
        """Check a single address; returns a unified :class:`ProviderVerifyResult`."""

    def healthy(self) -> bool:
        """Cheap liveness/credentials check; default True."""
        return True


# --- module-private helpers -------------------------------------------------
def _fold(text: str | None) -> str | None:
    """Lowercase + strip diacritics from a single token-ish field."""
    if not text:
        return None
    folded = _strip_diacritics(text).strip().lower()
    return folded or None


def _fold_keep_spaces(text: str | None) -> str | None:
    """Fold diacritics/casing but keep internal whitespace (company names)."""
    if not text:
        return None
    folded = _strip_diacritics(text).strip().lower()
    folded = " ".join(folded.split())
    return folded or None


def _strip_diacritics(text: str) -> str:
    """NFKD-decompose and drop combining marks (stdlib only)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


@dataclass
class _HttpResponse:
    """A tiny provider-agnostic HTTP response the adapters can parse."""

    status_code: int
    headers: dict
    text: str

    def json(self) -> dict:
        """Parse the body as JSON, tolerating an empty/invalid body."""
        if not self.text:
            return {}
        try:
            return _json.loads(self.text)
        except ValueError:
            return {}


def _http_request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    json_body: dict | None = None,
    timeout_ms: int = 30000,
    session=None,
    provider_name: str = "provider",
) -> _HttpResponse:
    """Perform one HTTP request using httpx if importable, else urllib.

    Transport-level failures are translated to typed :mod:`emailfinder.errors`
    so callers branch on the error kind, never on a message string:

    * connect/read timeout -> :class:`ErrTimeout`
    * connection refused / network down -> :class:`ErrProviderDown`

    HTTP status codes (401/403/429/5xx) are NOT raised here — the adapter maps
    them, because the credit/auth semantics differ per vendor.
    """
    method = method.upper()
    timeout_s = max(0.001, timeout_ms / 1000.0)

    # Preferred path: httpx (optional dep). Reuse a caller-provided session.
    try:
        import httpx  # type: ignore
    except ImportError:
        httpx = None  # type: ignore

    if httpx is not None:
        try:
            if session is not None and isinstance(session, httpx.Client):
                resp = session.request(
                    method, url, headers=headers, params=params,
                    json=json_body, timeout=timeout_s,
                )
            else:
                resp = httpx.request(
                    method, url, headers=headers, params=params,
                    json=json_body, timeout=timeout_s,
                )
        except httpx.TimeoutException as exc:  # type: ignore[attr-defined]
            raise ErrTimeout(f"{provider_name}: request timed out") from exc
        except httpx.HTTPError as exc:  # type: ignore[attr-defined]
            raise ErrProviderDown(f"{provider_name}: transport error: {exc}") from exc
        return _HttpResponse(
            status_code=resp.status_code,
            headers={k.lower(): v for k, v in resp.headers.items()},
            text=resp.text,
        )

    # Fallback: stdlib urllib.
    import urllib.error
    import urllib.parse
    import urllib.request

    full_url = url
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if query:
            sep = "&" if "?" in full_url else "?"
            full_url = f"{full_url}{sep}{query}"

    data = None
    req_headers = dict(headers or {})
    if json_body is not None:
        data = _json.dumps(json_body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(
        full_url, data=data, headers=req_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as fh:
            body = fh.read().decode("utf-8", errors="replace")
            return _HttpResponse(
                status_code=fh.getcode(),
                headers={k.lower(): v for k, v in fh.headers.items()},
                text=body,
            )
    except urllib.error.HTTPError as exc:
        # An HTTP error status still carries a body the adapter must inspect.
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return _HttpResponse(
            status_code=exc.code,
            headers={k.lower(): v for k, v in (exc.headers or {}).items()},
            text=raw,
        )
    except (TimeoutError, ) as exc:
        raise ErrTimeout(f"{provider_name}: request timed out") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError):
            raise ErrTimeout(f"{provider_name}: request timed out") from exc
        raise ErrProviderDown(f"{provider_name}: transport error: {reason}") from exc


def _raise_for_common_status(
    resp: _HttpResponse, provider_name: str
) -> None:
    """Map the auth/quota/rate/5xx HTTP statuses shared by all vendors.

    Adapters call this after their own vendor-specific 2xx handling so the
    remaining error statuses become typed errors the registry understands.
    2xx is a no-op. 402/403-quota vs 401-auth is a per-vendor nuance the
    adapter may handle before delegating here.
    """
    code = resp.status_code
    if code < 400:
        return
    if code in (401, 403):
        raise ErrAuth(f"{provider_name}: authentication failed (HTTP {code})")
    if code == 402:
        raise ErrQuotaExhausted(f"{provider_name}: out of credits (HTTP 402)")
    if code == 429:
        retry_after = _parse_retry_after(resp.headers.get("retry-after"))
        raise ErrRateLimited(
            f"{provider_name}: rate limited (HTTP 429)", retry_after=retry_after
        )
    if code >= 500:
        raise ErrProviderDown(f"{provider_name}: server error (HTTP {code})")
    # 400/404/422 and other 4xx -> bad, non-retryable input.
    raise ErrBadInput(f"{provider_name}: bad request (HTTP {code})")


def _parse_retry_after(value) -> float:
    """Parse a Retry-After header into seconds; default 60s when absent/odd."""
    if not value:
        return 60.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 60.0
