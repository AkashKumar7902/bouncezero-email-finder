"""Tests for the headline re-scorer (emailfinder/rescore.py).

Covers the rescore.py test-plan bullet:
  * GOLDEN over the real audit records.csv: address_not_found -> WRONG_GUESS,
    M365 5.4.1/recipient_rejected -> PROBABLE_INVALID_M365 (banked, not discarded),
    5.7.x / policy_or_spam -> SENDER_SIDE (untouched), routing_loop (loop.example) ->
    DOMAIN_ISSUE;
  * banking a wrong guess into known_bad_locals, a corrected candidate via
    engine.find, and the accuracy-compounding invariant (a re-find of a banked
    local now returns UNDELIVERABLE);
  * provider-aware refinement (5.4.1 banks only on M365, downgrades elsewhere);
  * CSV auto-detection + non-bounce filtering, DSN mailbox parity, fix-list I/O.
"""
from __future__ import annotations

import collections
import json

import pytest
import copy

from emailfinder import dns_mx, kb_store, rescore
from emailfinder.models import BounceRow, MXInfo, Provider, Status


# --------------------------------------------------------------------------- #
# DNS mock (offline) shared with the engine-driven tests
# --------------------------------------------------------------------------- #
def _mx(domain, hosts, error=None):
    return MXInfo(domain=domain, hosts=list(hosts), is_implicit=False, error=error)


@pytest.fixture
def mock_dns(monkeypatch):
    table = {
        "underscore.example": ["aspmx.l.google.com", "alt1.aspmx.l.google.com"],
        "loop.example": ["aspmx.l.google.com"],
        "acme.example": ["mx1.hc5016-32.iphmx.com"],
    }

    def fake_resolve_mx(domain, timeout=5.0):
        hosts = table.get(domain)
        if hosts is None:
            return _mx(domain, [], error="dns_failure")
        return _mx(domain, hosts)

    monkeypatch.setattr(dns_mx, "resolve_mx", fake_resolve_mx)
    return table


def _write_csv(path, header, rows):
    import csv

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


@pytest.fixture(autouse=True)
def _load_sample_kb(engine, sample_kb):
    """Inject the synthetic KB into the engine used by the rescore tests (the
    shipped package ships an empty seed)."""
    engine.kb.update(copy.deepcopy(sample_kb))
    return engine


# --------------------------------------------------------------------------- #
# ENHANCED_CODE_MAP + classify_bounce
# --------------------------------------------------------------------------- #
def test_enhanced_code_map_core_entries():
    m = rescore.ENHANCED_CODE_MAP
    assert m["5.1.1"] == "WRONG_GUESS"
    assert m["5.1.10"] == "WRONG_GUESS"
    assert m["address_not_found"] == "WRONG_GUESS"
    assert m["5.4.1"] == "PROBABLE_INVALID_M365"
    assert m["recipient_rejected"] == "PROBABLE_INVALID_M365"
    assert m["policy_or_spam_rejection"] == "SENDER_SIDE"
    assert m["routing_loop"] == "DOMAIN_ISSUE"
    assert m["dns_failure"] == "DOMAIN_ISSUE"


def _row(local="a.b", domain="x.com", enhanced=None, reason=None, code=None):
    return BounceRow(
        raw={},
        email=f"{local}@{domain}",
        local=local,
        domain=domain,
        smtp_code=code,
        enhanced=enhanced,
        reason_class=reason,
        provider_hint=None,
    )


def test_classify_address_not_found_is_wrong_guess():
    assert rescore.classify_bounce(_row(reason="address_not_found"), None) == "WRONG_GUESS"
    assert rescore.classify_bounce(_row(enhanced="5.1.1"), None) == "WRONG_GUESS"
    assert rescore.classify_bounce(_row(enhanced="5.1.10"), None) == "WRONG_GUESS"


