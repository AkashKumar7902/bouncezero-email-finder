"""Persistence layer for the hosted public web app.

Defines the FROZEN :class:`Store` protocol (see ``webapp/CONTRACT.md``) plus a
fully-working, dict-backed :class:`MemoryStore` used by local dev and the test
suite. The Postgres implementation lives in ``webapp/store_pg.py`` and satisfies
the same protocol.

This module is ADDITIVE: it never imports or mutates anything under
``emailfinder/`` beyond the pure, side-effect-free helpers the contract mandates
reusing — the shape taxonomy (:func:`emailfinder.shapes.shape`) and the
suppression-key normalizers from :mod:`emailfinder.compliance`. Verification is
OFF in the hosted app, so nothing here touches ``emailfinder.smtp_probe``.

The in-memory / on-the-wire KB representation matches what
``ranking``/``scoring``/``filters`` expect:

* ``known_bad_locals`` is a ``set`` (``filters.in_known_bad`` does membership),
* ``no_bounce_locals`` is a ``list``,
* ``dominant_separator`` is ``""`` for "no separator" (NEVER the on-disk
  ``"(none)"`` sentinel the local file KB uses).
"""
from __future__ import annotations

import time
import uuid
from typing import Protocol, runtime_checkable

from emailfinder.compliance import _identity_key, _norm_email, _norm_identity
from emailfinder.models import DomainFingerprint
from emailfinder.shapes import shape as _shape

# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
# Separator characters that can be embedded in a STRUCTURAL shape label.
_SEP_CHARS = "._-"
# Non-structural shape labels carry NO separator. Note "single_token" literally
# contains an underscore, so it must be excluded explicitly (it is a word, not a
# structural label with an "_" separator).
_NO_SEP_SHAPES = frozenset({"single_token", "name+digits", "other", ""})


def _sep_from_shape(shape: str) -> str:
    """Return the literal separator embedded in a STRUCTURAL shape label.

    ``first_l`` -> ``_``, ``first.last`` -> ``.``, ``f-last`` -> ``-``; ``""`` for
    ``single_token`` / ``name+digits`` / ``other`` / ``""`` (the ``_`` inside
    ``single_token`` is part of the word, not a separator). Mirrors
    ``ranking._sep_from_shape`` / ``kb_store._sep_from_shape`` exactly so the KB
    round-trips losslessly across surfaces.
    """
    label = shape or ""
    if label in _NO_SEP_SHAPES:
        return ""
    for ch in _SEP_CHARS:
        if ch in label:
            return ch
    return ""


def _norm_domain(domain: str | None) -> str:
    """Canonicalize a domain for KB keys/lookups: lowercase, no trailing dot."""
    return (domain or "").strip().lower().rstrip(".")


def _norm_local(local: str | None) -> str:
    """Canonicalize a local part: lowercase, whitespace-stripped."""
    return (local or "").strip().lower()


def _promote_dominant(entry: dict) -> None:
    """Promote ``dominant_shape``/``dominant_separator`` to the distribution ARGMAX.

    A single verified local must NEVER override the learned majority: the
    dominant pattern is always the ``shape_distribution`` argmax (matching
    ``kb_store.upsert_verified``), and the separator is derived from that same
    argmax shape so the two always agree. A no-op when the distribution is empty
    (the caller keeps whatever single-example fallback it already set).
    """
    dist = entry.get("shape_distribution") or {}
    if not dist:
        return
    argmax_shape = max(dist.items(), key=lambda kv: int(kv[1]))[0]
    entry["dominant_shape"] = argmax_shape
    entry["dominant_separator"] = _sep_from_shape(argmax_shape)


def _new_entry() -> dict:
    """Return a fresh, empty in-memory KB entry for an unseen domain."""
    return {
        "provider": "none_or_unknown",
        "dominant_shape": "",
        "dominant_separator": "",
        "shape_distribution": {},
        "no_bounce_locals": set(),
        "known_bad_locals": set(),
    }


