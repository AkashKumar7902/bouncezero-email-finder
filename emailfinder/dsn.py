"""Parser for DSN / bounce messages (RFC 3464 / RFC 3463).

A nice-to-have *superset* of the bounced-CSV re-score path: given a raw bounce
message (or a whole mbox / Maildir), pull out the failed recipient, its RFC 3463
enhanced status code, the 3-digit SMTP reply code, and a ``reason_class`` drawn
from the same audit taxonomy the CSV path uses, so both feed ``rescore``
identically.

Mostly pure — stdlib :mod:`email` / :mod:`mailbox` only, zero network I/O.
"""
from __future__ import annotations

import re
from email import message_from_bytes
from email.message import Message
from email.policy import default as _default_policy
from pathlib import Path
from typing import Iterator

from .models import BounceRow

__all__ = ["parse_dsn_message", "iter_mailbox", "classify_enhanced"]

# RFC 3463 enhanced code, e.g. "5.1.1" / "4.7.1".
_ENHANCED_RE = re.compile(r"\b([245]\.\d{1,3}\.\d{1,3})\b")
# A bare 3-digit SMTP reply code, e.g. "550" / "451".
_SMTP_CODE_RE = re.compile(r"\b([245]\d\d)\b")
# addr-spec inside a Final-Recipient / diagnostic string.
_ADDR_RE = re.compile(r"[\w.+\-]+@[\w.\-]+")


# --- reason-class keyword tables (checked only when the enhanced code is
# --- ambiguous or absent). Order matters: earlier == higher priority.
_ADDRESS_NOT_FOUND_KW = (
    "recipient not found",
    "recipientnotfound",
    "does not exist",
    "no such user",
    "no such recipient",
    "user unknown",
    "unknown user",
    "unknown recipient",
    "no mailbox",
    "mailbox not found",
    "invalid recipient",
    "invalid mailbox",
    "address rejected",
    "recipient address rejected",
    "no such address",
    "unrouteable address",
)
_INACTIVE_KW = (
    "disabled",
    "inactive",
    "suspended",
    "deactivated",
    "no longer active",
    "no longer employed",
    "account is not active",
    "mailbox unavailable",
    "mailbox is full",
    "over quota",
    "quota exceeded",
)
_ROUTING_LOOP_KW = (
    "routing loop",
    "mail loop",
    "loop detected",
    "too many hops",
    "hop count exceeded",
    "maximum hop count",
)
_DNS_FAILURE_KW = (
    "dns",
    "no route to host",
    "unable to route",
    "unrouteable",
    "domain not found",
    "host not found",
    "no mx",
    "unable to resolve",
    "name service",
    "cannot resolve",
    "no such domain",
)
_POLICY_SPAM_KW = (
    "spam",
    "blocked",
    "blacklist",
    "block list",
    "reputation",
    "policy",
    "spf",
    "dkim",
    "dmarc",
    "authentication",
    "content rejected",
    "message rejected",
    "denied by policy",
    "access denied",
)


