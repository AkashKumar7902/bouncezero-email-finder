"""Tests for emailfinder.dns_mx — MX/A/AAAA resolution + offline company guess.

Covers the contract's test-plan bullet:
  mock resolver — MX sorted asc pref; no-MX domain -> implicit A/AAAA
  is_implicit=True; NXDOMAIN -> error='dns_failure' (no raise). Optional live
  integration: resolve a real domain (DNS works in this env).
"""
from __future__ import annotations

import socket

import dns.exception
import dns.resolver
import pytest

from emailfinder import dns_mx
from emailfinder.config import load_config
from emailfinder.models import MXInfo


# --------------------------------------------------------------------------- #
# Fakes for a mocked resolver
# --------------------------------------------------------------------------- #
class _FakeMX:
    def __init__(self, preference: int, exchange: str):
        self.preference = preference
        self.exchange = exchange  # str(...) gives the hostname


class _FakeResolver:
    """Stand-in for dns.resolver.Resolver driven by a {(name, rrtype): result} map.

    A result may be a list of records or an exception class/instance to raise.
    """

    def __init__(self, records):
        self._records = records
        self.timeout = None
        self.lifetime = None

    def resolve(self, name, rrtype):
        key = (name, rrtype)
        if key not in self._records:
            raise dns.resolver.NXDOMAIN()
        result = self._records[key]
        if isinstance(result, type) and issubclass(result, BaseException):
            raise result()
        if isinstance(result, BaseException):
            raise result
        return result


def _patch_resolver(monkeypatch, records):
    monkeypatch.setattr(
        dns_mx, "_make_resolver", lambda timeout: _FakeResolver(records)
    )


# --------------------------------------------------------------------------- #
# resolve_mx
# --------------------------------------------------------------------------- #
def test_mx_sorted_ascending_by_preference(monkeypatch):
    _patch_resolver(
        monkeypatch,
        {
            ("acme.com", "MX"): [
                _FakeMX(30, "mx3.acme.com."),
                _FakeMX(10, "mx1.acme.com."),
                _FakeMX(20, "mx2.acme.com."),
            ]
        },
    )
    info = dns_mx.resolve_mx("acme.com")
    assert isinstance(info, MXInfo)
    assert info.error is None
    assert info.is_implicit is False
    # Lowest preference first (primary), trailing dot stripped.
    assert info.hosts == ["mx1.acme.com", "mx2.acme.com", "mx3.acme.com"]


def test_no_mx_falls_back_to_a_record_implicit(monkeypatch):
    _patch_resolver(
        monkeypatch,
        {
            ("noscmx.com", "MX"): dns.resolver.NoAnswer,
            ("noscmx.com", "A"): ["93.184.216.34"],
        },
    )
    info = dns_mx.resolve_mx("noscmx.com")
    assert info.error is None
    assert info.is_implicit is True
    assert info.hosts == ["noscmx.com"]


def test_no_mx_falls_back_to_aaaa_when_no_a(monkeypatch):
    _patch_resolver(
        monkeypatch,
        {
            ("v6.example", "MX"): dns.resolver.NoAnswer,
            ("v6.example", "A"): dns.resolver.NoAnswer,
            ("v6.example", "AAAA"): ["2606:2800:220:1::"],
        },
    )
    info = dns_mx.resolve_mx("v6.example")
    assert info.is_implicit is True
    assert info.hosts == ["v6.example"]


def test_nxdomain_returns_dns_failure_no_raise(monkeypatch):
    # No records at all -> every lookup NXDOMAINs.
    _patch_resolver(monkeypatch, {})
    info = dns_mx.resolve_mx("does-not-exist.invalid")
    assert info.error == "dns_failure"
    assert info.hosts == []
    assert info.is_implicit is False


def test_timeout_is_dns_timeout_never_valid(monkeypatch):
    # A transient DNS timeout must be surfaced as 'dns_timeout' (an UNKNOWN
    # signal), NOT 'dns_failure' (permanent UNDELIVERABLE) — a momentary
    # resolver hiccup must never permanently condemn a real domain.
    _patch_resolver(
        monkeypatch,
        {
            ("slow.com", "MX"): dns.exception.Timeout,
            ("slow.com", "A"): dns.exception.Timeout,
            ("slow.com", "AAAA"): dns.exception.Timeout,
        },
    )
    info = dns_mx.resolve_mx("slow.com")
    assert info.error == "dns_timeout"
    assert info.hosts == []


