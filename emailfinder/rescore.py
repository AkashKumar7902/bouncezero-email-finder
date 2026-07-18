"""THE HEADLINE FEATURE: re-score a bounced / audit list (research dossier 7.7).

Ingest a bounced CSV (the audit ``records.csv`` schema, or a generic
``email`` + code CSV) or a DSN mailbox, bucket every row by its RFC 3463
enhanced status code / audit ``reason_class``, and emit a per-address
:class:`~emailfinder.models.FixItem` list. For a wrong guess the offending local
part is banked into the domain's ``known_bad_locals`` and the engine is re-run to
produce a *corrected candidate*, so the next run of the finder is more accurate
(the accuracy-compounding feedback loop).

The bucketing pivots on the master rule from the dossier: *the meaning of an SMTP
result depends on the provider behind the MX.* A ``550 5.4.1`` / ``recipient_rejected``
is a directory-not-found (probable-invalid) only on a Microsoft 365 DBEB tenant;
on any other provider it is an ambiguous policy signal and is NOT banked. A
``5.7.x`` / ``policy_or_spam_rejection`` is about the *sender*, never the mailbox;
a ``routing_loop`` / ``dns_failure`` / ``connection_failure`` is a domain-wide
problem (circuit-break, do not re-guess); a ``4.x.x`` is transient (soft retry).

Safety invariants (dossier 2.2 / 4.3 / 5): a timeout / 4xx is NEVER treated as an
invalid mailbox; M365 5.4.1 DSNs are BANKED (not discarded — that would throw
away the audit's 118 true negatives); sender-side and transient failures leave
the KB untouched. Stdlib only (``csv`` / ``re``) plus the pure ``shapes``
taxonomy; DNS/SMTP only happen indirectly through the caller-supplied engine.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import TYPE_CHECKING

from . import dsn, kb_store
from .models import BounceRow, FixItem, Provider, Status
from .shapes import shape

if TYPE_CHECKING:  # pragma: no cover - type-only import, keeps engine out of the
    from .engine import Engine  # import graph for leaf callers.

__all__ = [
    "ENHANCED_CODE_MAP",
    "parse_bounce_csv",
    "classify_bounce",
    "rescore_csv",
    "rescore_mailbox",
    "write_fixlist",
]


# --------------------------------------------------------------------------- #
# Verdict taxonomy
# --------------------------------------------------------------------------- #
# Map an RFC 3463 enhanced code OR an audit ``reason_class`` string to a verdict.
# Verdicts: WRONG_GUESS / PROBABLE_INVALID_M365 / SENDER_SIDE / DOMAIN_ISSUE /
# TRANSIENT / UNKNOWN (see models.FixItem). ``classify_bounce`` refines the
# PROBABLE_INVALID_M365 verdict using the provider behind the MX.
ENHANCED_CODE_MAP: dict[str, str] = {
    # --- RFC 3463 enhanced status codes -------------------------------------
    "5.1.1": "WRONG_GUESS",           # mailbox does not exist (audit #1 cause)
    "5.1.10": "WRONG_GUESS",          # M365 RecipientNotFound
    "5.4.1": "PROBABLE_INVALID_M365",  # M365 DBEB "Access denied" (directory)
    "5.7.1": "SENDER_SIDE",           # auth / policy block (about YOU)
    # --- audit reason_class strings -----------------------------------------
    "address_not_found": "WRONG_GUESS",
    "recipient_rejected": "PROBABLE_INVALID_M365",
    "policy_or_spam_rejection": "SENDER_SIDE",
    "routing_loop": "DOMAIN_ISSUE",
    "dns_failure": "DOMAIN_ISSUE",
    "connection_failure": "DOMAIN_ISSUE",
    "temporary_failure": "TRANSIENT",
    "inactive_account": "WRONG_GUESS",
    "group_not_found_or_permission_denied": "WRONG_GUESS",
}

# Verdict -> the FixItem.action string the sending pipeline branches on.
_VERDICT_ACTION = {
    "WRONG_GUESS": "bank_known_bad",
    "PROBABLE_INVALID_M365": "probable_invalid",
    "SENDER_SIDE": "sender_side_skip",
    "DOMAIN_ISSUE": "circuit_break",
    "TRANSIENT": "retry_soft",
    "UNKNOWN": "",
}

# Verdicts whose local part is banked into the KB's known_bad_locals.
_BANK_VERDICTS = ("WRONG_GUESS", "PROBABLE_INVALID_M365")

# Precedence for resolving a compound reason_class to a single verdict — a
# domain-wide failure dominates an address-specific one, which dominates a
# sender-side one, which dominates a transient defer (lower index = more severe).
_VERDICT_SEVERITY = {
    "DOMAIN_ISSUE": 0,
    "WRONG_GUESS": 1,
    "PROBABLE_INVALID_M365": 2,
    "SENDER_SIDE": 3,
    "TRANSIENT": 4,
    "UNKNOWN": 5,
}

_SPLIT_LOCAL_RE = re.compile(r"[._\-+]+")


# --------------------------------------------------------------------------- #
# CSV ingestion
# --------------------------------------------------------------------------- #
# Canonical column -> the header aliases we accept (case-insensitive).
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "email": ("email", "address", "recipient", "to", "e-mail"),
    "domain": ("domain",),
    "local": ("local", "local_part", "localpart", "user"),
    "reason_class": ("reason_class", "reason", "reasonclass", "category"),
    "bounce_status": ("bounce_status", "status", "result", "disposition"),
    "enhanced": ("enhanced", "enhanced_code", "dsn", "status_code"),
    "code": ("code", "smtp_code", "reply_code"),
    "company": ("company", "organization", "org"),
    "shape": ("shape",),
    "sep": ("sep", "separator"),
    "provider": ("provider", "mx_provider"),
}


def _resolve_columns(
    fieldnames: list[str], column_map: dict[str, str] | None
) -> dict[str, str]:
    """Return ``canonical -> actual header`` for the columns present.

    An explicit ``column_map`` ({source_header: canonical} OR {canonical:
    source_header}) wins over the built-in alias table.
    """
    lower = {fn.strip().lower(): fn for fn in fieldnames if fn}
    resolved: dict[str, str] = {}

    for canonical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lower:
                resolved[canonical] = lower[alias]
                break

    if column_map:
        canonical_names = set(_COLUMN_ALIASES)
        for a, b in column_map.items():
            # Accept either direction: {header: canonical} or {canonical: header}.
            if a in canonical_names and b in lower:
                resolved[a] = lower[b]
            elif b in canonical_names and a in lower:
                resolved[b] = lower[a]
    return resolved


def _extract_enhanced(*values: str) -> str | None:
    """Pull the first RFC 3463 enhanced code (e.g. ``5.1.1``) out of any value."""
    for value in values:
        if not value:
            continue
        m = re.search(r"\b([245]\.\d{1,3}\.\d{1,3})\b", value)
        if m:
            return m.group(1)
    return None


def _extract_code(*values: str) -> int | None:
    """Pull the first 3-digit SMTP reply code out of any value."""
    for value in values:
        if not value:
            continue
        m = re.search(r"\b([245]\d\d)\b", value)
        if m:
            return int(m.group(1))
    return None


def _is_non_bounce(status: str) -> bool:
    """True for an audit row that did NOT bounce (``No bounce found``)."""
    return "no bounce" in (status or "").strip().lower()


def _bouncerow_from_csv(raw: dict, cols: dict[str, str]) -> BounceRow | None:
    """Build a :class:`BounceRow` from one CSV row, or None if it is not a bounce."""
    def cell(name: str) -> str:
        col = cols.get(name)
        return (raw.get(col) or "").strip() if col else ""

    bounce_status = cell("bounce_status")
    # When the source carries a status column, skip clean (non-bounce) rows.
    if "bounce_status" in cols and _is_non_bounce(bounce_status):
        return None

    email = cell("email").lower()
    domain = cell("domain").lower()
    local = cell("local").lower()

    if email and "@" in email:
        e_local, _, e_domain = email.partition("@")
        local = local or e_local
        domain = domain or e_domain
    elif local and domain:
        email = f"{local}@{domain}"
    else:
        return None  # not enough to identify a recipient

    reason_class = cell("reason_class").lower() or None
    enhanced = cell("enhanced") or None
    if enhanced:
        enhanced = _extract_enhanced(enhanced) or enhanced.strip() or None
    else:
        enhanced = _extract_enhanced(bounce_status)
    smtp_code = _extract_code(cell("code"), bounce_status)

    provider_hint = cell("provider").lower() or None

    return BounceRow(
        raw=dict(raw),
        email=email,
        local=local,
        domain=domain,
        smtp_code=smtp_code,
        enhanced=enhanced,
        reason_class=reason_class,
        provider_hint=provider_hint,
    )


def parse_bounce_csv(
    path: Path, column_map: dict[str, str] | None = None
) -> list[BounceRow]:
    """Read a bounced / audit CSV into :class:`BounceRow` objects.

    Auto-detects the audit ``records.csv`` columns
    (``company,email,domain,local,shape,sep,bounce_status,reason_class``) or a
    generic ``email`` + code CSV. Rows the source explicitly marks as
    ``No bounce found`` are dropped so the fix-list only covers real bounces.
    ``column_map`` can rename source headers onto the canonical column names.
    """
    path = Path(path)
    rows: list[BounceRow] = []
    with path.open("r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = _resolve_columns(reader.fieldnames or [], column_map)
        for raw in reader:
            row = _bouncerow_from_csv(raw, cols)
            if row is not None:
                rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def _verdict_from_enhanced(enhanced: str | None) -> str | None:
    """Map a full RFC 3463 enhanced code to a verdict (None if not decodable)."""
    if not enhanced:
        return None
    parts = enhanced.strip().strip(".").split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    cls, subject, detail = parts
    if cls == "4":
        return "TRANSIENT"  # any transient defer -> soft retry, never invalid
    if cls != "5":
        return None
    code = ".".join(parts)
    exact = ENHANCED_CODE_MAP.get(code)
    if exact:
        return exact
    if subject == "7":  # 5.7.x reputation / auth / policy -> about the sender
        return "SENDER_SIDE"
    if code in ("5.4.0", "5.4.4", "5.4.6", "5.1.2"):  # routing / DNS to the host
        return "DOMAIN_ISSUE"
    if subject == "1":  # 5.1.x recipient-address failures -> wrong guess
        return "WRONG_GUESS"
    if subject == "2":
        # 5.2.x mailbox-status. Only a DISABLED mailbox (5.2.1) is bankable; a
        # FULL / over-quota (5.2.2) or too-large (5.2.3) mailbox is a VALID
        # address that must NOT be banked into known_bad_locals.
        if detail == "1":
            return "WRONG_GUESS"   # mailbox disabled / inactive
        return "TRANSIENT"          # full / over-quota / size / other -> not banked
    return None


def classify_bounce(row: BounceRow, provider: Provider | None) -> str:
    """Return the verdict for one bounce row, provider-aware.

    Precedence: a decodable enhanced status code wins; otherwise the audit
    ``reason_class`` is mapped through :data:`ENHANCED_CODE_MAP`; otherwise a bare
    4xx reply code falls back to TRANSIENT and anything else to UNKNOWN.

    The provider refines the ``PROBABLE_INVALID_M365`` bucket: a
    ``5.4.1`` / ``recipient_rejected`` is a directory-not-found (bankable) only on
    a Microsoft 365 DBEB tenant. On a known non-M365 provider the same signal is
    an ambiguous policy block, so it is downgraded to SENDER_SIDE and the local is
    NOT banked (dossier 4.3). When the provider is unknown we keep the audit's
    dominant reading (118/134 of these were M365) and bank it.
    """
    verdict = _verdict_from_enhanced(row.enhanced)
    if verdict is None:
        rc = (row.reason_class or "").strip().lower()
        verdict = ENHANCED_CODE_MAP.get(rc)
        if verdict is None and rc:
            # A compound reason_class ("connection_failure; temporary_failure")
            # is not a single map key; split it, map each token, and take the
            # most-severe verdict present.
            tokens = [t.strip() for t in re.split(r"[;,]", rc) if t.strip()]
            mapped = [ENHANCED_CODE_MAP[t] for t in tokens if t in ENHANCED_CODE_MAP]
            if mapped:
                verdict = min(mapped, key=lambda v: _VERDICT_SEVERITY.get(v, 99))
    if verdict is None:
        if row.smtp_code is not None and 400 <= row.smtp_code < 500:
            verdict = "TRANSIENT"
        else:
            verdict = "UNKNOWN"

    if verdict == "PROBABLE_INVALID_M365":
        if provider is not None and provider != Provider.MICROSOFT365:
            verdict = "SENDER_SIDE"
    return verdict


# --------------------------------------------------------------------------- #
# Re-scoring
# --------------------------------------------------------------------------- #
def _name_from_local(local: str) -> str:
    """Reconstruct a probable name from a local part for the corrected re-guess.

    ``abhijeet.shekhar`` -> ``abhijeet shekhar``; ``ajith_c`` -> ``ajith c``;
    ``achauhan`` -> ``achauhan`` (a single token the engine treats as a mononym).
    Best-effort only: the goal is to drive a fresh candidate generation, and the
    per-domain KB pattern (not this reconstruction) decides the winning shape.
    """
    tokens = [t for t in _SPLIT_LOCAL_RE.split((local or "").strip().lower()) if t]
    return " ".join(tokens)


def _provider_for(engine: "Engine", row: BounceRow) -> Provider | None:
    """Resolve the provider behind a bounced address from the KB (offline)."""
    entry = kb_store.get_entry(engine.kb, row.domain)
    candidates = []
    if entry and entry.get("provider"):
        candidates.append(entry["provider"])
    if row.provider_hint:
        candidates.append(row.provider_hint)
    for value in candidates:
        try:
            return Provider(value)
        except ValueError:
            continue
    return None


def _corrected_candidate(engine: "Engine", row: BounceRow) -> str | None:
    """Re-run the finder (with the bad local now banked) for a better guess.

    Returns the best candidate email whose local part differs from the banked bad
    one, preferring a still-plausible (non-UNDELIVERABLE) candidate. Returns None
    when nothing better can be generated (e.g. a bare mononym with no alternative
    pattern, or a dead domain).
    """
    if not row.domain:
        return None
    name = _name_from_local(row.local)
    if not name:
        return None
    try:
        result = engine.find(name, row.domain)
    except Exception:
        return None

    bad = (row.local or "").strip().lower()
    scored = [sc for sc in ([result.best] + result.alternates) if sc is not None]

    for sc in scored:  # first, a plausible non-undeliverable alternative
        lp = sc.candidate.local_part.lower()
        if lp != bad and sc.status != Status.UNDELIVERABLE:
            return f"{sc.candidate.local_part}@{row.domain}"
    for sc in scored:  # else any other local part
        lp = sc.candidate.local_part.lower()
        if lp != bad:
            return f"{sc.candidate.local_part}@{row.domain}"
    return None


def _bank_source(row: BounceRow, verdict: str) -> str:
    """Choose the reason-class source string recorded on the KB bank."""
    if row.reason_class:
        return row.reason_class
    return "address_not_found" if verdict == "WRONG_GUESS" else "recipient_rejected"


def _handle_row(
    row: BounceRow,
    verdict: str,
    engine: "Engine",
    kb_path: Path,
    apply_kb: bool,
    provider: Provider | None,
) -> FixItem:
    """Apply the per-verdict policy to one row and return its :class:`FixItem`."""
    action = _VERDICT_ACTION.get(verdict, "")
    corrected: str | None = None
    kb_change: str | None = None

    if verdict in _BANK_VERDICTS:
        source = _bank_source(row, verdict)
        if apply_kb and row.local:
            kb_store.append_known_bad(engine.kb, kb_path, row.domain, row.local, source)
            kb_change = f"known_bad += {row.local}"
        corrected = _corrected_candidate(engine, row)
        shp, _sep = shape(row.local) if row.local else ("", "")
        if verdict == "WRONG_GUESS":
            detail = (
                f"wrong guess (shape {shp or 'n/a'}, {source}); "
                "banked known-bad, re-guessed"
            )
        else:
            detail = (
                "M365 DBEB directory-not-found (5.4.1); banked probable-invalid "
                "(not discarded), re-guessed"
            )
    elif verdict == "DOMAIN_ISSUE":
        detail = (
            f"domain-wide failure ({row.reason_class or row.enhanced or 'routing/dns'}) "
            "— circuit-break this domain, do not re-guess"
        )
    elif verdict == "SENDER_SIDE":
        detail = (
            "sender reputation / auth / policy block — fix your sending config; "
            "the recipient address may well be valid, NOT banked"
        )
    elif verdict == "TRANSIENT":
        detail = "transient 4.x.x deferral — retry <=2x over 48h (same IP + MAIL FROM)"
    else:
        detail = "unclassified bounce — left untouched for manual review"

    return FixItem(
        email=row.email,
        domain=row.domain,
        verdict=verdict,
        enhanced=row.enhanced,
        action=action,
        corrected_candidate=corrected,
        kb_change=kb_change,
        detail=detail,
    )


def _rescore_rows(
    rows: list[BounceRow], engine: "Engine", kb_path: Path, apply_kb: bool
) -> list[FixItem]:
    """Shared core: classify + fix every row (used by CSV and mailbox paths)."""
    items: list[FixItem] = []
    for row in rows:
        provider = _provider_for(engine, row)
        verdict = classify_bounce(row, provider)
        items.append(_handle_row(row, verdict, engine, kb_path, apply_kb, provider))
    return items


def rescore_csv(
    path: Path, engine: "Engine", kb_path: Path, apply_kb: bool = True
) -> list[FixItem]:
    """Re-score a bounced / audit CSV, returning the per-address fix list.

    Each row is bucketed by :func:`classify_bounce`; WRONG_GUESS and
    PROBABLE_INVALID_M365 rows bank the local into ``known_bad_locals`` (when
    ``apply_kb``) and re-run :meth:`Engine.find` for a corrected candidate;
    DOMAIN_ISSUE rows flag a circuit-break; SENDER_SIDE and TRANSIENT rows are
    left untouched. The KB is persisted to the per-user silo as it is banked, so
    a subsequent find of a banked local now returns UNDELIVERABLE.
    """
    rows = parse_bounce_csv(path)
    return _rescore_rows(rows, engine, Path(kb_path), apply_kb)


def rescore_mailbox(
    path: Path, engine: "Engine", kb_path: Path, apply_kb: bool = True
) -> list[FixItem]:
    """Same buckets as :func:`rescore_csv`, driven by a DSN mbox / Maildir."""
    rows = list(dsn.iter_mailbox(Path(path)))
    return _rescore_rows(rows, engine, Path(kb_path), apply_kb)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
FIXLIST_COLUMNS = [
    "email",
    "domain",
    "verdict",
    "enhanced",
    "action",
    "corrected_candidate",
    "kb_change",
    "detail",
]


def _csv_safe(value) -> str:
    """Neutralize spreadsheet formula (CSV) injection.

    A cell whose first character is one of ``= + - @`` or a TAB/CR can be
    interpreted as a formula by Excel/Sheets. Prefix such values with a single
    quote so they render as literal text. Applied to every string written out.
    """
    s = "" if value is None else str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def write_fixlist(items: list[FixItem], out: Path) -> None:
    """Write the mail-merge-ready per-address fix CSV.

    Columns: email, domain, verdict, enhanced, action, corrected_candidate,
    kb_change, detail — the corrected candidate is what a job-seeker mail-merges
    against on the next send.
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(FIXLIST_COLUMNS)
        for it in items:
            writer.writerow(
                [
                    _csv_safe(it.email),
                    _csv_safe(it.domain),
                    _csv_safe(it.verdict),
                    _csv_safe(it.enhanced or ""),
                    _csv_safe(it.action),
                    _csv_safe(it.corrected_candidate or ""),
                    _csv_safe(it.kb_change or ""),
                    _csv_safe(it.detail),
                ]
            )