def classify_enhanced(enhanced: str | None, code: int | None, reason_text: str) -> str:
    """Map an enhanced code + reply code + diagnostic text to a reason class.

    The returned label is one of the audit taxonomy strings::

        address_not_found, recipient_rejected, routing_loop, dns_failure,
        policy_or_spam_rejection, inactive_account

    Returns ``""`` when nothing matches (an honest "unclassified"; the caller
    treats it like the audit's empty ``reason_class``). The enhanced subcode is
    authoritative when present; the diagnostic text is only consulted to
    disambiguate or when no subcode is available.
    """
    text = (reason_text or "").lower()

    if enhanced:
        # Normalize "5.1.1." / stray whitespace.
        parts = enhanced.strip().strip(".").split(".")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            cls, subject, detail = parts
            sub = f"{cls}.{subject}.{detail}"

            # 5.1.1 / 5.1.10 => mailbox does not exist (audit #1 cause).
            if sub in ("5.1.1", "5.1.10"):
                return "address_not_found"
            # 5.4.1 => M365 "Access denied" / DBEB edge decision (audit #2).
            if sub == "5.4.1":
                return "recipient_rejected"
            # 5.4.6 / 5.4.0 => routing loop detected (cdk.com class).
            if sub in ("5.4.6", "5.4.0"):
                return "routing_loop"
            # 5.4.4 => unable to route / DNS problem for the destination.
            if sub in ("5.4.4", "5.1.2"):
                return "dns_failure"
            # 5.2.1 / 5.2.0 => mailbox disabled / not accepting mail.
            if sub in ("5.2.1", "5.2.0"):
                return "inactive_account"
            # Generic subject-class rules apply only to PERMANENT (5.y.z)
            # failures; transient 4.y.z leans on text so it isn't misread as a
            # mailbox-existence signal.
            if cls == "5":
                # 5.7.x => sender-side reputation / auth / policy (about YOU).
                if subject == "7":
                    return "policy_or_spam_rejection"
                # Other 5.1.x recipient problems default to address_not_found.
                if subject == "1":
                    return "address_not_found"
            # Fall through to text heuristics for anything else (incl. 4.x.x).

    # Text-driven fallback (also covers messages with no enhanced code).
    for kw in _ROUTING_LOOP_KW:
        if kw in text:
            return "routing_loop"
    for kw in _DNS_FAILURE_KW:
        if kw in text:
            return "dns_failure"
    for kw in _INACTIVE_KW:
        if kw in text:
            return "inactive_account"
    for kw in _ADDRESS_NOT_FOUND_KW:
        if kw in text:
            return "address_not_found"
    for kw in _POLICY_SPAM_KW:
        if kw in text:
            return "policy_or_spam_rejection"

    return ""


def _first_addr(value: str) -> str:
    """Pull the bare addr-spec out of a Final-Recipient / diagnostic value."""
    m = _ADDR_RE.search(value or "")
    return m.group(0).strip().strip("<>").lower() if m else ""


def _fields_from_block(block: Message | dict[str, str]) -> dict[str, str]:
    """Flatten a delivery-status per-recipient block into a plain dict."""
    if isinstance(block, Message):
        return {k.lower(): str(v) for k, v in block.items()}
    return {k.lower(): str(v) for k, v in block.items()}


