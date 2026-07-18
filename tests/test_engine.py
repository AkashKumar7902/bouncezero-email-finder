"""End-to-end engine tests (offline: DNS mocked, SMTP + providers off).

Covers the engine.py test-plan bullet plus the cross-cutting safety invariants:
  * KB-driven separator (underscore) and dot pattern (first.last);
  * compliance suppression short-circuits to suppressed=True with no candidates;
  * provenance is written once per find;
  * LinkedIn URLs are only slug-parsed locally (no network) and still drive a find;
  * a dns_failure domain -> UNDELIVERABLE (never a resolvable guess);
  * M365 / catch-all can NEVER be reported DELIVERABLE;
  * find_batch resolves MX once per distinct domain across the batch.

The KB is a SYNTHETIC fixture (fake domains + names) injected into the engine —
the shipped package carries no real audit data.
"""
from __future__ import annotations

import copy

import pytest

from emailfinder import dns_mx
from emailfinder.models import MXInfo, Status


def _mx(domain, hosts, error=None):
    return MXInfo(domain=domain, hosts=list(hosts), is_implicit=False, error=error)


@pytest.fixture(autouse=True)
def _load_sample_kb(engine, sample_kb):
    """Inject the synthetic KB into the shared engine (deep-copied so per-test
    upserts never leak across tests)."""
    engine.kb.update(copy.deepcopy(sample_kb))
    return engine


@pytest.fixture
def mock_dns(monkeypatch):
    """Route resolve_mx through a domain->hosts table with a call counter."""
    table = {
        "underscore.example": ["aspmx.l.google.com", "alt1.aspmx.l.google.com"],
        "acme.example": ["aspmx.l.google.com"],
        "widgets.example": ["widgets-example.mail.protection.outlook.com"],
    }
    calls = {"n": 0, "by_domain": {}}

    def fake_resolve_mx(domain, timeout=5.0):
        calls["n"] += 1
        calls["by_domain"][domain] = calls["by_domain"].get(domain, 0) + 1
        hosts = table.get(domain)
        if hosts is None:
            return _mx(domain, [], error="dns_failure")
        return _mx(domain, hosts)

    monkeypatch.setattr(dns_mx, "resolve_mx", fake_resolve_mx)
    return calls


# --------------------------------------------------------------------------- #
# KB-driven pattern + separator
# --------------------------------------------------------------------------- #
def test_kb_underscore_separator(engine, mock_dns):
    result = engine.find("Ajith Kumar", "underscore.example")
    assert result.best is not None
    assert result.domain == "underscore.example"
    # underscore.example's KB dominant shape is first_last with a "_" separator.
    assert result.best.candidate.separator == "_"
    assert result.best.candidate.local_part == "ajith_kumar"
    assert result.best.candidate.source == "kb"
    assert result.best_email() == "ajith_kumar@underscore.example"


def test_first_last_dot_high_pattern(engine, mock_dns):
    # A clean name not banked in acme's known_bad_locals.
    result = engine.find("Rahul Verma", "acme.example")
    assert result.best is not None
    assert result.best.candidate.separator == "."
    assert result.best.candidate.local_part == "rahul.verma"
    # KB dominant pattern match should score in the strong pattern band.
    assert result.best.score >= 70
    assert result.best.reasons  # a 'why this guess' trail is always populated


def test_known_bad_forces_undeliverable(engine, mock_dns):
    # 'wrong.guess' is banked in acme's known_bad_locals.
    result = engine.find("Wrong Guess", "acme.example")
    banked = [
        sc
        for sc in ([result.best] + result.alternates)
        if sc.candidate.local_part == "wrong.guess"
    ]
    assert banked, "expected the banked local to appear among candidates"
    assert banked[0].status == Status.UNDELIVERABLE


# --------------------------------------------------------------------------- #
# compliance gate
# --------------------------------------------------------------------------- #
def test_suppressed_identity_returns_no_candidate(engine, mock_dns):
    engine.compliance.add_suppression(None, "Jane Doe", "acme.example", "test-optout")
    result = engine.find("Jane Doe", "acme.example")
    assert result.suppressed is True
    assert result.best is None
    assert result.provenance_id is None  # nothing processed