# --------------------------------------------------------------------------- #
# FROZEN protocol
# --------------------------------------------------------------------------- #
@runtime_checkable
class Store(Protocol):
    """The persistence surface the hosted app depends on (see CONTRACT.md)."""

    # --- knowledge base ---
    def get_kb_entry(self, domain: str) -> dict | None: ...

    def upsert_verified(
        self,
        domain: str,
        template: str,
        separator: str,
        provider_value: str,
        example_local: str,
    ) -> None: ...

    def append_known_bad(self, domain: str, local: str, source: str) -> None: ...

    # --- domain fingerprint cache ---
    def get_domain_fp(self, domain: str, ttl_days: int) -> DomainFingerprint | None: ...

    def put_domain_fp(self, fp: DomainFingerprint) -> None: ...

    # --- suppression / opt-out (global) ---
    def is_suppressed(
        self, email: str | None, name: str | None, domain: str | None
    ) -> bool: ...

    def suppression_emails(self) -> set[str]: ...

    def add_suppression(
        self, email: str | None, name: str | None, domain: str | None, source: str
    ) -> None: ...

    # --- audit log ---
    def log_lookup(self, record: dict) -> str: ...

    def purge_lookups(self, older_than_days: int) -> int: ...

    def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# In-memory implementation (local dev + tests)