def test_classify_m365_5_4_1_banks_only_on_m365():
    # DBEB directory-not-found on M365 -> probable-invalid (bankable).
    assert (
        rescore.classify_bounce(_row(reason="recipient_rejected"), Provider.MICROSOFT365)
        == "PROBABLE_INVALID_M365"
    )
    # Same signal on a non-M365 provider is an ambiguous policy block -> not banked.
    assert (
        rescore.classify_bounce(_row(reason="recipient_rejected"), Provider.GOOGLE_WORKSPACE)
        == "SENDER_SIDE"
    )
    # Unknown provider keeps the audit's dominant (M365) reading -> banked.
    assert (
        rescore.classify_bounce(_row(reason="recipient_rejected"), None)
        == "PROBABLE_INVALID_M365"
    )


def test_classify_sender_side_and_transient_and_domain():
    assert rescore.classify_bounce(_row(enhanced="5.7.1"), None) == "SENDER_SIDE"
    assert (
        rescore.classify_bounce(_row(reason="policy_or_spam_rejection"), None)
        == "SENDER_SIDE"
    )
    assert rescore.classify_bounce(_row(enhanced="4.7.1"), None) == "TRANSIENT"
    assert rescore.classify_bounce(_row(reason="routing_loop"), None) == "DOMAIN_ISSUE"
    assert rescore.classify_bounce(_row(reason="dns_failure"), None) == "DOMAIN_ISSUE"


def test_classify_timeout_or_4xx_never_invalid():
    # A bare transient reply code must never become a WRONG_GUESS.
    assert rescore.classify_bounce(_row(code=451), None) == "TRANSIENT"
    assert rescore.classify_bounce(_row(), None) == "UNKNOWN"  # no signal at all


# --------------------------------------------------------------------------- #
# GOLDEN: classify the real audit corpus and match the dossier ground truth
# --------------------------------------------------------------------------- #
def test_golden_verdict_distribution(sample_bounces_path, sample_kb):
    # Synthetic bounce corpus (no real PII) exercising the exact bucketing rules.
    rows = rescore.parse_bounce_csv(sample_bounces_path)
    # The 4 "No bounce found" rows must be filtered out -> 9 real bounces remain.
    assert len(rows) == 9

    def prov(dom):
        e = sample_kb.get(dom)
        if not e or not e.get("provider"):
            return None
        try:
            return Provider(e["provider"])
        except ValueError:
            return None

    ctr = collections.Counter()
    by_reason = collections.defaultdict(collections.Counter)
    for r in rows:
        v = rescore.classify_bounce(r, prov(r.domain))
        ctr[v] += 1
        by_reason[r.reason_class][v] += 1

    # address_not_found is always a wrong guess (the #1 real-world bounce cause).
    assert by_reason["address_not_found"]["WRONG_GUESS"] == 3
    # recipient_rejected is directory-not-found (bankable) ONLY on Microsoft 365;
    # on any other provider it is an ambiguous policy signal -> SENDER_SIDE.
    assert by_reason["recipient_rejected"]["PROBABLE_INVALID_M365"] == 2   # widgets = M365
    assert by_reason["recipient_rejected"]["SENDER_SIDE"] == 1             # acme = google
    assert by_reason["policy_or_spam_rejection"]["SENDER_SIDE"] == 1
    # routing_loop is a domain-wide problem, never a wrong guess.
    assert by_reason["routing_loop"]["DOMAIN_ISSUE"] == 2

    loop = [r for r in rows if r.domain == "loop.example"]
    assert loop
    assert all(
        rescore.classify_bounce(r, prov("loop.example")) == "DOMAIN_ISSUE" for r in loop
    )


# --------------------------------------------------------------------------- #
# parse_bounce_csv: auto-detection + filtering + generic form
# --------------------------------------------------------------------------- #
def test_parse_drops_non_bounce_rows(tmp_path):
    p = _write_csv(
        tmp_path / "b.csv",
        ["company", "email", "domain", "local", "shape", "sep", "bounce_status", "reason_class"],
        [
            ["Acme", "a.b@acme.com", "acme.com", "a.b", "first.last", ".", "Bounced", "address_not_found"],
            ["Acme", "c.d@acme.com", "acme.com", "c.d", "first.last", ".", "No bounce found", ""],
        ],
    )
    rows = rescore.parse_bounce_csv(p)
    assert len(rows) == 1
    assert rows[0].email == "a.b@acme.com"
    assert rows[0].reason_class == "address_not_found"


