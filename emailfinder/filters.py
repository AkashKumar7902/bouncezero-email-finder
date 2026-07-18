"""PURE suppression / flagging filters from vendored static JSON.

Four independent, side-effect-free checks the engine layers on top of candidate
generation (research dossier sections 1.6 and 5):

* ``is_role_local``    — drop role / functional locals (info, hr, careers, ...)
  from *person* guessing.
* ``is_disposable_domain`` — flag throwaway domains.
* ``is_webmail``       — flag consumer webmail (gmail/yahoo/...): still possibly
  deliverable, but status stays UNKNOWN and the flag is surfaced.
* ``in_known_bad``     — a local part the domain's KB has already banked as a
  confirmed bounce; the caller forces UNDELIVERABLE (the audit's #1
  bounce-cause killer).

Everything here is read-only against sets loaded once from the packaged data
dir. No network, no scoring, no I/O beyond the one-time cached JSON load.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

# Filenames under the package data dir, each a flat JSON array of strings.
_ROLE_FILE = "role_locals.json"
_DISPOSABLE_FILE = "disposable_domains.json"
_WEBMAIL_FILE = "webmail_domains.json"


def _norm_local(local: str) -> str:
    """Canonicalize a local part for comparison: lowercase, whitespace-stripped."""
    return (local or "").strip().lower()


def _norm_domain(domain: str) -> str:
    """Canonicalize a domain for comparison: lowercase, strip whitespace and a
    trailing FQDN dot."""
    return (domain or "").strip().lower().rstrip(".")


def is_role_local(local: str, role_set: set[str]) -> bool:
    """Return True when ``local`` is a role / functional mailbox name.

    Matches info/hr/careers/hiring/sales/support/admin/engineering/recruiting
    and friends (whatever is vendored in ``role_locals.json``). Such locals are
    removed from person-guessing because they are shared functional addresses,
    not individuals (dossier 1.6). Empty / whitespace input is never a role.
    """
    norm = _norm_local(local)
    if not norm:
        return False
    return norm in role_set


def is_disposable_domain(domain: str, disposable_set: set[str]) -> bool:
    """Return True when ``domain`` is a known throwaway / disposable mail domain.

    Plain membership check against the vendored disposable-domains list
    (~8000 entries). Case-insensitive; a trailing dot is ignored.
    """
    norm = _norm_domain(domain)
    if not norm:
        return False
    return norm in disposable_set


def is_webmail(domain: str, webmail_set: set[str]) -> bool:
    """Return True when ``domain`` is consumer webmail (gmail.com, yahoo.com,
    outlook.com, ...).

    Webmail hits set the ``webmail`` flag: the address may still be deliverable,
    but pattern inference is meaningless there so status stays UNKNOWN.
    """
    norm = _norm_domain(domain)
    if not norm:
        return False
    return norm in webmail_set


def in_known_bad(local: str, kb_entry: dict | None) -> bool:
    """Return True when ``local`` is in the domain KB's ``known_bad_locals``.

    These are locals a prior bounce/rescore run confirmed as not-found (or a
    DBEB-M365 5.4.1); the caller forces the candidate to UNDELIVERABLE. Missing
    entry or missing/empty list -> False. Comparison is case-insensitive.
    """
    if not kb_entry:
        return False
    bad = kb_entry.get("known_bad_locals")
    if not bad:
        return False
    return _norm_local(local) in {_norm_local(b) for b in bad}


@lru_cache(maxsize=None)
def _load_static_sets_cached(data_dir_key: str) -> dict[str, frozenset[str]]:
    """Cache-backed loader keyed on the resolved data-dir string.

    Returns immutable frozensets so the cached value can never be mutated by a
    caller and leak across calls. Missing files degrade to empty sets rather
    than raising, so a partial data dir is still usable.
    """
    base = Path(data_dir_key)
    result: dict[str, frozenset[str]] = {}
    for key, filename in (
        ("role", _ROLE_FILE),
        ("disposable", _DISPOSABLE_FILE),
        ("webmail", _WEBMAIL_FILE),
    ):
        path = base / filename
        if not path.exists():
            result[key] = frozenset()
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        norm = _norm_local if key == "role" else _norm_domain
        result[key] = frozenset(norm(x) for x in raw if isinstance(x, str) and norm(x))
    return result


def load_static_sets(data_dir: Path) -> dict[str, set[str]]:
    """Load role / disposable / webmail sets from ``data_dir`` (cached per dir).

    Returns ``{"role": set, "disposable": set, "webmail": set}``. The underlying
    load is memoized by resolved path, so repeated calls (e.g. once per batch
    row) are free. Fresh ``set`` copies are returned so a caller mutating the
    result cannot corrupt the cache.
    """
    key = str(Path(data_dir).expanduser().resolve())
    cached = _load_static_sets_cached(key)
    return {name: set(members) for name, members in cached.items()}
