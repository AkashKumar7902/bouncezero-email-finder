"""I/O: load / lookup / upsert the domain knowledge base (KB).

The KB is the audit-derived, self-improving memory of what email pattern each
domain actually uses (dominant shape + separator + provider) plus the banked
outcomes of prior sends (``no_bounce_locals`` / ``known_bad_locals``).

Layout
------
The packaged seed at ``emailfinder/data/domain_kb.seed.json`` ships EMPTY (``{}``)
— no real contacts or outcomes are bundled. On first run it is copied into a
per-user overlay in that user's silo, and every runtime upsert (from ``rescore``
/ ``confirm`` on the user's OWN data) writes to that overlay only. The packaged
seed is never mutated (research dossier section 5, the feedback loop). A user can
also drop their own pre-built KB into the overlay path returned by
``Compliance.silo_paths()['kb']``.

In-memory vs on-disk representation
-----------------------------------
So callers get a convenient, dedup-friendly structure while the file stays
schema-identical to the seed, two fields are transcoded at the load/save edges:

* ``known_bad_locals`` / ``no_bounce_locals`` are Python ``set`` in memory and
  serialize to **sorted lists** on disk.
* ``dominant_separator`` is the empty string ``""`` in memory (so renderers can
  use it directly) and serializes to the sentinel ``"(none)"`` on disk.

Both transforms are exact inverses, so ``load -> save -> load`` is lossless.

Zero third-party deps: stdlib ``json`` / ``os`` / ``tempfile`` only, plus the
pure ``emailfinder.shapes`` taxonomy so re-scored rows share the seed's shapes.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from .models import Provider
from .shapes import shape

# Fields stored as sorted JSON lists on disk but handled as sets in memory.
_LOCAL_SET_FIELDS = ("known_bad_locals", "no_bounce_locals")

# On-disk sentinel for "no separator" (dominant_separator); "" in memory.
_SEP_NONE = "(none)"

# Separator characters that can be embedded in a structural shape label.
_SEP_CHARS = "._-"
# Non-structural shape labels carry NO separator. Note "single_token" literally
# contains an underscore, so it must be excluded explicitly.
_NO_SEP_SHAPES = frozenset({"single_token", "name+digits", "other", ""})


def _sep_from_shape(shape_label: str) -> str:
    """Return the literal separator embedded in a STRUCTURAL shape label
    (``first_l`` -> ``_``, ``first.last`` -> ``.``); ``""`` for single_token /
    name+digits / other (the "_" in "single_token" is part of the word, not a
    separator)."""
    label = shape_label or ""
    if label in _NO_SEP_SHAPES:
        return ""
    for ch in _SEP_CHARS:
        if ch in label:
            return ch
    return ""

# Reason-class keys that also exist as top-level integer counters in the schema.
_TOP_LEVEL_COUNTERS = ("address_not_found", "recipient_rejected", "inactive_account")


# --------------------------------------------------------------------------- #
# Domain / local normalization
# --------------------------------------------------------------------------- #
def _norm_domain(domain: str | None) -> str:
    """Canonicalize a domain for KB keys/lookups: lowercase, no trailing dot."""
    return (domain or "").strip().lower().rstrip(".")


def _norm_local(local: str | None) -> str:
    """Canonicalize a local part: lowercase, whitespace-stripped."""
    return (local or "").strip().lower()


def _provider_value(provider) -> str:
    """Return the audit/on-disk string for a Provider (or a plain string)."""
    return getattr(provider, "value", provider)


# --------------------------------------------------------------------------- #
# Encode / decode between the on-disk schema and the in-memory representation
# --------------------------------------------------------------------------- #
def _decode_entry(entry: dict) -> dict:
    """Copy one on-disk entry into its in-memory form.

    Lists -> sets for the two locals fields; ``"(none)"`` -> ``""`` for the
    dominant separator. A shallow copy is made so the source dict (and thus the
    packaged seed, if this ever ran over it) is never mutated.
    """
    e = dict(entry)
    for f in _LOCAL_SET_FIELDS:
        vals = e.get(f) or []
        e[f] = {_norm_local(v) for v in vals if _norm_local(v)}
    if e.get("dominant_separator") == _SEP_NONE:
        e["dominant_separator"] = ""
    return e


def _encode_entry(entry: dict) -> dict:
    """Copy one in-memory entry back into its on-disk form.

    Sets (or any iterable) -> sorted lists for the two locals fields;
    ``""`` -> ``"(none)"`` for the dominant separator. Exact inverse of
    ``_decode_entry`` so the round-trip is lossless.
    """
    e = dict(entry)
    for f in _LOCAL_SET_FIELDS:
        vals = e.get(f)
        if vals is None:
            e[f] = []
        else:
            e[f] = sorted({_norm_local(v) for v in vals if _norm_local(v)})
    if e.get("dominant_separator", None) == "":
        e["dominant_separator"] = _SEP_NONE
    return e


def _decode(raw: dict) -> dict[str, dict]:
    """Decode a whole raw KB dict (keys lowercased)."""
    return {_norm_domain(dom): _decode_entry(entry) for dom, entry in raw.items()}


def _encode(kb: dict) -> dict:
    """Encode a whole in-memory KB back to the on-disk schema."""
    return {dom: _encode_entry(entry) for dom, entry in kb.items()}


# --------------------------------------------------------------------------- #
# Atomic write
# --------------------------------------------------------------------------- #
def _atomic_write_json(path: Path, obj: dict) -> None:
    """Serialize ``obj`` to ``path`` atomically (write tmp + fsync + rename).

    A crash mid-write leaves the pre-existing file untouched: the new bytes go
    to a sibling temp file that is only ``os.replace``-d into place once fully
    written, and the temp file is removed on any failure. ``os.replace`` is an
    atomic rename on the same filesystem.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=1, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# New-entry template
