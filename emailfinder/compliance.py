"""I/O clean-room legal gate (research dossier 8.1).

This module is a HARD gate, not advisory. It owns three responsibilities:

1. **Per-user data silo.** Each ``user_id`` gets an isolated directory holding
   its own KB overlay, SQLite cache, and provenance log. Silos never share
   rows, so one user's derived guesses can never leak into another's — the
   derivation-only, per-user-siloed posture that keeps the tool clear of the
   Kaspr/CNIL precedent (dossier 8.1 / pitfall 11).
2. **Global cross-user suppression / opt-out.** A single shared JSONL list
   (``<base_dir>/global_suppression.jsonl``) that any recipient can join via the
   public opt-out page or that a 5.x DSN can feed. Checked BEFORE any result is
   returned, honoring the absolute Art. 21 opt-out.
3. **Per-record provenance + retention.** Every find appends one provenance
   line ("derived from user-entered name + public MX") to the per-user log, and
   ``purge_expired`` enforces the ~90-day retention cap.

Depends only on :mod:`emailfinder.models`. Pure stdlib (``json``, ``pathlib``,
``uuid``, ``time``, ``unicodedata``) — no optional third-party deps.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
import uuid
from pathlib import Path

from .models import Candidate, MXInfo

# Fixed, honest provenance source string (dossier 8.1). Never scraped.
_SOURCE = "user-entered name + public MX"

_UNSAFE = re.compile(r"[^a-z0-9_-]+")
_WS = re.compile(r"\s+")


def _safe_user_id(user_id: str) -> str:
    """Return a filesystem-safe directory name for ``user_id``.

    Lower-cased, diacritics stripped, every run of unsafe characters collapsed
    to a single underscore. Empty / all-unsafe ids fall back to ``"default"``
    so a silo directory always resolves.
    """
    folded = _strip_diacritics(user_id or "").lower()
    safe = _UNSAFE.sub("_", folded).strip("_")
    return safe or "default"


def _strip_diacritics(text: str) -> str:
    """NFKD-fold ``text`` and drop combining marks (stdlib romanization)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _norm_email(email: str | None) -> str | None:
    """Canonical suppression key for an address: trimmed + lower-cased."""
    if not email:
        return None
    e = email.strip().lower()
    return e or None


def _norm_name(name: str | None) -> str | None:
    """Canonical name form: diacritics stripped, lower-cased, whitespace
    collapsed to single spaces."""
    if not name:
        return None
    n = _WS.sub(" ", _strip_diacritics(name).strip().lower())
    return n or None


def _identity_key(name: str | None, domain: str | None) -> str | None:
    """Normalized ``name@domain`` suppression key, or None if incomplete."""
    n = _norm_name(name)
    d = _norm_email(domain)  # same trim+lower canonicalization suits a domain
    if not n or not d:
        return None
    return f"{n}@{d}"


def _norm_identity(identity: str | None) -> str | None:
    """Canonicalize a stored ``name@domain`` identity string for matching.

    Applies the same diacritic-strip + lowercase + whitespace-collapse used to
    build query keys, so an identity row imported from any source matches
    regardless of case/spacing.
    """
    if not identity:
        return None
    n = _WS.sub(" ", _strip_diacritics(identity).strip().lower())
    return n or None


