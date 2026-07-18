"""Tests for the pluggable paid-provider adapters + registry.

httpx/respx are optional and absent in this env, so we mock at the shared
``_http_request`` seam (monkeypatched in each adapter's namespace) rather than
at the socket layer. The behaviors asserted here mirror the MODULE_CONTRACTS
"providers" test-plan bullet:

  * status mapping frozen (MillionVerifier ok/invalid/catch_all; Hunter webmail
    -> UNKNOWN + flag; Anymail valid/risky/not_found)
  * typed errors on 401 / 429
  * registry consults the sha256 cache BEFORE calling (zero double-charge)
  * should_route True only for M365 / catch-all / no-KB-pattern; False for a
    PROBE domain that has a KB pattern
  * empty config -> inert registry
"""
from __future__ import annotations

import hashlib

import pytest

from emailfinder.config import Config, ProviderConfig
from emailfinder.errors import ErrAuth, ErrBadInput, ErrRateLimited
from emailfinder.models import Provider, Status
from emailfinder.providers import base as base_mod
from emailfinder.providers.anymailfinder import AnymailFinderAdapter
from emailfinder.providers.base import (
    FOUND_CATCH_ALL,
    FOUND_VERIFIED,
    NOT_FOUND,
    EmailFinder,
    FindRequest,
    ProviderFindResult,
    _HttpResponse,
)
from emailfinder.providers.hunter import HunterAdapter
from emailfinder.providers.millionverifier import MillionVerifierAdapter
from emailfinder.providers.registry import (
    ProviderRegistry,
    _AdapterState,
    build_registry,
)


# --- test doubles -----------------------------------------------------------
class FakeCache:
    """Duck-typed stand-in for cache.Cache (get_api/put_api/api_key)."""

    def __init__(self):
        self.store: dict[str, dict] = {}

    @staticmethod
    def api_key(provider: str, normalized_input: str) -> str:
        return hashlib.sha256((provider + normalized_input).encode()).hexdigest()

    def get_api(self, key: str, ttl_days: int):
        return self.store.get(key)

    def put_api(self, key: str, value: dict, ttl_days: int) -> None:
        self.store[key] = value


def _resp(status_code=200, body: dict | None = None, headers: dict | None = None):
    import json

    return _HttpResponse(
        status_code=status_code,
        headers=headers or {},
        text=json.dumps(body or {}),
    )


def _patch_http(monkeypatch, module, response):
    """Patch the module-local ``_http_request`` to return ``response``.

    ``response`` may be a callable(**kwargs) or a static _HttpResponse.
    """

    def fake(*args, **kwargs):
        if callable(response):
            return response(*args, **kwargs)
        return response

    monkeypatch.setattr(module, "_http_request", fake)


# --- FindRequest validation / normalization ---------------------------------
def test_findrequest_requires_linkedin_or_name_plus_company():
    with pytest.raises(ErrBadInput):
        FindRequest(first_name="ada").validate()
    FindRequest(linkedin_url="https://linkedin.com/in/ada").validate()
    FindRequest(full_name="Ada Lovelace", domain="acme.com").validate()


def test_findrequest_normalizes_diacritics_and_casing():
    req = FindRequest(first_name="José", last_name="Muñoz", domain="ACME.com")
    norm = req.normalized()
    assert norm.first_name == "jose"
    assert norm.last_name == "munoz"
    assert norm.domain == "acme.com"


def test_cache_input_is_stable():
    a = FindRequest(full_name="Ada Lovelace", domain="Acme.com").cache_input()
    b = FindRequest(full_name="ada lovelace", domain="acme.com").cache_input()
    assert a == b


# --- Anymail Finder mapping -------------------------------------------------
def test_anymail_valid_maps_to_found_verified(monkeypatch):
    from emailfinder.providers import anymailfinder as mod

    _patch_http(monkeypatch, mod, _resp(200, {
        "results": {"email": "ada@acme.com", "email_status": "valid", "confidence": 97}
    }))
    adapter = AnymailFinderAdapter("key")
    res = adapter.find(FindRequest(full_name="Ada Lovelace", domain="acme.com"))
    assert res.status == FOUND_VERIFIED
    assert res.email == "ada@acme.com"
    assert res.credits_charged == 1.0  # charged only on verified find


def test_anymail_risky_maps_to_catch_all_free(monkeypatch):
    from emailfinder.providers import anymailfinder as mod

    _patch_http(monkeypatch, mod, _resp(200, {
        "results": {"email": "ada@acme.com", "email_status": "risky"}
    }))
    res = AnymailFinderAdapter("key").find(
        FindRequest(full_name="Ada Lovelace", domain="acme.com")
    )
    assert res.status == FOUND_CATCH_ALL
    assert res.credits_charged == 0.0  # risky is free


def test_anymail_not_found_free(monkeypatch):
    from emailfinder.providers import anymailfinder as mod

    _patch_http(monkeypatch, mod, _resp(404, {"email_status": "not_found"}))
    res = AnymailFinderAdapter("key").find(
        FindRequest(full_name="Ada Lovelace", domain="acme.com")
    )
    assert res.status == NOT_FOUND
    assert res.email is None
    assert res.credits_charged == 0.0