def test_parse_generic_email_plus_code_csv(tmp_path):
    p = _write_csv(
        tmp_path / "g.csv",
        ["address", "smtp_code", "enhanced_code"],
        [["jane.doe@foo.io", "550", "5.1.1"]],
    )
    rows = rescore.parse_bounce_csv(p)
    assert len(rows) == 1
    r = rows[0]
    assert r.email == "jane.doe@foo.io"
    assert r.domain == "foo.io"
    assert r.local == "jane.doe"
    assert r.enhanced == "5.1.1"
    assert r.smtp_code == 550


# --------------------------------------------------------------------------- #
# rescore_csv: banking, corrected candidate, and accuracy-compounding
# --------------------------------------------------------------------------- #
def test_rescore_banks_wrong_guess_and_compounds(engine, mock_dns, tmp_path):
    csv_path = _write_csv(
        tmp_path / "bounced.csv",
        ["email", "domain", "bounce_status", "reason_class"],
        [["ajith_kumar@underscore.example", "underscore.example", "Bounced", "address_not_found"]],
    )
    items = rescore.rescore_csv(csv_path, engine, engine._kb_path, apply_kb=True)
    assert len(items) == 1
    fix = items[0]
    assert fix.verdict == "WRONG_GUESS"
    assert fix.action == "bank_known_bad"
    assert fix.kb_change and "ajith_kumar" in fix.kb_change

    # The bad local is now banked in the KB overlay.
    entry = kb_store.get_entry(engine.kb, "underscore.example")
    assert "ajith_kumar" in entry["known_bad_locals"]

    # A corrected candidate was generated and it is NOT the bad address.
    assert fix.corrected_candidate is not None
    assert fix.corrected_candidate != "ajith_kumar@underscore.example"
    assert fix.corrected_candidate.endswith("@underscore.example")

    # Accuracy compounds: re-finding the banked local now returns UNDELIVERABLE.
    result = engine.find("Ajith Kumar", "underscore.example")
    banked = [
        sc
        for sc in ([result.best] + result.alternates)
        if sc.candidate.local_part == "ajith_kumar"
    ]
    assert banked and banked[0].status == Status.UNDELIVERABLE


def test_rescore_m365_recipient_rejected_is_banked_not_discarded(engine, mock_dns, tmp_path):
    # loop.example is a Microsoft 365 tenant in the seed KB.
    csv_path = _write_csv(
        tmp_path / "m365.csv",
        ["email", "domain", "bounce_status", "reason_class"],
        [["abhishek.sharma@loop.example", "loop.example", "Bounced", "recipient_rejected"]],
    )
    items = rescore.rescore_csv(csv_path, engine, engine._kb_path, apply_kb=True)
    fix = items[0]
    assert fix.verdict == "PROBABLE_INVALID_M365"
    assert fix.action == "probable_invalid"
    entry = kb_store.get_entry(engine.kb, "loop.example")
    assert "abhishek.sharma" in entry["known_bad_locals"]


def test_rescore_sender_side_left_untouched(engine, mock_dns, tmp_path):
    csv_path = _write_csv(
        tmp_path / "policy.csv",
        ["email", "domain", "bounce_status", "reason_class"],
        [["rahul.verma@underscore.example", "underscore.example", "Bounced", "policy_or_spam_rejection"]],
    )
    items = rescore.rescore_csv(csv_path, engine, engine._kb_path, apply_kb=True)
    fix = items[0]
    assert fix.verdict == "SENDER_SIDE"
    assert fix.action == "sender_side_skip"
    assert fix.kb_change is None
    assert fix.corrected_candidate is None
    # The KB was NOT touched for this address.
    entry = kb_store.get_entry(engine.kb, "underscore.example")
    assert "rahul.verma" not in entry["known_bad_locals"]