class Compliance:
    """Clean-room gate for one user, sharing the global suppression list.

    Parameters
    ----------
    user_id:
        Owner of this silo. Sanitized for use as a directory name.
    base_dir:
        Root under which the global suppression file and every per-user silo
        live.
    retention_days:
        Age (in days) beyond which provenance rows are purged.
    """

    def __init__(self, user_id: str, base_dir: Path, retention_days: int = 90):
        self.user_id = user_id
        self.retention_days = int(retention_days)
        self.base_dir = Path(base_dir).expanduser()
        self.silo_dir = self.base_dir / "users" / _safe_user_id(user_id)
        # Global suppression list is SHARED across every user's silo.
        self.global_suppression_path = self.base_dir / "global_suppression.jsonl"
        self._ensure_silo()

    # -- silo layout -----------------------------------------------------

    def _ensure_silo(self) -> None:
        """Create the per-user silo directory (and base dir) if absent."""
        self.silo_dir.mkdir(parents=True, exist_ok=True)

    def silo_paths(self) -> dict[str, Path]:
        """Return the per-user file paths the Engine wires into kb_store/cache.

        ``suppression`` is the SHARED global list (opt-outs are cross-user);
        ``kb``, ``cache`` and ``provenance`` are private to this user.
        """
        return {
            "kb": self.silo_dir / "kb.json",
            "cache": self.silo_dir / "cache.sqlite",
            "suppression": self.global_suppression_path,
            "provenance": self.silo_dir / "provenance.jsonl",
        }

    @property
    def provenance_path(self) -> Path:
        return self.silo_dir / "provenance.jsonl"

    # -- suppression / opt-out ------------------------------------------

    def _load_suppression(self) -> tuple[set[str], set[str]]:
        """Read the global suppression list into ``(emails, identities)`` sets.

        Malformed lines are skipped rather than raised — a corrupt opt-out row
        must never crash the gate open OR closed for unrelated queries.
        """
        emails: set[str] = set()
        identities: set[str] = set()
        path = self.global_suppression_path
        if not path.exists():
            return emails, identities
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(row, dict):
                    continue
                # Normalize on READ with the SAME canonicalization used to build
                # the query keys, so rows written by any source (DSN feed, manual
                # import, differing case/whitespace) still match is_suppressed.
                if row.get("email"):
                    ne = _norm_email(str(row["email"]))
                    if ne:
                        emails.add(ne)
                if row.get("identity"):
                    ni = _norm_identity(str(row["identity"]))
                    if ni:
                        identities.add(ni)
        return emails, identities

    def load_suppression_sets(self) -> tuple[set[str], set[str]]:
        """Public accessor for the ``(emails, identities)`` suppression sets so a
        caller (the engine) can filter many candidate addresses without re-reading
        the file per address."""
        return self._load_suppression()

    def is_suppressed(
        self, email: str | None, name: str | None, domain: str | None
    ) -> bool:
        """True if this identity is on the global opt-out list.

        Matches on either the normalized address or the normalized
        ``name@domain`` key. The engine short-circuits to
        ``FindResult(suppressed=True)`` before any processing when this is True.
        """
        emails, identities = self._load_suppression()
        norm_email = _norm_email(email)
        if norm_email and norm_email in emails:
            return True
        key = _identity_key(name, domain)
        if key and key in identities:
            return True
        return False

    def add_suppression(
        self,
        email: str | None,
        name: str | None,
        domain: str | None,
        source: str,
    ) -> None:
        """Append an opt-out row to the global suppression list.

        Fed by the public opt-out endpoint or a 5.x DSN. Stores whichever keys
        are derivable (address and/or ``name@domain``). A no-op if neither an
        address nor a complete name+domain pair is supplied.
        """
        norm_email = _norm_email(email)
        identity = _identity_key(name, domain)
        if not norm_email and not identity:
            return
        record = {
            "email": norm_email,
            "identity": identity,
            "name": _norm_name(name),
            "domain": _norm_email(domain),
            "source": source,
            "added_at": time.time(),
        }
        self.global_suppression_path.parent.mkdir(parents=True, exist_ok=True)
        with self.global_suppression_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # -- provenance ------------------------------------------------------

    def build_provenance(
        self,
        query: dict,
        mx: MXInfo | None,
        chosen: Candidate | None,
        verification_mode: str,
        reasons: list[str],
    ) -> dict:
        """Assemble the per-record provenance dict (dossier 8.1).

        Records that the result was DERIVED — user-entered name + public MX —
        never scraped. When a LinkedIn URL was supplied, flags that it was used
        for LOCAL slug parsing only (no linkedin.com network I/O ever happens).
        """
        query = dict(query or {})
        linkedin_local_only = bool(
            query.get("linkedin_url") or query.get("linkedin_slug")
        )
        domain = None
        mx_hosts: list[str] = []
        if mx is not None:
            domain = mx.domain
            mx_hosts = list(mx.hosts)
        if not domain:
            domain = query.get("domain")

        return {
            "id": uuid.uuid4().hex,
            "user_id": self.user_id,
            "timestamp": time.time(),
            "source": _SOURCE,
            "linkedin_slug_local_only": linkedin_local_only,
            "query": query,
            "domain": domain,
            "mx": mx_hosts,
            "template": chosen.template if chosen is not None else None,
            "separator": chosen.separator if chosen is not None else None,
            "local_part": chosen.local_part if chosen is not None else None,
            "provider": query.get("provider"),
            "verification_mode": verification_mode,
            "reasons": list(reasons or []),
        }

    def log_provenance(self, record: dict) -> str:
        """Append one provenance record to the per-user JSONL log; return its id.

        A record without an ``id`` gets one generated so callers can always
        thread the id back onto the FindResult.
        """
        record = dict(record)
        rec_id = record.get("id")
        if not rec_id:
            rec_id = uuid.uuid4().hex
            record["id"] = rec_id
        record.setdefault("timestamp", time.time())
        path = self.provenance_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return str(rec_id)

    def purge_expired(self) -> int:
        """Delete provenance rows older than ``retention_days``; return count.

        Rewrites the per-user provenance log atomically (temp file + replace) so
        a crash mid-purge never leaves a half-written log. Rows lacking a usable
        timestamp are treated as current and kept.
        """
        path = self.provenance_path
        if not path.exists():
            return 0

        cutoff = time.time() - self.retention_days * 86400.0
        kept: list[str] = []
        purged = 0
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                ts = self._row_timestamp(stripped)
                if ts is not None and ts < cutoff:
                    purged += 1
                    continue
                kept.append(stripped)

        if purged == 0:
            return 0

        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for row in kept:
                fh.write(row + "\n")
        tmp.replace(path)
        return purged

    @staticmethod
    def _row_timestamp(line: str) -> float | None:
        """Extract a numeric ``timestamp`` from a JSONL row, or None."""
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(row, dict):
            return None
        ts = row.get("timestamp")
        if isinstance(ts, (int, float)):
            return float(ts)
        return None
