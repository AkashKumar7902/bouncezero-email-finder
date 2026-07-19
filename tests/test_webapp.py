"""Tests for the hosted public web app (webapp/).

Additive: these drive the NEW web surface only and never touch the pure core's
behavior. Everything runs offline — ``emailfinder.dns_mx.resolve_mx`` is
monkeypatched so no real DNS is issued (the service reaches DNS via the
``dns_mx`` module attribute, so patching it there is sufficient).

Coverage (per webapp/CONTRACT.md):
  * a KB-seeded domain uses the learned pattern;
  * an M365 domain is capped and never DELIVERABLE;
  * a dns_failure domain -> UNDELIVERABLE;
  * opt-out then find is suppressed; an opted-out ADDRESS is filtered out;
  * the per-IP rate limiter returns 429 after the cap;
  * /healthz is ok; the served page has NO external http(s) URLs;
  * a PgStore smoke test (integration; skipped unless DATABASE_URL is set).
"""
from __future__ import annotations

import os

import pytest

from emailfinder import dns_mx
from emailfinder.models import MXInfo, Provider, Status
from webapp.service import HostedFinder
from webapp.store import MemoryStore


# --------------------------------------------------------------------------- #
# offline DNS
# --------------------------------------------------------------------------- #
def _mx(domain, hosts, error=None):
    return MXInfo(domain=domain, hosts=list(hosts), is_implicit=False, error=error)


# Synthetic MX table: fake domains, deterministic providers, no PII.
_DNS_TABLE = {
    "seeded.example": ["aspmx.l.google.com", "alt1.aspmx.l.google.com"],
    "acme.com": ["aspmx.l.google.com"],
    "widgets.example": ["widgets-example.mail.protection.outlook.com"],
}


@pytest.fixture
def mock_dns(monkeypatch):
    """Patch ``dns_mx.resolve_mx`` so tests never hit the network.

    Any domain not in the table resolves to a permanent ``dns_failure`` (empty
    host list) — the UNDELIVERABLE path.
    """

    def fake_resolve_mx(domain, timeout=5.0):
        hosts = _DNS_TABLE.get(domain)
        if hosts is None:
            return _mx(domain, [], error="dns_failure")
        return _mx(domain, hosts)

    monkeypatch.setattr(dns_mx, "resolve_mx", fake_resolve_mx)
    return _DNS_TABLE


@pytest.fixture
def finder(mock_dns):
    """A fresh HostedFinder over an empty in-memory store (offline DNS)."""
    return HostedFinder(MemoryStore())


def _seed_pattern(store, domain, provider_value, locals_):
    """Seed the KB so ``domain`` learns a dominant pattern from ``locals_``."""
    for local in locals_:
        store.upsert_verified(domain, "", "", provider_value, local)


# --------------------------------------------------------------------------- #
# KB-seeded domain uses the learned pattern
# --------------------------------------------------------------------------- #
def test_kb_seeded_domain_uses_learned_pattern(mock_dns):
    store = MemoryStore()
    # Three verified first_last (underscore) locals -> dominant shape first_last.
    _seed_pattern(
        store,
        "seeded.example",
        Provider.GOOGLE_WORKSPACE.value,
        ["bharat_singh", "chetan_kumar", "deepak_rao"],
    )
    entry = store.get_kb_entry("seeded.example")
    assert entry["dominant_shape"] == "first_last"
    assert entry["dominant_separator"] == "_"

    finder = HostedFinder(store)
    result = finder.find("Ajith Kumar", "seeded.example")

    assert result["suppressed"] is False
    assert result["domain"] == "seeded.example"
    assert result["provider"] == Provider.GOOGLE_WORKSPACE.value
    best = result["best"]
    assert best is not None
    # The learned underscore pattern drives the top guess.
    assert best["email"] == "ajith_kumar@seeded.example"
    assert best["separator"] == "_"
    # The KB match is surfaced in the reasons trail.
    assert best["reasons"]


# --------------------------------------------------------------------------- #
# M365 domain: capped, never DELIVERABLE
# --------------------------------------------------------------------------- #
def test_m365_domain_capped_never_deliverable(finder):
    result = finder.find("Rahul Verma", "widgets.example")
    assert result["provider"] == Provider.MICROSOFT365.value
    best = result["best"]
    assert best is not None
    assert best["status"] != Status.DELIVERABLE.value
    assert best["cap_note"] == "capped: Microsoft 365 not RCPT-verifiable"
    assert best["score"] <= finder.cfg.score.m365_cap


def test_m365_kb_override_keeps_caps(mock_dns):
    """A KB that records M365 keeps the caps even when live MX looks like Google."""
    store = MemoryStore()
    # KB says M365; live DNS for acme.com classifies as Google Workspace.
    _seed_pattern(store, "acme.com", Provider.MICROSOFT365.value, ["a_one", "b_two"])
    finder = HostedFinder(store)
    result = finder.find("Jane Doe", "acme.com")
    assert result["provider"] == Provider.MICROSOFT365.value
    assert result["best"]["status"] != Status.DELIVERABLE.value
    assert result["best"]["cap_note"] == "capped: Microsoft 365 not RCPT-verifiable"


# --------------------------------------------------------------------------- #
# dns_failure -> UNDELIVERABLE
# --------------------------------------------------------------------------- #
def test_dns_failure_is_undeliverable(finder):
    result = finder.find("Some One", "no-such-domain-xyz.example")
    best = result["best"]
    assert best is not None
    assert best["status"] == Status.UNDELIVERABLE.value