# --------------------------------------------------------------------------- #
def _new_entry(domain: str) -> dict:
    """Return a fresh, schema-identical KB entry for an unseen domain."""
    return {
        "company": "",
        "total_addresses": 0,
        "provider": Provider.NONE_UNKNOWN.value,
        "mx": [],
        "dominant_separator": "",
        "dominant_shape": "",
        "shape_distribution": {},
        "reason_classes": {},
        "no_bounce_found": 0,
        "address_not_found": 0,
        "recipient_rejected": 0,
        "inactive_account": 0,
        "known_bad_locals": set(),
        "no_bounce_locals": set(),
    }


def _ensure_entry(kb: dict, domain: str) -> dict:
    """Return the live (mutable) entry for ``domain``, creating it if absent.

    The returned dict is the one stored in ``kb`` (case-insensitive), so callers
    mutate the KB in place; a subsequent ``save_kb``/atomic write persists it.
    """
    entry = get_entry(kb, domain)
    if entry is None:
        entry = _new_entry(_norm_domain(domain))
        kb[_norm_domain(domain)] = entry
    # Defensively coerce the set-fields in case a caller injected list forms.
    for f in _LOCAL_SET_FIELDS:
        if not isinstance(entry.get(f), set):
            entry[f] = {_norm_local(v) for v in (entry.get(f) or []) if _norm_local(v)}
    entry.setdefault("shape_distribution", {})
    entry.setdefault("reason_classes", {})
    return entry


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def load_kb(path: Path, seed_path: Path) -> dict[str, dict]:
    """Load the per-user KB overlay, copying the packaged seed on first run.

    On the first run (``path`` absent) the read-only ``seed_path`` is copied
    verbatim into the overlay so the packaged seed is never mutated. The overlay
    is then read and decoded (``"(none)"`` -> ``""``; lists -> sets). Returns an
    empty dict only when BOTH the overlay and the seed are absent.
    """
    path = Path(path)
    seed_path = Path(seed_path)

    if not path.exists():
        if seed_path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Verbatim copy: the seed file's bytes are never altered.
            shutil.copyfile(str(seed_path), str(path))
        else:
            return {}

    raw = json.loads(path.read_text(encoding="utf-8"))
    return _decode(raw)


def get_entry(kb: dict, domain: str) -> dict | None:
    """Case-insensitive domain lookup. Returns the live entry dict or None."""
    key = _norm_domain(domain)
    if not key:
        return None
    entry = kb.get(key)
    if entry is not None:
        return entry
    # Fallback scan in case a caller populated non-normalized keys.
    for k, v in kb.items():
        if isinstance(k, str) and k.strip().lower().rstrip(".") == key:
            return v
    return None


