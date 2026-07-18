"""Anymail Finder adapter — the ONE concrete reference paid finder (dossier 6.1).

``POST /v5.1/find-email/person`` with bare-key ``Authorization`` header. Accepts a
``linkedin_url`` alone or a name plus a company/domain. Anymail Finder charges
ONLY for a verified find (risky / not_found / blacklisted are free, with 30-day
free repeats), so we set ``credits_charged`` accordingly.

Status mapping (dossier 6.3 table):
    valid     -> FOUND_VERIFIED
    risky     -> FOUND_CATCH_ALL   (catch-all/unverifiable — pattern-only)
    not_found -> NOT_FOUND

Transport uses httpx when importable, otherwise stdlib urllib (via the shared
``base._http_request`` helper). Vendor error statuses become typed errors.
"""
from __future__ import annotations

import time

from emailfinder.errors import ErrBadInput
from emailfinder.providers.base import (
    FOUND_CATCH_ALL,
    FOUND_UNVERIFIED,
    FOUND_VERIFIED,
    NOT_FOUND,
    EmailFinder,
    FindRequest,
    ProviderFindResult,
    _http_request,
    _raise_for_common_status,
)

_ENDPOINT = "https://api.anymailfinder.com/v5.1/find-email/person"
_NAME = "anymailfinder"
# Anymail Finder documents no rate limits and a 180-second timeout.
_DEFAULT_TIMEOUT_MS = 180000


class AnymailFinderAdapter(EmailFinder):
    """Concrete :class:`EmailFinder` over the Anymail Finder v5.1 API."""

    def __init__(self, api_key: str, session=None):
        """``api_key`` is sent as a bare ``Authorization`` header value.

        ``session`` is an optional ``httpx.Client`` for connection reuse; when
        absent (or when httpx is not installed) a one-shot request is made.
        """
        self._api_key = api_key
        self._session = session

    def name(self) -> str:
        return _NAME

    def estimated_cost_credits(self, req: FindRequest) -> float:
        """One credit is billed only on a verified find; estimate 1.0."""
        return 1.0

    def healthy(self) -> bool:
        """Healthy iff an API key is configured (no network probe)."""
        return bool(self._api_key)

    def find(self, req: FindRequest) -> ProviderFindResult:
        """Resolve one person to an email via Anymail Finder.

        Validates + normalizes ``req``, dispatches, and maps the vendor's
        ``valid/risky/not_found`` verdict onto the unified status enum.
        """
        req.validate()
        norm = req.normalized()

        payload = _build_payload(norm)
        timeout_ms = norm.timeout_ms or _DEFAULT_TIMEOUT_MS
        headers = {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        start = time.monotonic()
        resp = _http_request(
            "POST",
            _ENDPOINT,
            headers=headers,
            json_body=payload,
            timeout_ms=timeout_ms,
            session=self._session,
            provider_name=_NAME,
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        # 200 with a body carrying results, or 404 which AMF uses for "no email
        # found" (free). Any other error status becomes a typed error.
        if resp.status_code not in (200, 404):
            _raise_for_common_status(resp, _NAME)

        data = resp.json()
        return _map_result(data, resp.status_code, latency_ms)


# --- module-private helpers -------------------------------------------------
def _build_payload(req: FindRequest) -> dict:
    """Assemble the v5.1 person payload from a normalized request."""
    payload: dict = {}
    if req.linkedin_url:
        payload["linkedin_url"] = req.linkedin_url
    if req.full_name:
        payload["full_name"] = req.full_name
    if req.first_name:
        payload["first_name"] = req.first_name
    if req.last_name:
        payload["last_name"] = req.last_name
    if req.domain:
        payload["domain"] = req.domain
    if req.company_name:
        payload["company_name"] = req.company_name
    if not payload:
        raise ErrBadInput(f"{_NAME}: empty request payload")
    return payload


def _map_result(data: dict, status_code: int, latency_ms: int) -> ProviderFindResult:
    """Map an Anymail Finder response body onto :class:`ProviderFindResult`.

    AMF returns a top-level ``email_status`` in {valid, risky, not_found} (older
    payloads use a ``results`` envelope). Only ``valid`` is charged.
    """
    results = data.get("results") if isinstance(data.get("results"), dict) else data
    email = results.get("email") or data.get("email")
    validation = (
        results.get("email_status")
        or results.get("validation")
        or data.get("email_status")
        or data.get("validation")
        or ""
    ).lower()

    if status_code == 404 or validation == "not_found" or not email:
        return ProviderFindResult(
            email=None,
            status=NOT_FOUND,
            confidence=0,
            pattern_hint=None,
            provider=_NAME,
            credits_charged=0.0,   # not_found is free
            latency_ms=latency_ms,
            raw=data,
        )

    if validation == "valid":
        return ProviderFindResult(
            email=email,
            status=FOUND_VERIFIED,
            confidence=int(results.get("confidence", 95) or 95),
            pattern_hint=results.get("pattern") or data.get("pattern"),
            provider=_NAME,
            credits_charged=1.0,   # charged only on a verified find
            latency_ms=latency_ms,
            raw=data,
        )

    if validation == "risky":
        return ProviderFindResult(
            email=email,
            status=FOUND_CATCH_ALL,
            confidence=int(results.get("confidence", 50) or 50),
            pattern_hint=results.get("pattern") or data.get("pattern"),
            provider=_NAME,
            credits_charged=0.0,   # risky is free
            latency_ms=latency_ms,
            raw=data,
        )

    # An email came back with an unknown validation string: unverified, free.
    return ProviderFindResult(
        email=email,
        status=FOUND_UNVERIFIED,
        confidence=int(results.get("confidence", 40) or 40),
        pattern_hint=results.get("pattern") or data.get("pattern"),
        provider=_NAME,
        credits_charged=0.0,
        latency_ms=latency_ms,
        raw=data,
    )