def test_null_mx_rfc7505_yields_no_host(monkeypatch):
    # A single "." exchange = null MX; with no A/AAAA it's a dns_failure.
    _patch_resolver(
        monkeypatch,
        {
            ("nomail.com", "MX"): [_FakeMX(0, ".")],
            ("nomail.com", "A"): dns.resolver.NoAnswer,
            ("nomail.com", "AAAA"): dns.resolver.NoAnswer,
        },
    )
    info = dns_mx.resolve_mx("nomail.com")
    assert info.error == "dns_failure"


def test_domain_is_normalized(monkeypatch):
    _patch_resolver(
        monkeypatch, {("acme.com", "MX"): [_FakeMX(10, "mx.acme.com.")]}
    )
    info = dns_mx.resolve_mx("  ACME.com.  ")
    assert info.domain == "acme.com"
    assert info.hosts == ["mx.acme.com"]


def test_empty_domain_is_dns_failure(monkeypatch):
    _patch_resolver(monkeypatch, {})
    info = dns_mx.resolve_mx("")
    assert info.error == "dns_failure"


# --------------------------------------------------------------------------- #
# resolve_domain_for_company (offline slug + live-MX confirm)
# --------------------------------------------------------------------------- #
def _cfg():
    return load_config()


def test_company_single_resolving_tld_accepted(monkeypatch):
    resolving = {"acme.com"}

    def fake_resolve_mx(domain, timeout=5.0):
        if domain in resolving:
            return MXInfo(domain=domain, hosts=["mx." + domain], error=None)
        return MXInfo(domain=domain, hosts=[], error="dns_failure")

    monkeypatch.setattr(dns_mx, "resolve_mx", fake_resolve_mx)
    assert dns_mx.resolve_domain_for_company("Acme Technologies, Inc.", _cfg()) == "acme.com"


def test_company_multiple_tlds_prefers_com(monkeypatch):
    # When several TLDs resolve (a brand defensively registers .com + .in), the
    # first in preference order (.com) is chosen rather than declaring ambiguity.
    resolving = {"acme.com", "acme.in"}

    def fake_resolve_mx(domain, timeout=5.0):
        if domain in resolving:
            return MXInfo(domain=domain, hosts=["mx." + domain], error=None)
        return MXInfo(domain=domain, hosts=[], error="dns_failure")

    monkeypatch.setattr(dns_mx, "resolve_mx", fake_resolve_mx)
    assert dns_mx.resolve_domain_for_company("Acme", _cfg()) == "acme.com"


def test_company_none_resolving_returns_none(monkeypatch):
    def fake_resolve_mx(domain, timeout=5.0):
        return MXInfo(domain=domain, hosts=[], error="dns_failure")

    monkeypatch.setattr(dns_mx, "resolve_mx", fake_resolve_mx)
    assert dns_mx.resolve_domain_for_company("Nonexistent Widgets", _cfg()) is None


def test_company_empty_returns_none():
    assert dns_mx.resolve_domain_for_company("", _cfg()) is None
    assert dns_mx.resolve_domain_for_company("   ", _cfg()) is None


def test_slug_strips_accents_and_stopwords(monkeypatch):
    seen = []

    def fake_resolve_mx(domain, timeout=5.0):
        seen.append(domain)
        return MXInfo(domain=domain, hosts=[], error="dns_failure")

    monkeypatch.setattr(dns_mx, "resolve_mx", fake_resolve_mx)
    dns_mx.resolve_domain_for_company("Zünd Systems Pvt Ltd", _cfg())
    # 'systems','pvt','ltd' dropped, ü -> u; tried across the ordered TLDs.
    assert seen == ["zund.com", "zund.in", "zund.io"]


def test_slug_all_stopwords_falls_back_to_raw(monkeypatch):
    seen = []

    def fake_resolve_mx(domain, timeout=5.0):
        seen.append(domain)
        return MXInfo(domain=domain, hosts=[], error="dns_failure")

    monkeypatch.setattr(dns_mx, "resolve_mx", fake_resolve_mx)
    dns_mx.resolve_domain_for_company("The Company", _cfg())
    assert seen == ["thecompany.com", "thecompany.in", "thecompany.io"]


# --------------------------------------------------------------------------- #
# Optional live integration (DNS works in this env)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_live_resolve_real_domain():
    try:
        info = dns_mx.resolve_mx("google.com", timeout=5.0)
    except Exception:  # pragma: no cover - environment dependent
        pytest.skip("no live DNS")
    if info.error == "dns_failure":
        pytest.skip("live DNS unavailable in this env")
    assert info.hosts
    assert info.error is None
