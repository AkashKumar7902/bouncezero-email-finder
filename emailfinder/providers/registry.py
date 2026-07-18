"""Provider registry — builds enabled adapters and orchestrates paid lookups.

Responsibilities (dossier 6.3):
  * Build only the ENABLED adapters from :class:`Config`, ordered by priority.
  * Enforce the MANDATORY cache-before-call (sha256 idempotency) so a repeat
    lookup NEVER double-charges a paid provider.
  * Route paid calls ONLY to domains where local guessing cannot help — M365,
    confirmed catch-all, or a domain with no dominant KB pattern (~68-70% cut).
  * Honor per-day credit budgets, typed errors (auth/quota/rate/timeout/down),
    and short-circuit on the first FOUND_VERIFIED.
  * Empty / zero-provider config => an inert registry (never touches network).
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from emailfinder.config import Config, ProviderConfig
from emailfinder.errors import (
    ErrAuth,
    ErrBadInput,
    ErrProviderDown,
    ErrQuotaExhausted,
    ErrRateLimited,
    ErrTimeout,
)
from emailfinder.models import Provider, Status
from emailfinder.providers.base import (
    FOUND_VERIFIED,
    EmailFinder,
    EmailVerifier,
    FindRequest,
    ProviderFindResult,
)

if TYPE_CHECKING:  # cache.py is a peer module; only needed for type hints here.
    from emailfinder.cache import Cache

# Registered adapter classes keyed by their canonical config ``name``. Adapters
# are imported lazily inside the factory so an unused vendor never imports its
# (optional) transport deps.
_CIRCUIT_BREAK_SECONDS = 300.0  # 5-minute circuit break on ErrProviderDown
_FIND_TTL_DAYS = 90             # AMF 30-day / Prospeo 90-day free-repeat window
_VERIFY_TTL_DAYS = 30


@dataclass
class _AdapterState:
    """Per-adapter session state the registry mutates as calls succeed/fail."""

    adapter: object
    cfg: ProviderConfig
    disabled: bool = False               # set on ErrAuth (fail-fast)
    circuit_open_until: float = 0.0      # monotonic ts; >now => skip
    credits_used: float = 0.0
    credits_day: str = ""
    flags: dict = field(default_factory=dict)


class ProviderRegistry:
    """Holds the enabled adapters and runs the cache-guarded fallback chain."""

    def __init__(
        self,
        cfg: Config,
        cache: Cache,
        finders: list[_AdapterState] | None = None,
        verifiers: list[_AdapterState] | None = None,
    ):
        self._cfg = cfg
        self._cache = cache
        self._finders = finders or []
        self._verifiers = verifiers or []

    # --- introspection ------------------------------------------------------
    def is_empty(self) -> bool:
        """True when no adapter is enabled (zero-provider / inert mode)."""
        return not self._finders and not self._verifiers

    def should_route(
        self,
        provider: Provider,
        is_catch_all: bool | None,
        has_kb_pattern: bool,
    ) -> bool:
        """Route to paid providers ONLY when local guessing can't help.

        True iff the domain is Microsoft 365 (no verifier resolves M365 5.4.1),
        is a confirmed catch-all, or has no dominant KB pattern. A PROBE-class
        domain that DOES have a KB pattern returns False (skip paid calls).
        Always False when the registry is inert.
        """
        if self.is_empty():
            return False
        if provider == Provider.MICROSOFT365:
            return True
        if is_catch_all is True:
            return True
        if not has_kb_pattern:
            return True
        return False

    # --- orchestration ------------------------------------------------------
    def find_with_fallback(
        self,
        req: FindRequest,
        provider: Provider,
        is_catch_all: bool | None,
    ) -> ProviderFindResult | None:
        """Run the finder chain with a mandatory cache check before each call.

        Order (dossier 6.3): try each enabled finder by priority; a returned
        unverified email is upgraded via an available verifier. Short-circuits
        on the first FOUND_VERIFIED. Honors typed errors, daily budgets, and
        circuit-breaks. Returns the best result, or None when nothing usable
        was produced.
        """
        if self.is_empty():
            return None
        try:
            req.validate()
        except ErrBadInput:
            return None

        best: ProviderFindResult | None = None
        for state in self._finders:
            if not self._available(state):
                continue
            result = self._run_finder(state, req)
            if result is None:
                continue
            if result.status == FOUND_VERIFIED:
                return result  # short-circuit
            # Remember the best non-verified result; try to upgrade it.
            if best is None or result.email:
                best = result
            if result.email:
                upgraded = self._try_verify(result)
                if upgraded is not None and upgraded.status == FOUND_VERIFIED:
                    return upgraded
                if upgraded is not None:
                    best = upgraded
        return best

    def verify_with_fallback(
        self, email: str, *, timeout_ms: int = 15000
    ) -> ProviderFindResult | None:
        """Verify an existing address through the verifier chain (cache-first).

        Returns a :class:`ProviderFindResult` (FOUND_VERIFIED on DELIVERABLE)
        so the engine can treat verifier confirmation like a finder hit.
        """
        for state in self._verifiers:
            if not self._available(state):
                continue
            vres = self._run_verifier(state, email, timeout_ms)
            if vres is None:
                continue
            status = FOUND_VERIFIED if vres.status == Status.DELIVERABLE else "FOUND_UNVERIFIED"
            fr = ProviderFindResult(
                email=email,
                status=status,
                confidence=(vres.score if vres.score is not None else 0),
                provider=vres.provider,
                credits_charged=vres.credits_charged,
                raw=vres.raw,
            )
            if status == FOUND_VERIFIED:
                return fr
        return None

    # --- private: single-adapter runners (cache-guarded) --------------------
    def _run_finder(
        self, state: _AdapterState, req: FindRequest
    ) -> ProviderFindResult | None:
        adapter: EmailFinder = state.adapter  # type: ignore[assignment]
        name = adapter.name()
        key = self._cache.api_key(name, req.cache_input())

        # MANDATORY cache-before-call: never double-charge.
        cached = self._cache.get_api(key, _FIND_TTL_DAYS)
        if cached is not None:
            return _find_from_cache(cached)

        if not self._within_budget(state):
            return None

        result = self._call_with_typed_errors(state, lambda: adapter.find(req))
        if result is None:
            return None
        self._charge(state, result.credits_charged)
        self._cache.put_api(key, asdict(result), _FIND_TTL_DAYS)
        return result

    def _run_verifier(self, state: _AdapterState, email: str, timeout_ms: int):
        adapter: EmailVerifier = state.adapter  # type: ignore[assignment]
        name = adapter.name()
        key = self._cache.api_key(name, email.strip().lower())

        cached = self._cache.get_api(key, _VERIFY_TTL_DAYS)
        if cached is not None:
            return _verify_from_cache(cached)

        if not self._within_budget(state):
            return None

        result = self._call_with_typed_errors(
            state, lambda: adapter.verify(email, timeout_ms=timeout_ms)
        )
        if result is None:
            return None
        self._charge(state, result.credits_charged)
        self._cache.put_api(key, _verify_to_cache(result), _VERIFY_TTL_DAYS)
        return result

    def _try_verify(self, find_result: ProviderFindResult):
        """Upgrade an unverified finder hit through the verifier chain."""
        if not find_result.email or not self._verifiers:
            return None
        return self.verify_with_fallback(find_result.email)

    # --- private: error handling / budgets ----------------------------------
    def _call_with_typed_errors(self, state: _AdapterState, thunk):
        """Invoke ``thunk`` translating typed errors into registry state changes.

        Returns the call result, or None when the adapter should be skipped for
        this lookup (the specific side effect depends on the error kind).
        """
        name = state.cfg.name
        retried = False
        while True:
            try:
                return thunk()
            except ErrAuth:
                state.disabled = True          # fail-fast: disable for session
                return None
            except ErrQuotaExhausted:
                # Out of credits: skip until the next billing tick (today done).
                state.credits_day = date.today().isoformat()
                state.credits_used = float("inf")
                return None
            except ErrRateLimited:
                return None                    # honor the limit; skip this call
            except ErrTimeout:
                if retried:
                    return None
                retried = True                 # retry once
                continue
            except ErrProviderDown:
                state.circuit_open_until = time.monotonic() + _CIRCUIT_BREAK_SECONDS
                return None
            except ErrBadInput:
                return None                    # non-retryable for this adapter

    def _available(self, state: _AdapterState) -> bool:
        """True when the adapter is enabled, not circuit-broken, has budget."""
        if state.disabled:
            return False
        if state.circuit_open_until > time.monotonic():
            return False
        return self._within_budget(state)

    def _within_budget(self, state: _AdapterState) -> bool:
        limit = state.cfg.max_credits_per_day
        if limit is None:
            return True
        today = date.today().isoformat()
        if state.credits_day != today:
            state.credits_day = today
            state.credits_used = 0.0
        return state.credits_used < limit

    def _charge(self, state: _AdapterState, credits: float) -> None:
        if not credits:
            return
        today = date.today().isoformat()
        if state.credits_day != today:
            state.credits_day = today
            state.credits_used = 0.0
        state.credits_used += credits


# --- factory ----------------------------------------------------------------
def build_registry(cfg: Config, cache: Cache) -> ProviderRegistry:
    """Instantiate only the enabled adapters, ordered by priority.

    An adapter is skipped when its ``api_key_env`` is unset, its ``name`` is
    unknown, or ``enabled`` is False. With no enabled providers (the default),
    the returned registry is inert.
    """
    finders: list[_AdapterState] = []
    verifiers: list[_AdapterState] = []

    if not getattr(cfg, "enable_providers", False):
        return ProviderRegistry(cfg, cache, finders, verifiers)

    for pconf in sorted(cfg.providers, key=lambda p: p.priority):
        if not pconf.enabled:
            continue
        api_key = os.environ.get(pconf.api_key_env, "").strip()
        if not api_key:
            continue
        adapter = _instantiate(pconf.name, api_key)
        if adapter is None:
            continue
        state = _AdapterState(adapter=adapter, cfg=pconf)
        role = (pconf.role or "finder").lower()
        if isinstance(adapter, EmailFinder) and role in ("finder", "both"):
            finders.append(state)
        if isinstance(adapter, EmailVerifier) and role in ("verifier", "both"):
            verifiers.append(state)

    return ProviderRegistry(cfg, cache, finders, verifiers)


def _instantiate(name: str, api_key: str):
    """Lazily import + construct an adapter by canonical name; None if unknown."""
    key = name.strip().lower()
    if key in ("anymailfinder", "anymail", "amf"):
        from emailfinder.providers.anymailfinder import AnymailFinderAdapter

        return AnymailFinderAdapter(api_key)
    if key == "hunter":
        from emailfinder.providers.hunter import HunterAdapter

        return HunterAdapter(api_key)
    if key in ("millionverifier", "mv"):
        from emailfinder.providers.millionverifier import MillionVerifierAdapter

        return MillionVerifierAdapter(api_key)
    return None


# --- cache (de)serialization helpers ----------------------------------------
def _find_from_cache(data: dict) -> ProviderFindResult:
    """Rebuild a ProviderFindResult from a cached dict, dropping any charge."""
    return ProviderFindResult(
        email=data.get("email"),
        status=data.get("status", "NOT_FOUND"),
        confidence=int(data.get("confidence", 0) or 0),
        pattern_hint=data.get("pattern_hint"),
        provider=data.get("provider", ""),
        credits_charged=0.0,  # served from cache: no charge on a repeat
        latency_ms=int(data.get("latency_ms", 0) or 0),
        raw=data.get("raw", {}),
    )


def _verify_to_cache(result) -> dict:
    """Serialize a ProviderVerifyResult to a JSON-safe dict (Status -> str)."""
    d = asdict(result)
    status = d.get("status")
    d["status"] = status.value if isinstance(status, Status) else status
    return d


def _verify_from_cache(data: dict):
    """Rebuild a ProviderVerifyResult from a cached dict, dropping any charge."""
    from emailfinder.providers.base import ProviderVerifyResult

    status = data.get("status", "unknown")
    try:
        status = Status(status)
    except ValueError:
        status = Status.UNKNOWN
    return ProviderVerifyResult(
        email=data.get("email", ""),
        status=status,
        reason=data.get("reason", "other"),
        is_catch_all=bool(data.get("is_catch_all")),
        is_disposable=bool(data.get("is_disposable")),
        is_role=bool(data.get("is_role")),
        webmail=bool(data.get("webmail")),
        score=data.get("score"),
        provider=data.get("provider", ""),
        credits_charged=0.0,
        raw=data.get("raw", {}),
    )
