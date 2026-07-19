"""Postgres-backed :class:`Store` implementation for the hosted web app.

``PgStore(dsn)`` implements the SAME frozen ``Store`` interface used by
``webapp.store.MemoryStore`` (see ``webapp/CONTRACT.md``) against a Postgres
database via **psycopg 3**. It mirrors ``MemoryStore``'s exact semantics:

* ``upsert_verified`` bumps ``shape_distribution`` via ``shapes.shape`` and
  promotes ``dominant_shape`` to the distribution ARGMAX (a single verified
  local never overrides the learned majority), with the matching separator
  derived from that same shape.
* ``append_known_bad`` banks the local as ``known_bad`` and drops it from the
  no-bounce set (deduped).
* ``domain_cache`` entries honour a TTL; a transient ``dns_timeout`` is never
  cached by the caller.
* The ``"(none)"`` sentinel and ``""`` empty separator are handled in-memory,
  exactly as the pure KB store does; the DB always stores the in-memory form.
* Suppression keys are normalised with the SAME helpers the pure compliance
  module uses (``_norm_email`` / ``_identity_key`` / ``_norm_identity``), so
  reads and writes agree with the rest of the stack.

Only **parameterised** SQL is used — user input is never string-formatted into
a statement. ``psycopg`` is imported **lazily** (inside the constructor and
methods) so importing this module never fails when psycopg is absent or no DB
is reachable.
"""
from __future__ import annotations

import time

from emailfinder.compliance import (
    _identity_key,
    _norm_email,
    _norm_identity,
    _norm_name,
)
from emailfinder.models import DomainFingerprint, Provider
from emailfinder.ranking import _sep_from_shape
from emailfinder.shapes import shape as _shape

# Sentinel the pure KB store serialises an empty separator to on disk. The
# hosted DB stores the in-memory ("") form, but we normalise defensively.
_SEP_NONE = "(none)"


# --------------------------------------------------------------------------- #
# Schema (idempotent) + init
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
    domain              TEXT PRIMARY KEY,
    provider            TEXT NOT NULL DEFAULT '',
    mx                  JSONB NOT NULL DEFAULT '[]'::jsonb,
    dominant_shape      TEXT NOT NULL DEFAULT '',
    dominant_separator  TEXT NOT NULL DEFAULT '',
    shape_distribution  JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at          DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS locals (
    domain      TEXT NOT NULL,
    local_part  TEXT NOT NULL,
    status      TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT '',
    seen_at     DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (domain, local_part)
);
CREATE INDEX IF NOT EXISTS locals_domain_idx ON locals (domain);

