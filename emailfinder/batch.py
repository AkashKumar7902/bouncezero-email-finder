"""Batch-CSV read/write helpers for the mail-merge workflow.

This module is a thin orchestration layer over :meth:`Engine.find_batch`. It
does three things:

  * :func:`read_input_csv` — read a flexible input CSV (any subset of
    ``name | first,last`` and ``domain | company | linkedin_url``) with an
    optional column-mapping override so a job-seeker's arbitrary spreadsheet
    headers can be renamed to the canonical field names.
  * :func:`run_batch` — drive :meth:`Engine.find_batch`, which fingerprints
    each DISTINCT domain exactly once across the whole file (MX resolution +
    provider classification + catch-all guard are cache-backed), preserving
    input row order, and return a :class:`BatchStats` summary for the CLI.
  * :func:`write_enriched_csv` — emit the frozen :data:`ENRICHED_COLUMNS` set,
    ready to feed straight into a mail-merge.

Safety invariant carried through from scoring/engine: Microsoft 365 and
catch-all rows can only ever carry ``UNKNOWN`` / ``RISKY`` (hard-capped) — they
are NEVER written as DELIVERABLE. LinkedIn URLs in the input are slug-parsed
locally by the engine, never fetched.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .engine import Engine
from .models import FindResult

# The exact, mail-merge-ready output columns (contract-frozen order).
ENRICHED_COLUMNS: list[str] = [
    "email",
    "first",
    "last",
    "domain",
    "company",
    "template",
    "separator",
    "provider",
    "status",
    "confidence",
    "is_catch_all",
    "is_role",
    "is_disposable",
    "webmail",
    "alt_candidates",
    "verification_mode",
    "provenance_id",
]

# Canonical input fields the engine understands, in the order we probe them.
_CANONICAL_FIELDS: list[str] = [
    "name",
    "first",
    "last",
    "domain",
    "company",
    "linkedin_url",
]


@dataclass
class BatchStats:
    """Summary counts for the CLI progress/summary line.

    ``by_status`` maps each rendered status label (the ``Status`` value, plus the
    synthetic ``"suppressed"`` label for opted-out rows) to a count.
    ``distinct_domains`` is the number of unique resolved domains — i.e. the
    number of times MX/provider/catch-all was fingerprinted.
    """

    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    distinct_domains: int = 0
    suppressed: int = 0


def read_input_csv(
    path: Path, mapping: dict[str, str] | None = None
) -> list[dict]:
    """Read a batch input CSV into a list of canonical-field row dicts.

    Each returned dict contains only the non-empty canonical fields present for
    that row, drawn from ``{name, first, last, domain, company, linkedin_url}``.

    ``mapping`` renames *source* headers onto canonical fields: it maps a
    canonical field name to the source column that holds it, e.g.
    ``{"name": "Full Name", "domain": "Company Domain"}``. Header matching is
    case-insensitive; a canonical field with no mapping falls back to a column of
    the same name. Rows preserve input order.
    """
    mapping = mapping or {}
    rows: list[dict] = []
    with Path(path).open("r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            # Case-insensitive header lookup for this row.
            lower = {
                (k or "").strip().lower(): v
                for k, v in raw.items()
                if k is not None
            }
            row: dict = {}
            for field_name in _CANONICAL_FIELDS:
                source = mapping.get(field_name, field_name)
                value = _lookup(raw, lower, source)
                if value is not None:
                    text = str(value).strip()
                    if text:
                        row[field_name] = text
            rows.append(row)
    return rows


def run_batch(
    engine: Engine,
    in_csv: Path,
    out_csv: Path,
    *,
    mapping: dict[str, str] | None = None,
    verify: bool = False,
    use_providers: bool = False,
) -> BatchStats:
    """Read ``in_csv``, run the whole file, and write the enriched ``out_csv``.

    Rows are handed to :meth:`Engine.find_batch`, which groups by resolved
    domain so MX resolution, provider classification and the catch-all
    fingerprint run ONCE per distinct domain across the file (cache-backed),
    while row order is preserved. ``verify`` / ``use_providers`` are applied to
    every row (SMTP + paid providers stay off unless explicitly enabled here).

    Returns a :class:`BatchStats` with per-status counts and the distinct-domain
    count for the CLI summary.
    """
    rows = read_input_csv(in_csv, mapping)
    for row in rows:
        row["verify"] = verify
        row["use_providers"] = use_providers

    results = list(engine.find_batch(rows))
    write_enriched_csv(results, out_csv)

    stats = BatchStats(total=len(results))
    domains: set[str] = set()
    for res in results:
        status = _status_label(res)
        stats.by_status[status] = stats.by_status.get(status, 0) + 1
        if res.suppressed:
            stats.suppressed += 1
        if res.domain:
            domains.add(res.domain)
    stats.distinct_domains = len(domains)
    return stats


def write_enriched_csv(results: Iterable[FindResult], out: Path) -> None:
    """Write the frozen :data:`ENRICHED_COLUMNS` for each FindResult.

    Suppressed rows carry ``status="suppressed"`` and no address. M365/catch-all
    rows carry the hard-capped ``UNKNOWN`` / ``RISKY`` the scorer assigned — they
    are never DELIVERABLE.
    """
    with Path(out).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=ENRICHED_COLUMNS)
        writer.writeheader()
        for res in results:
            row = _enriched_row(res)
            writer.writerow({k: _csv_safe(v) for k, v in row.items()})


def _csv_safe(value):
    """Neutralize spreadsheet formula (CSV) injection from user-supplied cells.

    A value whose first character is ``= + - @`` or TAB/CR can execute as a
    formula in Excel/Sheets; prefix it with a single quote so it stays literal.
    Non-strings pass through unchanged.
    """
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


# --------------------------------------------------------------------------- #
# private helpers
# --------------------------------------------------------------------------- #
def _lookup(raw: dict, lower: dict, source: str) -> str | None:
    """Fetch a column value by name, case-insensitively; None if absent."""
    if source in raw:
        return raw[source]
    return lower.get(source.strip().lower())


def _status_label(res: FindResult) -> str:
    """The rendered status for a result: 'suppressed', the Status value, or ''."""
    if res.suppressed:
        return "suppressed"
    if res.best is not None:
        return res.best.status.value
    return ""


def _first_last(res: FindResult) -> tuple[str, str]:
    """Best-effort (first, last) for the merge, from the query or the name."""
    query = res.query or {}
    first = (query.get("first") or "").strip()
    last = (query.get("last") or "").strip()
    if not first or not last:
        tokens = (query.get("name") or "").split()
        if tokens:
            if not first:
                first = tokens[0]
            if not last and len(tokens) > 1:
                last = tokens[-1]
    return first, last


def _enriched_row(res: FindResult) -> dict:
    """Project a FindResult onto the ENRICHED_COLUMNS dict."""
    first, last = _first_last(res)
    query = res.query or {}
    best = None if res.suppressed else res.best

    if best is not None:
        cand = best.candidate
        template = cand.template
        separator = cand.separator
        confidence = best.score
        is_catch_all = _bool(best.is_catch_all)
        is_role = _bool(best.is_role)
        is_disposable = _bool(best.is_disposable)
        webmail = _bool(best.webmail)
        email = res.best_email() or ""
    else:
        template = separator = ""
        confidence = ""
        is_catch_all = is_role = is_disposable = webmail = ""
        email = ""

    alt_candidates = ""
    if not res.suppressed and res.alternates:
        alts = [
            f"{sc.candidate.local_part}@{res.domain}"
            for sc in res.alternates
            if res.domain
        ]
        alt_candidates = "; ".join(alts)

    return {
        "email": email,
        "first": first,
        "last": last,
        "domain": res.domain or "",
        "company": (query.get("company") or "").strip(),
        "template": template,
        "separator": separator,
        "provider": res.provider.value if res.provider is not None else "",
        "status": _status_label(res),
        "confidence": confidence,
        "is_catch_all": is_catch_all,
        "is_role": is_role,
        "is_disposable": is_disposable,
        "webmail": webmail,
        "alt_candidates": alt_candidates,
        "verification_mode": res.verification_mode,
        "provenance_id": res.provenance_id or "",
    }


def _bool(value) -> str:
    """Render a tri-state/boolean flag for the CSV: 'true' / 'false' / ''."""
    if value is None:
        return ""
    return "true" if value else "false"
