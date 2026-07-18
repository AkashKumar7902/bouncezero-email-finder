"""I/O: a single sqlite store backing two caches (dossier 5 + 6.1).

Two tables live in one ``cache.sqlite`` file inside the per-user silo:

1. ``domain_fp`` — the per-domain fingerprint (provider / MX list /
   ``is_catch_all`` tri-state / learned template+separator / ``last_probed_at``).
   Resolved ONCE per distinct domain across a batch and reused via
   :meth:`Cache.get_domain`, subject to a TTL so a stale verdict re-resolves.

2. ``api_cache`` — a sha256-keyed provider-response cache. It is MANDATORY in
   front of any paid verifier/finder so a cache hit never triggers a second
   billed call for the same normalized input (dossier 6.1: MillionVerifier has
   no server-side dedupe).

stdlib only (sqlite3, json, hashlib, time). Depends solely on
:mod:`emailfinder.models` for the ``DomainFingerprint`` / ``Provider`` types.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .models import DomainFingerprint, Provider

_SECONDS_PER_DAY = 86400

# ``is_catch_all`` is tri-state: None (unknown) must survive a round-trip
# distinctly from False, so we serialize it as a nullable INTEGER (NULL / 0 / 1).


class Cache:
    """SQLite-backed domain-fingerprint + provider-response cache."""

    def __init__(self, path: Path) -> None:
        """Open (creating if absent) the sqlite file and ensure both tables exist.

        ``path``'s parent silo directory is created if it does not yet exist.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False keeps this usable from a batch worker pool;
        # the engine owns the single Cache instance and serializes writes.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS domain_fp (
                domain            TEXT PRIMARY KEY,
                provider          TEXT NOT NULL,
                mx                TEXT NOT NULL,          -- JSON array of hosts
                is_catch_all      INTEGER,               -- NULL/0/1 tri-state
                learned_template  TEXT,
                learned_separator TEXT,
                last_probed_at    REAL NOT NULL,
                flags             TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS api_cache (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,                 -- JSON object
                stored_at  REAL NOT NULL
            );
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------ domain
    def get_domain(
        self, domain: str, ttl_days: int = 14
    ) -> DomainFingerprint | None:
        """Return a non-expired cached :class:`DomainFingerprint`, else ``None``.

        A row older than ``ttl_days`` (relative to its ``last_probed_at``) is
        treated as a miss so the domain is re-resolved. Lookup is
        case-insensitive on the domain.
        """
        key = _norm_domain(domain)
        row = self._conn.execute(
            "SELECT * FROM domain_fp WHERE domain = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if _expired(row["last_probed_at"], ttl_days):
            return None
        return _row_to_fingerprint(row)

    def put_domain(self, fp: DomainFingerprint) -> None:
        """Upsert ``fp``, stamping ``last_probed_at`` with the current time.

        The MX list is serialized as JSON and the provider as its enum
        ``.value`` so rows round-trip losslessly.
        """
        now = time.time()
        fp.last_probed_at = now
        self._conn.execute(
            """
            INSERT INTO domain_fp
                (domain, provider, mx, is_catch_all, learned_template,
                 learned_separator, last_probed_at, flags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                provider          = excluded.provider,
                mx                = excluded.mx,
                is_catch_all      = excluded.is_catch_all,
                learned_template  = excluded.learned_template,
                learned_separator = excluded.learned_separator,
                last_probed_at    = excluded.last_probed_at,
                flags             = excluded.flags
            """,
            (
                _norm_domain(fp.domain),
                _provider_value(fp.provider),
                json.dumps(list(fp.mx)),
                _tri_to_int(fp.is_catch_all),
                fp.learned_template,
                fp.learned_separator,
                now,
                json.dumps(fp.flags or {}),
            ),
        )
        self._conn.commit()

    # --------------------------------------------------------------- api cache
    @staticmethod
    def api_key(provider: str, normalized_input: str) -> str:
        """sha256(provider + normalized_input) idempotency key (hex digest).

        Callers MUST normalize ``normalized_input`` identically on every call
        (e.g. lowercased email) so repeat lookups collide and never re-bill.
        """
        digest = hashlib.sha256()
        digest.update(provider.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(normalized_input.encode("utf-8"))
        return digest.hexdigest()

    def get_api(self, key: str, ttl_days: int) -> dict | None:
        """Return a cached provider response dict, or ``None`` on miss/expiry."""
        row = self._conn.execute(
            "SELECT value, stored_at FROM api_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if _expired(row["stored_at"], ttl_days):
            return None
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return None

    def put_api(self, key: str, value: dict, ttl_days: int) -> None:
        """Store a provider response under ``key`` stamped with the current time.

        ``ttl_days`` is accepted for signature symmetry with :meth:`get_api`;
        expiry is evaluated at read time against ``stored_at``.
        """
        self._conn.execute(
            """
            INSERT INTO api_cache (key, value, stored_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value     = excluded.value,
                stored_at = excluded.stored_at
            """,
            (key, json.dumps(value), time.time()),
        )
        self._conn.commit()

    # -------------------------------------------------------------- lifecycle
    def purge_expired(self, domain_ttl_days: int, api_ttl_days: int) -> int:
        """Delete rows older than their TTL; return the number of rows purged."""
        cutoff_domain = time.time() - domain_ttl_days * _SECONDS_PER_DAY
        cutoff_api = time.time() - api_ttl_days * _SECONDS_PER_DAY
        cur = self._conn.execute(
            "DELETE FROM domain_fp WHERE last_probed_at < ?", (cutoff_domain,)
        )
        purged = cur.rowcount or 0
        cur = self._conn.execute(
            "DELETE FROM api_cache WHERE stored_at < ?", (cutoff_api,)
        )
        purged += cur.rowcount or 0
        self._conn.commit()
        return purged

    def close(self) -> None:
        """Close the underlying sqlite connection."""
        self._conn.close()


# --------------------------------------------------------------------- helpers
def _norm_domain(domain: str) -> str:
    return (domain or "").strip().lower()


def _expired(stored_at: float, ttl_days: int) -> bool:
    return (time.time() - stored_at) > ttl_days * _SECONDS_PER_DAY


def _tri_to_int(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def _int_to_tri(value) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _provider_value(provider) -> str:
    """Accept a Provider enum or a bare string; return its ``.value``."""
    if isinstance(provider, Provider):
        return provider.value
    return str(provider)


def _provider_from_value(value: str) -> Provider:
    try:
        return Provider(value)
    except ValueError:
        return Provider.NONE_UNKNOWN


def _row_to_fingerprint(row: sqlite3.Row) -> DomainFingerprint:
    try:
        mx = json.loads(row["mx"]) or []
    except (ValueError, TypeError):
        mx = []
    try:
        flags = json.loads(row["flags"]) or {}
    except (ValueError, TypeError):
        flags = {}
    return DomainFingerprint(
        domain=row["domain"],
        provider=_provider_from_value(row["provider"]),
        mx=list(mx),
        is_catch_all=_int_to_tri(row["is_catch_all"]),
        learned_template=row["learned_template"],
        learned_separator=row["learned_separator"],
        last_probed_at=row["last_probed_at"],
        flags=flags,
    )
