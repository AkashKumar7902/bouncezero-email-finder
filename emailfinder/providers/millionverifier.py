"""MillionVerifier adapter — cheap bulk verifier (dossier 6.1).

``GET api/v3/`` returns ``result`` in {ok, invalid, catch_all, unknown,
unverified, disposable}. Mapping (dossier 6.3 table):

    ok                    -> DELIVERABLE
    invalid               -> UNDELIVERABLE (reason=mailbox_not_found)
    catch_all, disposable -> RISKY
    unknown, unverified   -> UNKNOWN

CRITICAL: MillionVerifier has NO server-side dedupe, so every repeat call is
charged. The sha256 cache in ``cache.py`` MUST be consulted before this adapter
is invoked — that is enforced by the registry, not here.
"""
from __future__ import annotations

from emailfinder.models import Status
from emailfinder.providers.base import (
    EmailVerifier,
    ProviderVerifyResult,
    _http_request,
    _raise_for_common_status,
)

_ENDPOINT = "https://api.millionverifier.com/api/v3/"
_NAME = "millionverifier"


class MillionVerifierAdapter(EmailVerifier):
    """Concrete :class:`EmailVerifier` over the MillionVerifier v3 API."""

    def __init__(self, api_key: str, session=None):
        self._api_key = api_key
        self._session = session

    def name(self) -> str:
        return _NAME

    def healthy(self) -> bool:
        return bool(self._api_key)

    def verify(
        self, email: str, *, timeout_ms: int = 15000, deep: bool = False
    ) -> ProviderVerifyResult:
        """Verify one address; map ``result`` per the dossier-6.3 table."""
        params = {
            "api": self._api_key,
            "email": email,
            # v3 accepts a timeout in seconds (default 20, min 2, max 60).
            "timeout": max(2, min(60, int(timeout_ms / 1000) or 20)),
        }
        resp = _http_request(
            "GET", _ENDPOINT, params=params, timeout_ms=timeout_ms,
            session=self._session, provider_name=_NAME,
        )
        if resp.status_code != 200:
            _raise_for_common_status(resp, _NAME)
        return _map_verify(email, resp.json())


# --- module-private helpers -------------------------------------------------
def _map_verify(email: str, data: dict) -> ProviderVerifyResult:
    """Map a MillionVerifier v3 body onto :class:`ProviderVerifyResult`."""
    result = (data.get("result") or "unknown").lower()
    sub = (data.get("subresult") or "").lower()
    quality = data.get("quality")
    is_disposable = result == "disposable" or "disposable" in sub
    is_role = "role" in sub
    is_catch_all = result == "catch_all" or "catch" in sub

    if result == "ok":
        status, reason = Status.DELIVERABLE, "other"
    elif result == "invalid":
        status, reason = Status.UNDELIVERABLE, "mailbox_not_found"
    elif result == "catch_all":
        status, reason = Status.RISKY, "catch_all"
    elif result == "disposable":
        status, reason = Status.RISKY, "disposable"
    else:  # unknown, unverified, anything else -> UNKNOWN (never invalid)
        status, reason = Status.UNKNOWN, "other"

    score = None
    if isinstance(quality, (int, float)):
        score = int(quality)

    return ProviderVerifyResult(
        email=email,
        status=status,
        reason=reason,
        is_catch_all=is_catch_all,
        is_disposable=is_disposable,
        is_role=is_role,
        webmail=False,
        score=score,
        provider=_NAME,
        credits_charged=1.0,
        raw=data,
    )