def _parse_status_blocks(part: Message) -> list[dict[str, str]]:
    """Return the per-recipient field dicts from a message/delivery-status part.

    Python's email parser normally exposes each RFC 3464 block as its own
    :class:`~email.message.Message`; we fall back to manual blank-line splitting
    when a payload arrives as a raw string.
    """
    blocks: list[dict[str, str]] = []
    payload = part.get_payload()

    if isinstance(payload, list):
        for sub in payload:
            if isinstance(sub, Message):
                blocks.append(_fields_from_block(sub))
        if blocks:
            return blocks

    # Fallback: split the raw text on blank lines and parse header-style fields.
    if isinstance(payload, str):
        text = payload
    else:
        raw = part.get_payload(decode=True)
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else ""

    for chunk in re.split(r"\n\s*\n", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        fields: dict[str, str] = {}
        cur_key: str | None = None
        for line in chunk.splitlines():
            if line[:1] in (" ", "\t") and cur_key:  # header continuation
                fields[cur_key] += " " + line.strip()
                continue
            if ":" in line:
                key, _, val = line.partition(":")
                cur_key = key.strip().lower()
                fields[cur_key] = val.strip()
        if fields:
            blocks.append(fields)
    return blocks


def _row_from_fields(fields: dict[str, str]) -> BounceRow | None:
    """Build a :class:`BounceRow` from one delivery-status recipient block."""
    recipient = fields.get("final-recipient") or fields.get("original-recipient") or ""
    email = _first_addr(recipient)
    if not email or "@" not in email:
        return None

    local, _, domain = email.partition("@")

    diagnostic = fields.get("diagnostic-code", "")
    status = fields.get("status", "")

    # Enhanced code: prefer the Status field, else scrape the diagnostic text.
    enhanced = None
    m = _ENHANCED_RE.search(status)
    if not m:
        m = _ENHANCED_RE.search(diagnostic)
    if m:
        enhanced = m.group(1)

    # 3-digit SMTP reply code lives in the diagnostic ("smtp; 550 5.1.1 ...").
    smtp_code = None
    cm = _SMTP_CODE_RE.search(diagnostic)
    if cm:
        smtp_code = int(cm.group(1))

    reason_text = diagnostic or fields.get("action", "")
    reason_class = classify_enhanced(enhanced, smtp_code, reason_text)

    return BounceRow(
        raw=dict(fields),
        email=email,
        local=local,
        domain=domain,
        smtp_code=smtp_code,
        enhanced=enhanced,
        reason_class=reason_class or None,
        provider_hint=None,
    )


def parse_dsn_message(raw: bytes) -> list[BounceRow]:
    """Parse a raw DSN message into one :class:`BounceRow` per failed recipient.

    Walks the ``multipart/report`` container for the ``message/delivery-status``
    part and reads each per-recipient block. Non-DSN messages (or DSNs with no
    parseable recipient) yield an empty list rather than raising.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    try:
        msg = message_from_bytes(raw, policy=_default_policy)
    except Exception:
        return []

    rows: list[BounceRow] = []
    for part in msg.walk():
        if part.get_content_type() == "message/delivery-status":
            for fields in _parse_status_blocks(part):
                row = _row_from_fields(fields)
                if row is not None:
                    rows.append(row)

    if rows:
        return rows

    # Fallback for non-RFC-3464 bounces: scavenge the flattened body text for
    # Final-Recipient / Status / Diagnostic-Code lines.
    return _parse_loose(msg)


def _parse_loose(msg: Message) -> list[BounceRow]:
    """Best-effort recovery when there is no delivery-status part."""
    text_parts: list[str] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype.startswith("multipart/"):
            continue
        try:
            body = part.get_payload(decode=True)
        except Exception:
            body = None
        if isinstance(body, (bytes, bytearray)):
            text_parts.append(body.decode("utf-8", "replace"))
        elif isinstance(part.get_payload(), str):
            text_parts.append(part.get_payload())
    text = "\n".join(text_parts)

    m = re.search(r"final-recipient:\s*[^;\n]*;?\s*(\S+@\S+)", text, re.IGNORECASE)
    if not m:
        return []
    email = _first_addr(m.group(1))
    if not email or "@" not in email:
        return []
    local, _, domain = email.partition("@")

    enhanced = None
    sm = re.search(r"status:\s*([245]\.\d{1,3}\.\d{1,3})", text, re.IGNORECASE)
    if sm:
        enhanced = sm.group(1)
    elif (em := _ENHANCED_RE.search(text)):
        enhanced = em.group(1)

    dm = re.search(r"diagnostic-code:\s*(.+)", text, re.IGNORECASE)
    reason_text = dm.group(1).strip() if dm else text
    smtp_code = None
    if dm and (cm := _SMTP_CODE_RE.search(dm.group(1))):
        smtp_code = int(cm.group(1))

    reason_class = classify_enhanced(enhanced, smtp_code, reason_text)
    return [
        BounceRow(
            raw={"source": "loose"},
            email=email,
            local=local,
            domain=domain,
            smtp_code=smtp_code,
            enhanced=enhanced,
            reason_class=reason_class or None,
            provider_hint=None,
        )
    ]


def iter_mailbox(path: Path) -> Iterator[BounceRow]:
    """Iterate an mbox file or a Maildir, yielding parsed :class:`BounceRow`.

    A directory is opened as a Maildir; anything else as an mbox. Each contained
    message is passed through :func:`parse_dsn_message`, so non-DSN messages are
    silently skipped.
    """
    import mailbox

    path = Path(path)
    if path.is_dir():
        box: mailbox.Mailbox = mailbox.Maildir(str(path), factory=None, create=False)
    else:
        box = mailbox.mbox(str(path), factory=None, create=False)

    try:
        for message in box:
            try:
                raw = message.as_bytes()
            except Exception:
                continue
            yield from parse_dsn_message(raw)
    finally:
        try:
            box.close()
        except Exception:
            pass