def test_rescore_domain_issue_circuit_breaks(engine, mock_dns, tmp_path):
    csv_path = _write_csv(
        tmp_path / "loop.csv",
        ["email", "domain", "bounce_status", "reason_class"],
        [["ajay.kanteti@loop.example", "loop.example", "Bounced", "routing_loop"]],
    )
    items = rescore.rescore_csv(csv_path, engine, engine._kb_path, apply_kb=True)
    fix = items[0]
    assert fix.verdict == "DOMAIN_ISSUE"
    assert fix.action == "circuit_break"
    assert fix.kb_change is None
    entry = kb_store.get_entry(engine.kb, "loop.example")
    assert "ajay.kanteti" not in entry["known_bad_locals"]


def test_rescore_apply_kb_false_does_not_persist(engine, mock_dns, tmp_path):
    csv_path = _write_csv(
        tmp_path / "dry.csv",
        ["email", "domain", "bounce_status", "reason_class"],
        [["some.one@underscore.example", "underscore.example", "Bounced", "address_not_found"]],
    )
    items = rescore.rescore_csv(csv_path, engine, engine._kb_path, apply_kb=False)
    fix = items[0]
    assert fix.verdict == "WRONG_GUESS"
    assert fix.kb_change is None  # dry run: nothing banked
    entry = kb_store.get_entry(engine.kb, "underscore.example")
    assert "some.one" not in entry["known_bad_locals"]
    # Still offers a corrected candidate that excludes the bad local.
    assert fix.corrected_candidate != "some.one@underscore.example"


# --------------------------------------------------------------------------- #
# DSN mailbox parity + fix-list output
# --------------------------------------------------------------------------- #
_DSN = b"""From: MAILER-DAEMON@mail.underscore.example
To: sender@example.com
Subject: Undelivered Mail Returned to Sender
Content-Type: multipart/report; report-type=delivery-status; boundary="B"

--B
Content-Type: text/plain

Delivery failed.

--B
Content-Type: message/delivery-status

Reporting-MTA: dns; mail.underscore.example

Final-Recipient: rfc822; ghost_user@underscore.example
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 <ghost_user@underscore.example> recipient not found

--B--
"""


def test_rescore_mailbox_matches_csv_path(engine, mock_dns, tmp_path):
    mbox = tmp_path / "bounces.mbox"
    mbox.write_bytes(b"From MAILER-DAEMON Thu Jan  1 00:00:00 2026\n" + _DSN + b"\n")

    items = rescore.rescore_mailbox(mbox, engine, engine._kb_path, apply_kb=True)
    assert items, "the DSN should yield at least one fix item"
    fix = next(i for i in items if i.email == "ghost_user@underscore.example")
    assert fix.verdict == "WRONG_GUESS"
    assert fix.enhanced == "5.1.1"
    entry = kb_store.get_entry(engine.kb, "underscore.example")
    assert "ghost_user" in entry["known_bad_locals"]


def test_write_fixlist_roundtrip(engine, mock_dns, tmp_path):
    csv_path = _write_csv(
        tmp_path / "in.csv",
        ["email", "domain", "bounce_status", "reason_class"],
        [["ajith_kumar@underscore.example", "underscore.example", "Bounced", "address_not_found"]],
    )
    items = rescore.rescore_csv(csv_path, engine, engine._kb_path, apply_kb=True)
    out = tmp_path / "fixes.csv"
    rescore.write_fixlist(items, out)

    import csv as _csv

    with out.open(encoding="utf-8") as fh:
        rows = list(_csv.DictReader(fh))
    assert rows[0]["email"] == "ajith_kumar@underscore.example"
    assert rows[0]["verdict"] == "WRONG_GUESS"
    assert rows[0]["action"] == "bank_known_bad"
    assert rows[0]["corrected_candidate"]
    for col in rescore.FIXLIST_COLUMNS:
        assert col in rows[0]


def test_module_level_rescore_csv_exposed():
    import emailfinder

    assert emailfinder.rescore_csv is rescore.rescore_csv