CREATE TABLE IF NOT EXISTS domain_cache (
    domain          TEXT PRIMARY KEY,
    provider        TEXT NOT NULL DEFAULT '',
    mx              JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_catch_all    BOOLEAN,
    flags           JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_probed_at  DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS suppression (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT,
    identity    TEXT,
    source      TEXT NOT NULL DEFAULT '',
    added_at    DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS suppression_email_idx ON suppression (email);
CREATE INDEX IF NOT EXISTS suppression_identity_idx ON suppression (identity);

CREATE TABLE IF NOT EXISTS lookups (
    id           BIGSERIAL PRIMARY KEY,
    ts           DOUBLE PRECISION NOT NULL DEFAULT 0,
    name         TEXT,
    domain       TEXT,
    local_part   TEXT,
    linkedin_url TEXT,
    provider     TEXT,
    reasons      JSONB NOT NULL DEFAULT '[]'::jsonb,
    ip_hash      TEXT
);
CREATE INDEX IF NOT EXISTS lookups_ts_idx ON lookups (ts);
"""


def _import_psycopg():
    """Lazily import psycopg 3 and its JSONB wrapper.

    Kept out of module scope so ``import webapp.store_pg`` succeeds even when
    psycopg is not installed; the ImportError only surfaces when a DB-touching
    method is actually invoked.
    """
    import psycopg  # noqa: PLC0415  (intentional lazy import)
    from psycopg.types.json import Jsonb  # noqa: PLC0415

    return psycopg, Jsonb


def init_schema(conn_or_dsn) -> None:
    """Create every table + index if absent. Accepts a DSN string or an open
    psycopg connection. When given a DSN, opens (and closes) its own connection.
    """
    if isinstance(conn_or_dsn, str):
        psycopg, _ = _import_psycopg()
        with psycopg.connect(conn_or_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA)
            conn.commit()
    else:
        conn = conn_or_dsn
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()


# --------------------------------------------------------------------------- #
# Helpers (mirror MemoryStore / pure kb_store semantics)
# --------------------------------------------------------------------------- #
def _norm_sep(separator: str | None) -> str:
    """Collapse the ``"(none)"`` sentinel and ``None`` to the in-memory ``""``."""
    if separator is None or separator == _SEP_NONE:
        return ""
    return separator


def _argmax_shape(dist: dict) -> str:
    """Return the ARGMAX shape label of a distribution (highest count wins),
    matching ``emailfinder.kb_store.upsert_verified``. Empty dist -> ``""``."""
    if not dist:
        return ""
    return max(dist.items(), key=lambda kv: int(kv[1]))[0]


# --------------------------------------------------------------------------- #
# PgStore
# --------------------------------------------------------------------------- #
class PgStore:
    """Postgres implementation of the frozen ``Store`` interface.

    The knowledge base, suppression list and audit log are GLOBAL (shared across
    every visitor) — there are no per-user silos in the hosted app.
    """

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn = None  # opened lazily on first use

    # -- connection management ------------------------------------------
    def _connection(self):
        """Return an open psycopg connection, (re)connecting as needed."""
        psycopg, _ = _import_psycopg()
        conn = self._conn
        if conn is None or getattr(conn, "closed", True):
            self._conn = psycopg.connect(self._dsn)
        return self._conn

    def _jsonb(self, obj):
        _, Jsonb = _import_psycopg()
        return Jsonb(obj)

    # -- knowledge base -------------------------------------------------
    def get_kb_entry(self, domain: str) -> dict | None:
        """Assemble the contract KB dict by joining ``domains`` + ``locals``,
        or ``None`` when the domain is unknown."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT provider, dominant_shape, dominant_separator, "
                "shape_distribution FROM domains WHERE domain = %s",
                (domain,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            provider, dominant_shape, dominant_separator, shape_distribution = row

            cur.execute(
                "SELECT local_part, status FROM locals WHERE domain = %s",
                (domain,),
            )
            no_bounce: list[str] = []
            known_bad: set[str] = set()
            for local_part, status in cur.fetchall():
                if status == "known_bad":
                    known_bad.add(local_part)
                elif status == "no_bounce":
                    no_bounce.append(local_part)

        return {
            "provider": provider or "",
            "dominant_shape": dominant_shape or "",
            "dominant_separator": _norm_sep(dominant_separator),
            "shape_distribution": dict(shape_distribution or {}),
            "no_bounce_locals": no_bounce,
            "known_bad_locals": known_bad,
        }

    def upsert_verified(
        self,
        domain: str,
        template: str,
        separator: str,
        provider_value: str,
        example_local: str,
    ) -> None:
        """Fold a confirmed local back into the KB: bank it as ``no_bounce``,
        bump ``shape_distribution`` via ``shapes.shape``, and promote
        ``dominant_shape`` to the distribution ARGMAX (never a one-off minority),
        with the separator derived from that same shape.
        """
        local = (example_local or "").strip().lower()
        shp, shp_sep = _shape(local) if local else ("", "")
        now = time.time()

        conn = self._connection()
        with conn.cursor() as cur:
            # Ensure the domains row exists so we can read/modify its dist.
            cur.execute(
                "INSERT INTO domains (domain) VALUES (%s) "
                "ON CONFLICT (domain) DO NOTHING",
                (domain,),
            )
            cur.execute(
                "SELECT shape_distribution FROM domains WHERE domain = %s FOR UPDATE",
                (domain,),
            )
            row = cur.fetchone()
            dist = dict((row[0] if row else None) or {})

            # Was this local newly banked as no-bounce? Only then bump the dist.
            newly_seen = False
            if local:
                cur.execute(
                    "SELECT status FROM locals WHERE domain = %s AND local_part = %s",
                    (domain, local),
                )
                existing = cur.fetchone()
                newly_seen = existing is None or existing[0] != "no_bounce"

            if local and newly_seen and shp:
                dist[shp] = int(dist.get(shp, 0)) + 1

            # Promote dominant to the argmax of the (possibly updated) dist.
            if dist:
                argmax = _argmax_shape(dist)
                dominant_shape = argmax
                dominant_separator = _sep_from_shape(argmax)
            elif shp:
                dominant_shape = shp
                dominant_separator = _norm_sep(separator) or shp_sep
            else:
                dominant_shape = ""
                dominant_separator = ""

            cur.execute(
                "UPDATE domains SET provider = %s, dominant_shape = %s, "
                "dominant_separator = %s, shape_distribution = %s, updated_at = %s "
                "WHERE domain = %s",
                (
                    provider_value or "",
                    dominant_shape,
                    dominant_separator,
                    self._jsonb(dist),
                    now,
                    domain,
                ),
            )

            if local:
                cur.execute(
                    "INSERT INTO locals (domain, local_part, status, source, seen_at) "
                    "VALUES (%s, %s, 'no_bounce', %s, %s) "
                    "ON CONFLICT (domain, local_part) DO UPDATE SET "
                    "status = 'no_bounce', source = EXCLUDED.source, "
                    "seen_at = EXCLUDED.seen_at",
                    (domain, local, "verified", now),
                )
        conn.commit()

    def append_known_bad(self, domain: str, local: str, source: str) -> None:
        """Bank ``local`` as ``known_bad`` (deduped) and drop it from no-bounce.

        Creates a minimal ``domains`` row if the domain was previously unknown,
        so ``get_kb_entry`` surfaces the known-bad set. No-op for an empty local.
        """
        local = (local or "").strip().lower()
        if not local:
            return
        now = time.time()

        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO domains (domain) VALUES (%s) "
                "ON CONFLICT (domain) DO NOTHING",
                (domain,),
            )
            cur.execute(
                "INSERT INTO locals (domain, local_part, status, source, seen_at) "
                "VALUES (%s, %s, 'known_bad', %s, %s) "
                "ON CONFLICT (domain, local_part) DO UPDATE SET "
                "status = 'known_bad', source = EXCLUDED.source, "
                "seen_at = EXCLUDED.seen_at",
                (domain, local, source or "", now),
            )
        conn.commit()

    # -- domain fingerprint cache ---------------------------------------
    def get_domain_fp(self, domain: str, ttl_days: int) -> DomainFingerprint | None:
        """Return a cached fingerprint, or ``None`` when absent or past its TTL."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT provider, mx, is_catch_all, flags, last_probed_at "
                "FROM domain_cache WHERE domain = %s",
                (domain,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        provider, mx, is_catch_all, flags, last_probed_at = row

        if ttl_days is not None and ttl_days >= 0:
            age = time.time() - float(last_probed_at or 0)
            if age > ttl_days * 86400.0:
                return None

        try:
            prov = Provider(provider)
        except ValueError:
            prov = Provider.NONE_UNKNOWN

        return DomainFingerprint(
            domain=domain,
            provider=prov,
            mx=list(mx or []),
            is_catch_all=is_catch_all,
            last_probed_at=float(last_probed_at or 0),
            flags=dict(flags or {}),
        )

    def put_domain_fp(self, fp: DomainFingerprint) -> None:
        """Insert/refresh a domain fingerprint."""
        provider_value = (
            fp.provider.value if isinstance(fp.provider, Provider) else str(fp.provider)
        )
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO domain_cache "
                "(domain, provider, mx, is_catch_all, flags, last_probed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (domain) DO UPDATE SET "
                "provider = EXCLUDED.provider, mx = EXCLUDED.mx, "
                "is_catch_all = EXCLUDED.is_catch_all, flags = EXCLUDED.flags, "
                "last_probed_at = EXCLUDED.last_probed_at",
                (
                    fp.domain,
                    provider_value,
                    self._jsonb(list(fp.mx or [])),
                    fp.is_catch_all,
                    self._jsonb(dict(fp.flags or {})),
                    float(fp.last_probed_at or 0),
                ),
            )
        conn.commit()

    # -- suppression / opt-out (global) ---------------------------------
    def is_suppressed(
        self, email: str | None, name: str | None, domain: str | None
    ) -> bool:
        """True if the address or the ``name@domain`` identity is opted out."""
        norm_email = _norm_email(email)
        key = _identity_key(name, domain)
        if not norm_email and not key:
            return False

        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM suppression WHERE "
                "(%s IS NOT NULL AND email = %s) OR "
                "(%s IS NOT NULL AND identity = %s) LIMIT 1",
                (norm_email, norm_email, key, key),
            )
            return cur.fetchone() is not None

    def suppression_emails(self) -> set[str]:
        """Every suppressed address, normalised, as a set."""
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute("SELECT email FROM suppression WHERE email IS NOT NULL")
            out: set[str] = set()
            for (email,) in cur.fetchall():
                norm = _norm_email(email)
                if norm:
                    out.add(norm)
            return out

    def add_suppression(
        self,
        email: str | None,
        name: str | None,
        domain: str | None,
        source: str,
    ) -> None:
        """Append an opt-out row. No-op if neither an address nor a complete
        name+domain pair is derivable — matching the compliance semantics."""
        norm_email = _norm_email(email)
        identity = _identity_key(name, domain)
        if not norm_email and not identity:
            return
        # Store the identity in the same canonical form reads use.
        identity = _norm_identity(identity) if identity else None

        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO suppression (email, identity, source, added_at) "
                "VALUES (%s, %s, %s, %s)",
                (norm_email, identity, source or "", time.time()),
            )
        conn.commit()

    # -- audit log ------------------------------------------------------
    def log_lookup(self, record: dict) -> str:
        """Append one audit row; return its id as a string."""
        record = record or {}
        reasons = record.get("reasons") or []
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO lookups "
                "(ts, name, domain, local_part, linkedin_url, provider, reasons, ip_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    float(record.get("ts") or time.time()),
                    record.get("name"),
                    record.get("domain"),
                    record.get("local_part"),
                    record.get("linkedin_url"),
                    record.get("provider"),
                    self._jsonb(list(reasons)),
                    record.get("ip_hash"),
                ),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
        return str(new_id)

    def purge_lookups(self, older_than_days: int) -> int:
        """Delete audit rows older than ``older_than_days``; return the count."""
        cutoff = time.time() - int(older_than_days) * 86400.0
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM lookups WHERE ts < %s", (cutoff,))
            deleted = cur.rowcount
        conn.commit()
        return int(deleted or 0)

    def close(self) -> None:
        """Close the underlying connection if open."""
        conn = self._conn
        if conn is not None and not getattr(conn, "closed", True):
            conn.close()
        self._conn = None