# --------------------------------------------------------------------------- #
class MemoryStore:
    """A complete, dict-backed :class:`Store`.

    State is process-local and lost on restart — it is what tests and local dev
    run against. The hosted app selects :class:`webapp.store_pg.PgStore` whenever
    ``DATABASE_URL`` is set. All KB/suppression/lookup state is GLOBAL (there are
    no per-user silos in the hosted app), matching the Postgres tables.
    """

    def __init__(self) -> None:
        self._kb: dict[str, dict] = {}
        self._domain_fp: dict[str, DomainFingerprint] = {}
        self._supp_emails: set[str] = set()
        self._supp_identities: set[str] = set()
        self._lookups: list[dict] = []

    # -- knowledge base -------------------------------------------------- #
    def _ensure_entry(self, domain: str) -> dict:
        key = _norm_domain(domain)
        entry = self._kb.get(key)
        if entry is None:
            entry = _new_entry()
            self._kb[key] = entry
        return entry

    def get_kb_entry(self, domain: str) -> dict | None:
        """Return the domain's KB entry in the exact shape ranking/filters expect.

        ``known_bad_locals`` is a ``set``, ``no_bounce_locals`` a sorted ``list``,
        ``dominant_separator`` is ``""`` (never ``"(none)"``). Returns ``None`` for
        an unknown domain. The returned dict is a fresh copy, so a caller can never
        mutate stored state.
        """
        key = _norm_domain(domain)
        entry = self._kb.get(key)
        if entry is None:
            return None
        return {
            "provider": entry.get("provider", "none_or_unknown"),
            "dominant_shape": entry.get("dominant_shape", "") or "",
            "dominant_separator": entry.get("dominant_separator", "") or "",
            "shape_distribution": dict(entry.get("shape_distribution") or {}),
            "no_bounce_locals": sorted(entry.get("no_bounce_locals") or set()),
            "known_bad_locals": set(entry.get("known_bad_locals") or set()),
        }

    def upsert_verified(
        self,
        domain: str,
        template: str,
        separator: str,
        provider_value: str,
        example_local: str,
    ) -> None:
        """Fold a confirmed no-bounce local back into the KB (feedback loop).

        Records the local in ``no_bounce_locals`` (deduped), bumps
        ``shape_distribution`` via :func:`emailfinder.shapes.shape` when the local
        is newly seen, refreshes the provider, and promotes ``dominant_shape`` to
        the distribution ARGMAX (a single example never overrides the learned
        majority). ``template`` is accepted for signature stability; storage keys
        off the verified local's shape so ``dominant_shape`` always matches the
        distribution keys.
        """
        entry = self._ensure_entry(domain)
        if provider_value:
            entry["provider"] = provider_value

        local = _norm_local(example_local)
        shp, shp_sep = _shape(local) if local else ("", "")

        if local and local not in entry["no_bounce_locals"]:
            entry["no_bounce_locals"].add(local)
            if shp:
                dist = entry["shape_distribution"]
                dist[shp] = int(dist.get(shp, 0)) + 1

        dist = entry.get("shape_distribution") or {}
        if dist:
            _promote_dominant(entry)
        elif shp:
            # No distribution yet (e.g. a non-structural local): fall back to the
            # single example's shape + the separator implied by that shape.
            entry["dominant_shape"] = shp
            entry["dominant_separator"] = separator or shp_sep or ""

    def append_known_bad(self, domain: str, local: str, source: str) -> None:
        """Bank a confirmed not-found local as ``known_bad`` (deduped).

        Also drops it from ``no_bounce_locals`` if it was ever banked there, so a
        later good/bad flip resolves to bad. No-op for an empty local.
        """
        local = _norm_local(local)
        if not local:
            return
        entry = self._ensure_entry(domain)
        if local not in entry["known_bad_locals"]:
            entry["known_bad_locals"].add(local)
            entry["no_bounce_locals"].discard(local)

    # -- domain fingerprint cache --------------------------------------- #
    def get_domain_fp(self, domain: str, ttl_days: int) -> DomainFingerprint | None:
        """Return the cached fingerprint if present and fresher than ``ttl_days``.

        A fingerprint older than the TTL (or a non-positive TTL) is treated as
        expired and returns ``None`` so the caller re-probes DNS.
        """
        key = _norm_domain(domain)
        fp = self._domain_fp.get(key)
        if fp is None:
            return None
        if ttl_days is not None and ttl_days > 0:
            age = time.time() - float(fp.last_probed_at or 0.0)
            if age > ttl_days * 86400.0:
                return None
        return fp

    def put_domain_fp(self, fp: DomainFingerprint) -> None:
        """Store (replace) the fingerprint for ``fp.domain``."""
        self._domain_fp[_norm_domain(fp.domain)] = fp

    # -- suppression / opt-out (global) --------------------------------- #
    def is_suppressed(
        self, email: str | None, name: str | None, domain: str | None
    ) -> bool:
        """True if this address or ``name@domain`` identity is on the opt-out list.

        Uses the SAME compliance normalizers as :meth:`add_suppression`, so query
        keys and stored keys always match regardless of case / whitespace.
        """
        ne = _norm_email(email)
        if ne and ne in self._supp_emails:
            return True
        key = _identity_key(name, domain)
        if key and _norm_identity(key) in self._supp_identities:
            return True
        return False

    def suppression_emails(self) -> set[str]:
        """Return a copy of the normalized suppressed-address set."""
        return set(self._supp_emails)

    def add_suppression(
        self, email: str | None, name: str | None, domain: str | None, source: str
    ) -> None:
        """Add an opt-out row. No-op unless an address or a full name+domain given.

        The address is keyed via ``_norm_email`` and the ``name@domain`` identity
        via ``_identity_key`` (then normalized with ``_norm_identity`` so it matches
        the read path in :meth:`is_suppressed`).
        """
        ne = _norm_email(email)
        identity = _identity_key(name, domain)
        if not ne and not identity:
            return
        if ne:
            self._supp_emails.add(ne)
        if identity:
            ni = _norm_identity(identity)
            if ni:
                self._supp_identities.add(ni)

    # -- audit log ------------------------------------------------------- #
    def log_lookup(self, record: dict) -> str:
        """Append one audit record; return its id (generated when absent)."""
        rec = dict(record or {})
        rec_id = rec.get("id") or uuid.uuid4().hex
        rec["id"] = rec_id
        rec.setdefault("ts", time.time())
        self._lookups.append(rec)
        return str(rec_id)

    def purge_lookups(self, older_than_days: int) -> int:
        """Drop audit rows older than ``older_than_days``; return the count purged."""
        cutoff = time.time() - float(older_than_days) * 86400.0
        kept: list[dict] = []
        purged = 0
        for rec in self._lookups:
            ts = rec.get("ts")
            if isinstance(ts, (int, float)) and float(ts) < cutoff:
                purged += 1
                continue
            kept.append(rec)
        self._lookups = kept
        return purged

    def close(self) -> None:
        """No-op for the in-memory store (kept for interface symmetry)."""
        return None
