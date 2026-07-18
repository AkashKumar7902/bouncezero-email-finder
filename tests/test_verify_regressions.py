"""Regression tests for the defects found by the Phase-6 verification pass.

Each test pins a specific fixed behavior so it cannot silently regress.
"""
from __future__ import annotations

from emailfinder.models import NameVariant, Provider


# --- Finding 1: South-Indian first + initial must render candidates -------- #
def test_first_initial_renders_via_templates():
    from emailfinder.templates import render

    v = NameVariant(first="ashwath", last=None, initials=["s"], origin="first_initial")
    assert render("first.last", v, ".") == "ashwath.s"
    assert render("first.l", v, ".") == "ashwath.s"
    assert render("firstlast", v, "") == "ashwaths"
    assert render("first", v, "") == "ashwath"
    # surname-leading templates must NOT fabricate junk for an initial-only variant
    assert render("flast", v, "") is None
    assert render("last.first", v, ".") is None


def test_first_initial_names_expand_and_generate():
    from emailfinder import names
    from emailfinder.candidates import generate_from_kb

    pn = names.parse_name("Ashwath S")
    variants = names.expand_variants(pn, {})
    cands = generate_from_kb(variants, "first.last", ".", [("first.l", ".")])
    locals_ = {c.local_part for c in cands}
    assert "ashwath.s" in locals_


# --- Finding 2: a KB fallback pick is labelled 'fallback', not 'dominant' -- #
def test_kb_fallback_reason_label():
    from emailfinder.config import ScoreConfig
    from emailfinder.models import Candidate, VerifyStrategy
    from emailfinder.scoring import score_candidate

    fallback = Candidate(local_part="a.b", template="first.l", separator=".",
                         shape="first.l", prior=0.5, source="kb")
    sc = score_candidate(fallback, Provider.PROOFPOINT, VerifyStrategy.PROBE,
                         None, None, {"syntax_ok": True, "mx_ok": True},
                         ScoreConfig(), kb_match=True)
    assert any("fallback pattern" in r for r in sc.reasons)
    assert not any("dominant pattern" in r for r in sc.reasons)


# --- Finding 9: a full/over-quota mailbox (5.2.2) is NOT banked ------------ #
def test_mailbox_full_is_transient_not_wrong_guess():
    from emailfinder.rescore import _verdict_from_enhanced
    assert _verdict_from_enhanced("5.2.2") == "TRANSIENT"   # full -> not banked
    assert _verdict_from_enhanced("5.2.3") == "TRANSIENT"   # too large -> not banked
    assert _verdict_from_enhanced("5.2.1") == "WRONG_GUESS"  # disabled -> bankable
    assert _verdict_from_enhanced("5.1.1") == "WRONG_GUESS"  # no such user


# --- Finding 14: a compound reason_class maps to its most-severe verdict --- #
def test_compound_reason_class():
    from emailfinder.models import BounceRow
    from emailfinder.rescore import classify_bounce

    row = BounceRow(raw={}, email="a@b.com", local="a", domain="b.com",
                    reason_class="connection_failure; temporary_failure")
    assert classify_bounce(row, None) == "DOMAIN_ISSUE"  # most severe of the two


# --- Finding 15: an email opt-out filters the generated address ------------ #
def test_email_optout_filters_generated_address(config):
    from emailfinder.engine import Engine
    from emailfinder.models import DomainFingerprint

    eng = Engine(config)
    # Inject a fake domain fingerprint so the test is offline + deterministic.
    eng.cache.put_domain(DomainFingerprint(
        domain="acme.example", provider=Provider.GOOGLE_WORKSPACE,
        mx=["aspmx.l.google.com"], is_catch_all=None,
        flags={"is_implicit": False, "dns_error": None}))
    eng.compliance.add_suppression("rahul.verma@acme.example", None, None, "test")
    r = eng.find("Rahul Verma", "acme.example")
    # the opted-out address is never returned as best or an alternate
    all_emails = {sc.candidate.local_part + "@acme.example"
                  for sc in ([r.best] + r.alternates) if sc is not None}
    assert "rahul.verma@acme.example" not in all_emails
    eng.close()


# --- Finding 16: identity opt-out blocks a --company lookup ---------------- #
def test_identity_optout_blocks_company_lookup(config, monkeypatch):
    from emailfinder import dns_mx
    from emailfinder.engine import Engine

    # Resolve the company to a fake domain (offline) so we test the gate, not DNS.
    monkeypatch.setattr(dns_mx, "resolve_domain_for_company",
                        lambda company, cfg: "acme.example")
    eng = Engine(config)
    eng.compliance.add_suppression(None, "Priya Sharma", "acme.example", "test")
    r = eng.find("Priya Sharma", company="Acme")
    assert r.suppressed is True
    assert r.best is None
    eng.close()