def test_anymail_401_raises_errauth(monkeypatch):
    from emailfinder.providers import anymailfinder as mod

    _patch_http(monkeypatch, mod, _resp(401, {}))
    with pytest.raises(ErrAuth):
        AnymailFinderAdapter("bad").find(
            FindRequest(full_name="Ada Lovelace", domain="acme.com")
        )


def test_anymail_429_raises_ratelimited(monkeypatch):
    from emailfinder.providers import anymailfinder as mod

    _patch_http(monkeypatch, mod, _resp(429, {}, {"retry-after": "12"}))
    with pytest.raises(ErrRateLimited) as exc:
        AnymailFinderAdapter("key").find(
            FindRequest(full_name="Ada Lovelace", domain="acme.com")
        )
    assert exc.value.retry_after == 12.0


# --- MillionVerifier mapping ------------------------------------------------
@pytest.mark.parametrize("result,expected_status,expected_catch", [
    ("ok", Status.DELIVERABLE, False),
    ("invalid", Status.UNDELIVERABLE, False),
    ("catch_all", Status.RISKY, True),
    ("disposable", Status.RISKY, False),
    ("unknown", Status.UNKNOWN, False),
    ("unverified", Status.UNKNOWN, False),
])
def test_millionverifier_status_mapping(monkeypatch, result, expected_status, expected_catch):
    from emailfinder.providers import millionverifier as mod

    _patch_http(monkeypatch, mod, _resp(200, {"result": result, "quality": 80}))
    res = MillionVerifierAdapter("key").verify("ada@acme.com")
    assert res.status == expected_status
    assert res.is_catch_all == expected_catch
    if result == "invalid":
        assert res.reason == "mailbox_not_found"


# --- Hunter mapping ---------------------------------------------------------
def test_hunter_webmail_maps_to_unknown_with_flag(monkeypatch):
    from emailfinder.providers import hunter as mod

    _patch_http(monkeypatch, mod, _resp(200, {"data": {"result": "webmail"}}))
    res = HunterAdapter("key").verify("ada@gmail.com")
    assert res.status == Status.UNKNOWN
    assert res.webmail is True


def test_hunter_accept_all_maps_to_risky_catch_all(monkeypatch):
    from emailfinder.providers import hunter as mod

    _patch_http(monkeypatch, mod, _resp(200, {"data": {"result": "accept_all"}}))
    res = HunterAdapter("key").verify("ada@acme.com")
    assert res.status == Status.RISKY
    assert res.is_catch_all is True


def test_hunter_domain_pattern_translation(monkeypatch):
    from emailfinder.providers import hunter as mod

    _patch_http(monkeypatch, mod, _resp(200, {"data": {"pattern": "{first}.{last}"}}))
    assert HunterAdapter("key").domain_pattern("acme.com") == ("first.last", ".")

    _patch_http(monkeypatch, mod, _resp(200, {"data": {"pattern": "{f}{last}"}}))
    assert HunterAdapter("key").domain_pattern("acme.com") == ("flast", "")


def test_hunter_finder_valid(monkeypatch):
    from emailfinder.providers import hunter as mod

    _patch_http(monkeypatch, mod, _resp(200, {
        "data": {"email": "ada@acme.com", "score": 92,
                 "verification": {"status": "valid"}}
    }))
    res = HunterAdapter("key").find(FindRequest(full_name="Ada Lovelace", domain="acme.com"))
    assert res.status == FOUND_VERIFIED
    assert res.confidence == 92


# --- registry: routing ------------------------------------------------------
def _nonempty_registry():
    cache = FakeCache()
    state = _AdapterState(adapter=_CountingFinder(), cfg=ProviderConfig(
        name="anymailfinder", api_key_env="X", enabled=True))
    return ProviderRegistry(Config(), cache, finders=[state]), cache


class _CountingFinder(EmailFinder):
    def __init__(self, result=None):
        self.calls = 0
        self._result = result or ProviderFindResult(
            email="ada@acme.com", status=FOUND_VERIFIED, confidence=95,
            provider="anymailfinder", credits_charged=1.0,
        )

    def name(self) -> str:
        return "anymailfinder"

    def find(self, req: FindRequest) -> ProviderFindResult:
        self.calls += 1
        return self._result


def test_should_route_true_for_m365_catchall_and_no_pattern():
    reg, _ = _nonempty_registry()
    assert reg.should_route(Provider.MICROSOFT365, None, has_kb_pattern=True)
    assert reg.should_route(Provider.GOOGLE_WORKSPACE, True, has_kb_pattern=True)
    assert reg.should_route(Provider.GOOGLE_WORKSPACE, False, has_kb_pattern=False)


def test_should_route_false_for_probe_domain_with_kb_pattern():
    reg, _ = _nonempty_registry()
    assert reg.should_route(Provider.GOOGLE_WORKSPACE, False, has_kb_pattern=True) is False
    assert reg.should_route(Provider.PROOFPOINT, None, has_kb_pattern=True) is False