def test_provenance_written_once(engine, mock_dns):
    before = _count_lines(engine.compliance.provenance_path)
    result = engine.find("Ajith Kumar", "underscore.example")
    after = _count_lines(engine.compliance.provenance_path)
    assert after == before + 1
    assert result.provenance_id is not None


# --------------------------------------------------------------------------- #
# LinkedIn slug is local-only
# --------------------------------------------------------------------------- #
def test_linkedin_url_slug_parsed_locally(engine, mock_dns, monkeypatch):
    # Any attempt to open a network socket would blow up this test.
    import socket

    def _no_net(*a, **k):  # pragma: no cover - only fires on a violation
        raise AssertionError("engine performed network I/O for a LinkedIn URL")

    monkeypatch.setattr(socket, "create_connection", _no_net)

    result = engine.find(
        None,
        "underscore.example",
        linkedin_url="https://www.linkedin.com/in/ajith-kumar-c-12ab34/",
    )
    assert result.best is not None
    # Slug 'ajith-kumar-c-12ab34' -> name 'ajith kumar c'.
    assert result.query["linkedin_slug"] == "ajith-kumar-c-12ab34"
    assert result.query["name"] == "ajith kumar c"
    assert result.best_email().endswith("@underscore.example")


# --------------------------------------------------------------------------- #
# safety invariants
# --------------------------------------------------------------------------- #
def test_dns_failure_is_undeliverable(engine, mock_dns):
    result = engine.find("Some One", "no-such-domain-xyz.example")
    assert result.mx is not None and result.mx.error == "dns_failure"
    assert result.best is not None
    assert result.best.status == Status.UNDELIVERABLE
    assert result.best.score == 0


def test_m365_never_deliverable(engine, mock_dns):
    result = engine.find("Rahul Verma", "widgets.example", verify=True)
    assert result.provider.value == "microsoft365"
    for sc in [result.best] + result.alternates:
        assert sc.status != Status.DELIVERABLE
    assert result.best.score <= engine.cfg.score.m365_cap


def test_verify_unavailable_never_marks_invalid(engine, mock_dns, monkeypatch):
    # Force the port-25 pre-flight to report blocked (this env blocks :25 anyway).
    from emailfinder import smtp_probe

    monkeypatch.setattr(smtp_probe, "port25_open", lambda *a, **k: False)
    result = engine.find("Ajith Kumar", "underscore.example", verify=True)
    assert result.verification_mode in ("none", "verification_unavailable")
    assert result.best.status != Status.UNDELIVERABLE  # pattern-only, not invalid


# --------------------------------------------------------------------------- #
# batch: fingerprint-once per distinct domain
# --------------------------------------------------------------------------- #
def test_find_batch_resolves_mx_once_per_domain(engine, mock_dns):
    rows = [
        {"name": "Ajith Kumar", "domain": "underscore.example"},
        {"name": "Rahul Verma", "domain": "underscore.example"},
        {"name": "Priya Nair", "domain": "acme.example"},
    ]
    results = list(engine.find_batch(rows))
    assert len(results) == 3
    # MX resolved once per DISTINCT domain (cache-backed fingerprint-once).
    assert mock_dns["by_domain"]["underscore.example"] == 1
    assert mock_dns["by_domain"]["acme.example"] == 1
    # Input order preserved.
    assert [r.query["name"] for r in results] == [
        "Ajith Kumar",
        "Rahul Verma",
        "Priya Nair",
    ]


# --------------------------------------------------------------------------- #
# feedback hooks
# --------------------------------------------------------------------------- #
def test_confirm_banks_known_bad_then_undeliverable(engine, mock_dns):
    engine.confirm("madeup.local@acme.example", "acme.example", deliverable=False)
    result = engine.find("Madeup Local", "acme.example")
    hit = [
        sc
        for sc in ([result.best] + result.alternates)
        if sc.candidate.local_part == "madeup.local"
    ]
    assert hit and hit[0].status == Status.UNDELIVERABLE


def _count_lines(path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.open("r", encoding="utf-8"))