# --------------------------------------------------------------------------- #
# no domain -> note, no candidates
# --------------------------------------------------------------------------- #
def test_no_domain_returns_note(finder):
    result = finder.find("Some One")
    assert result["best"] is None
    assert result["notes"] == ["no domain: pass a domain or a resolvable company"]


# --------------------------------------------------------------------------- #
# opt-out -> suppressed find / filtered address
# --------------------------------------------------------------------------- #
def test_optout_identity_then_find_is_suppressed(finder):
    finder.optout(name="Jane Doe", domain="acme.com")
    result = finder.find("Jane Doe", "acme.com")
    assert result["suppressed"] is True
    assert result["best"] is None


def test_optout_address_is_filtered(mock_dns):
    store = MemoryStore()
    _seed_pattern(
        store,
        "seeded.example",
        Provider.GOOGLE_WORKSPACE.value,
        ["bharat_singh", "chetan_kumar", "deepak_rao"],
    )
    finder = HostedFinder(store)
    # Confirm the address we are about to suppress is the top guess.
    before = finder.find("Ajith Kumar", "seeded.example")
    assert before["best"]["email"] == "ajith_kumar@seeded.example"

    finder.optout(email="ajith_kumar@seeded.example")
    after = finder.find("Ajith Kumar", "seeded.example")
    generated = []
    if after["best"]:
        generated.append(after["best"]["email"])
    generated += [a["email"] for a in after["alternates"]]
    assert "ajith_kumar@seeded.example" not in generated


# --------------------------------------------------------------------------- #
# KB read passthrough
# --------------------------------------------------------------------------- #
def test_kb_entry_passthrough(mock_dns):
    store = MemoryStore()
    _seed_pattern(store, "seeded.example", Provider.GOOGLE_WORKSPACE.value, ["a_b"])
    finder = HostedFinder(store)
    assert finder.kb_entry("SEEDED.EXAMPLE") is not None
    assert finder.kb_entry("unknown.example") is None


# --------------------------------------------------------------------------- #
# FastAPI app: TestClient
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch):
    """A TestClient over the real app with offline DNS and a clean rate window."""
    from fastapi.testclient import TestClient

    from webapp import app as app_module

    def fake_resolve_mx(domain, timeout=5.0):
        hosts = _DNS_TABLE.get(domain)
        if hosts is None:
            return _mx(domain, [], error="dns_failure")
        return _mx(domain, hosts)

    monkeypatch.setattr(dns_mx, "resolve_mx", fake_resolve_mx)
    # Isolate each test from the shared in-memory rate-limit window.
    app_module._hits.clear()
    return TestClient(app_module.app)


def test_healthz_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_page_has_no_external_urls(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    html = resp.text
    assert "http://" not in html
    assert "https://" not in html


def test_api_find_endpoint(client):
    resp = client.post(
        "/api/find", json={"name": "Rahul Verma", "domain": "widgets.example"}
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["provider"] == Provider.MICROSOFT365.value
    assert payload["best"]["cap_note"] == "capped: Microsoft 365 not RCPT-verifiable"


def test_api_optout_endpoint(client):
    resp = client.post("/api/optout", json={"name": "Jane Doe", "domain": "acme.com"})
    assert resp.status_code == 204
    # A subsequent find for that identity is suppressed.
    found = client.post("/api/find", json={"name": "Jane Doe", "domain": "acme.com"})
    assert found.status_code == 200
    assert found.json()["suppressed"] is True


def test_api_optout_requires_identity(client):
    resp = client.post("/api/optout", json={})
    assert resp.status_code == 400


def test_rate_limiter_returns_429(client, monkeypatch):
    from webapp import app as app_module

    monkeypatch.setattr(app_module, "RATE_PER_MIN", 3)
    app_module._hits.clear()
    # First 3 requests pass, the 4th trips the per-minute cap.
    for _ in range(3):
        assert client.get("/").status_code == 200
    tripped = client.get("/")
    assert tripped.status_code == 429
    assert tripped.json()["error"] == "rate_limited"


def test_healthz_exempt_from_rate_limit(client, monkeypatch):
    from webapp import app as app_module

    monkeypatch.setattr(app_module, "RATE_PER_MIN", 2)
    app_module._hits.clear()
    # Health checks never count toward or trip the limiter.
    for _ in range(6):
        assert client.get("/healthz").status_code == 200


# --------------------------------------------------------------------------- #
# PgStore smoke test (integration — needs a live Postgres)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping Postgres integration smoke test",
)
def test_pgstore_smoke_roundtrip():
    from webapp.store_pg import PgStore, init_schema

    dsn = os.environ["DATABASE_URL"]
    init_schema(dsn)
    store = PgStore(dsn)
    try:
        store.upsert_verified(
            "pgsmoke.example", "", "", Provider.GOOGLE_WORKSPACE.value, "jane_doe"
        )
        entry = store.get_kb_entry("pgsmoke.example")
        assert entry is not None
        assert "jane_doe" in entry["no_bounce_locals"]

        store.add_suppression("opt@pgsmoke.example", None, None, "test")
        assert store.is_suppressed("opt@pgsmoke.example", None, None) is True
        assert "opt@pgsmoke.example" in store.suppression_emails()
    finally:
        store.close()