def test_empty_config_registry_is_inert():
    cfg = Config()  # enable_providers False, no providers
    reg = build_registry(cfg, FakeCache())
    assert reg.is_empty()
    assert reg.should_route(Provider.MICROSOFT365, True, has_kb_pattern=False) is False
    assert reg.find_with_fallback(
        FindRequest(full_name="Ada Lovelace", domain="acme.com"),
        Provider.MICROSOFT365, True,
    ) is None


# --- registry: cache-before-call (no double-charge) -------------------------
def test_registry_checks_cache_before_calling(monkeypatch):
    cache = FakeCache()
    finder = _CountingFinder()
    state = _AdapterState(adapter=finder, cfg=ProviderConfig(
        name="anymailfinder", api_key_env="X", enabled=True))
    reg = ProviderRegistry(Config(), cache, finders=[state])
    req = FindRequest(full_name="Ada Lovelace", domain="acme.com")

    first = reg.find_with_fallback(req, Provider.MICROSOFT365, True)
    assert first.status == FOUND_VERIFIED
    assert finder.calls == 1
    assert first.credits_charged == 1.0

    # Identical repeat must hit the cache: adapter NOT called again, no charge.
    second = reg.find_with_fallback(req, Provider.MICROSOFT365, True)
    assert second.status == FOUND_VERIFIED
    assert finder.calls == 1  # zero double-charge
    assert second.credits_charged == 0.0


def test_registry_short_circuits_on_found_verified():
    cache = FakeCache()
    hit = _CountingFinder()
    never = _CountingFinder()
    reg = ProviderRegistry(Config(), cache, finders=[
        _AdapterState(adapter=hit, cfg=ProviderConfig(name="anymailfinder", api_key_env="X", enabled=True, priority=1)),
        _AdapterState(adapter=never, cfg=ProviderConfig(name="hunter", api_key_env="Y", enabled=True, priority=2)),
    ])
    res = reg.find_with_fallback(
        FindRequest(full_name="Ada Lovelace", domain="acme.com"),
        Provider.MICROSOFT365, True,
    )
    assert res.status == FOUND_VERIFIED
    assert hit.calls == 1
    assert never.calls == 0  # short-circuited before the second finder


# --- registry: typed error handling -----------------------------------------
class _AuthFailFinder(EmailFinder):
    def __init__(self):
        self.calls = 0

    def name(self) -> str:
        return "anymailfinder"

    def find(self, req):
        self.calls += 1
        raise ErrAuth("bad key")


def test_registry_disables_adapter_on_errauth():
    cache = FakeCache()
    bad = _AuthFailFinder()
    good = _CountingFinder()
    reg = ProviderRegistry(Config(), cache, finders=[
        _AdapterState(adapter=bad, cfg=ProviderConfig(name="anymailfinder", api_key_env="X", enabled=True, priority=1)),
        _AdapterState(adapter=good, cfg=ProviderConfig(name="hunter", api_key_env="Y", enabled=True, priority=2)),
    ])
    req = FindRequest(full_name="Ada Lovelace", domain="acme.com")
    res = reg.find_with_fallback(req, Provider.MICROSOFT365, True)
    assert res.status == FOUND_VERIFIED  # fell through to the good finder
    assert good.calls == 1


# --- registry: budgets ------------------------------------------------------
def test_registry_respects_daily_credit_budget():
    cache = FakeCache()
    finder = _CountingFinder(ProviderFindResult(
        email=None, status=NOT_FOUND, provider="anymailfinder", credits_charged=1.0))
    state = _AdapterState(adapter=finder, cfg=ProviderConfig(
        name="anymailfinder", api_key_env="X", enabled=True, max_credits_per_day=1))
    reg = ProviderRegistry(Config(), cache, finders=[state])

    reg.find_with_fallback(FindRequest(full_name="A B", domain="a.com"), Provider.MICROSOFT365, True)
    assert finder.calls == 1
    # Budget now exhausted -> a DIFFERENT (uncached) request must not call again.
    reg.find_with_fallback(FindRequest(full_name="C D", domain="a.com"), Provider.MICROSOFT365, True)
    assert finder.calls == 1


# --- build_registry from config ---------------------------------------------
def test_build_registry_instantiates_enabled_adapter(monkeypatch):
    monkeypatch.setenv("MV_KEY", "secret")
    cfg = Config(enable_providers=True, providers=[
        ProviderConfig(name="millionverifier", api_key_env="MV_KEY", enabled=True, role="verifier"),
    ])
    reg = build_registry(cfg, FakeCache())
    assert not reg.is_empty()


def test_build_registry_skips_missing_api_key(monkeypatch):
    monkeypatch.delenv("MV_KEY", raising=False)
    cfg = Config(enable_providers=True, providers=[
        ProviderConfig(name="millionverifier", api_key_env="MV_KEY", enabled=True, role="verifier"),
    ])
    reg = build_registry(cfg, FakeCache())
    assert reg.is_empty()
