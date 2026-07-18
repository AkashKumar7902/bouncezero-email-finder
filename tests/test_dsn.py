"""Tests for emailfinder.dsn — DSN/bounce parsing + enhanced-code classing."""
from __future__ import annotations

import mailbox
from pathlib import Path

import pytest

from emailfinder.dsn import classify_enhanced, iter_mailbox, parse_dsn_message


# --- a canonical RFC 3464 multipart/report bounce (mailbox does not exist) ----
DSN_5_1_1 = b"""\
From: MAILER-DAEMON@mx.example.com
To: sender@ours.com
Subject: Undelivered Mail Returned to Sender
Content-Type: multipart/report; report-type=delivery-status; boundary="XYZ"
MIME-Version: 1.0

--XYZ
Content-Type: text/plain; charset=us-ascii

This is the mail system at host mx.example.com.
The following address failed permanently.

--XYZ
Content-Type: message/delivery-status

Reporting-MTA: dns; mx.example.com

Final-Recipient: rfc822; ajith.kumar@acme.example
Original-Recipient: rfc822;ajith.kumar@acme.example
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 <ajith.kumar@acme.example>: Recipient address rejected: User unknown

--XYZ
Content-Type: message/rfc822

From: sender@ours.com
To: ajith.kumar@acme.example
Subject: Hi

--XYZ--
"""


def test_parse_dsn_recipient_and_enhanced():
    rows = parse_dsn_message(DSN_5_1_1)
    assert len(rows) == 1
    row = rows[0]
    assert row.email == "ajith.kumar@acme.example"
    assert row.local == "ajith.kumar"
    assert row.domain == "acme.example"
    assert row.enhanced == "5.1.1"
    assert row.smtp_code == 550
    assert row.reason_class == "address_not_found"


def test_parse_dsn_accepts_str_and_non_dsn():
    # str input is coerced; a plain (non-report) message yields nothing.
    assert parse_dsn_message("Subject: hi\n\nnot a bounce") == []


# --- classify_enhanced: enhanced code is authoritative -----------------------
@pytest.mark.parametrize(
    "enhanced,text,expected",
    [
        ("5.1.1", "user unknown", "address_not_found"),
        ("5.1.10", "RecipientNotFound", "address_not_found"),
        ("5.4.1", "Access denied", "recipient_rejected"),
        ("5.4.6", "routing loop detected", "routing_loop"),
        ("5.4.4", "unable to route", "dns_failure"),
        ("5.2.1", "mailbox disabled", "inactive_account"),
        ("5.7.1", "message blocked by policy", "policy_or_spam_rejection"),
    ],
)
def test_classify_by_enhanced(enhanced, text, expected):
    assert classify_enhanced(enhanced, None, text) == expected


# --- classify_enhanced: text fallback when no/ambiguous subcode --------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Mail loop detected: too many hops", "routing_loop"),
        ("DNS error: domain not found", "dns_failure"),
        ("The account has been disabled", "inactive_account"),
        ("No such user here", "address_not_found"),
        ("Message flagged as spam", "policy_or_spam_rejection"),
        ("something totally unclassifiable", ""),
    ],
)
def test_classify_text_fallback(text, expected):
    assert classify_enhanced(None, None, text) == expected


def test_classify_transient_4xx_uses_text():
    # 4.x.x is transient; classify_enhanced still leans on text, else "".
    assert classify_enhanced("4.7.1", "greylisted, try again later", "") == ""
    assert classify_enhanced("4.2.2", "mailbox is full", "mailbox is full") == "inactive_account"


def test_parse_dsn_feeds_like_csv():
    """A parsed DSN row exposes the same fields the CSV re-score path needs."""
    row = parse_dsn_message(DSN_5_1_1)[0]
    for attr in ("email", "local", "domain", "enhanced", "reason_class"):
        assert hasattr(row, attr)
    assert row.provider_hint is None


def test_iter_mailbox_mbox(tmp_path: Path):
    mbox_path = tmp_path / "bounces.mbox"
    box = mailbox.mbox(str(mbox_path))
    box.lock()
    box.add(DSN_5_1_1)
    box.add(b"From: x@y.com\nSubject: not a bounce\n\nhello")
    box.flush()
    box.unlock()
    box.close()

    rows = list(iter_mailbox(mbox_path))
    assert len(rows) == 1
    assert rows[0].email == "ajith.kumar@acme.example"
    assert rows[0].reason_class == "address_not_found"


def test_iter_mailbox_maildir(tmp_path: Path):
    md_path = tmp_path / "Maildir"
    box = mailbox.Maildir(str(md_path))
    box.add(DSN_5_1_1)
    box.close()

    rows = list(iter_mailbox(md_path))
    assert len(rows) == 1
    assert rows[0].domain == "acme.example"


def test_multi_recipient_dsn():
    raw = b"""\
From: MAILER-DAEMON@mx.example.com
To: sender@ours.com
Content-Type: multipart/report; report-type=delivery-status; boundary="B"
MIME-Version: 1.0

--B
Content-Type: message/delivery-status

Reporting-MTA: dns; mx.example.com

Final-Recipient: rfc822; alice@acme.com
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 User unknown

Final-Recipient: rfc822; bob@acme.com
Action: failed
Status: 5.4.1
Diagnostic-Code: smtp; 550 5.4.1 Access denied

--B--
"""
    rows = parse_dsn_message(raw)
    assert {r.email for r in rows} == {"alice@acme.com", "bob@acme.com"}
    by_email = {r.email: r for r in rows}
    assert by_email["alice@acme.com"].reason_class == "address_not_found"
    assert by_email["bob@acme.com"].reason_class == "recipient_rejected"