def upsert_verified(
    kb: dict,
    path: Path,
    domain: str,
    template: str,
    separator: str,
    provider: Provider,
    example_local: str,
) -> None:
    """Fold a DELIVERABLE/confirmed result back into the KB (feedback loop).

    Refreshes the domain's dominant template (as the audit shape of the verified
    local), separator and provider; appends ``example_local`` to
    ``no_bounce_locals`` (deduped); and bumps ``shape_distribution`` via
    ``shapes.shape`` when the example is newly seen. Persisted atomically.

    ``template`` is accepted for signature stability; the canonical, schema-
    consistent storage is the *shape* of ``example_local`` (so ``dominant_shape``
    always matches the ``shape_distribution`` keys). ``separator`` is stored as
    ``dominant_separator`` (``""`` in memory, ``"(none)"`` on disk).
    """
    entry = _ensure_entry(kb, domain)
    local = _norm_local(example_local)

    shp, _shp_sep = shape(local) if local else ("", "")
    entry["provider"] = _provider_value(provider)

    if local and local not in entry["no_bounce_locals"]:
        entry["no_bounce_locals"].add(local)
        if shp:
            dist = entry["shape_distribution"]
            dist[shp] = int(dist.get(shp, 0)) + 1
        entry["no_bounce_found"] = int(entry.get("no_bounce_found", 0)) + 1
        entry["total_addresses"] = int(entry.get("total_addresses", 0)) + 1

    # Promote the dominant pattern to the ARGMAX of the shape distribution — NOT
    # simply the last verified local's shape. Setting it to a one-off minority
    # shape would desync dominant_shape from shape_distribution and make the
    # ranker drop the true dominant pattern (a single verified local must not
    # override 59 observed first.last examples). The separator is derived from
    # that same argmax shape so the two always agree.
    dist = entry.get("shape_distribution") or {}
    if dist:
        argmax_shape = max(dist.items(), key=lambda kv: int(kv[1]))[0]
        entry["dominant_shape"] = argmax_shape
        entry["dominant_separator"] = _sep_from_shape(argmax_shape)
    elif shp:
        entry["dominant_shape"] = shp
        entry["dominant_separator"] = _norm_sep_in(separator, _shp_sep)

    save_kb(kb, path)


def append_known_bad(kb: dict, path: Path, domain: str, local: str, source: str) -> None:
    """Bank a confirmed not-found / DBEB-M365 local as ``known_bad``.

    On a true not-found (5.1.1 / 5.1.10 / address_not_found) or an M365 DBEB
    5.4.1, the local is deduped-appended to ``known_bad_locals`` so future
    guesses of it are forced UNDELIVERABLE. ``source`` (a reason-class such as
    ``address_not_found`` / ``recipient_rejected``) is recorded into the schema's
    ``reason_classes`` counter, and the matching top-level counter when one
    exists. Persisted atomically. No-op for an empty local.
    """
    local = _norm_local(local)
    if not local:
        return
    entry = _ensure_entry(kb, domain)

    if local not in entry["known_bad_locals"]:
        entry["known_bad_locals"].add(local)
        # Also drop it from the good set if it was ever banked as no-bounce.
        entry["no_bounce_locals"].discard(local)
        if source:
            rc = entry["reason_classes"]
            rc[source] = int(rc.get(source, 0)) + 1
            if source in _TOP_LEVEL_COUNTERS:
                entry[source] = int(entry.get(source, 0)) + 1

    save_kb(kb, path)


def save_kb(kb: dict, path: Path) -> None:
    """Atomically serialize the KB to the silo (sets->sorted lists, ""->'(none)')."""
    _atomic_write_json(Path(path), _encode(kb))


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _norm_sep_in(separator: str | None, shape_sep: str) -> str:
    """Normalize an incoming separator to the in-memory form.

    ``"(none)"`` and ``None`` collapse to ``""``. An explicit separator wins;
    when it is empty we defer to the separator implied by the verified local's
    shape (they agree for well-formed calls).
    """
    if separator is None or separator == _SEP_NONE:
        return "" if separator == _SEP_NONE else (shape_sep or "")
    if separator == "":
        return shape_sep or ""
    return separator
