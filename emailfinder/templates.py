"""PURE literal template registry + the ordered global-prior list (dossier 1.2)
+ KB shape -> literal-template translation with single_token disambiguation.

The whole point of this module (and the fix for opengov=flast, trimble=underscore,
purplle=first.l) is that we store and render a LITERAL template string AND a
literal separator, never just a shape family. ``render`` turns one
``NameVariant`` into a concrete local part; ``global_priors`` loads the
dossier-1.2 seed list; ``template_for_kb`` translates a KB entry's learned
``dominant_shape`` + ``dominant_separator`` into a concrete (template, separator)
pair the renderer understands.

Zero I/O beyond reading the vendored ``data/global_priors.json`` seed. Depends
only on :mod:`emailfinder.models`.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .models import NameVariant

# The package's read-only seed data directory (mirrors config.PACKAGE_DATA_DIR
# without taking a dependency on config — templates depends only on models).
_DATA_DIR = Path(__file__).resolve().parent / "data"

# Concatenation templates (no structural separator char inside the name). We
# decompose them explicitly because a bare string like 'flast' cannot be split
# unambiguously by regex ('f'+'last' vs 'fl'+'ast').
_CONCAT_ATOMS: dict[str, list[str]] = {
    "first": ["first"],
    "last": ["last"],
    "flast": ["f", "last"],
    "lfirst": ["l", "first"],
    "firstlast": ["first", "last"],
    "lastfirst": ["last", "first"],
    "firstl": ["first", "l"],
    "lastf": ["last", "f"],
    "fl": ["f", "l"],
    "lf": ["l", "f"],
    "firstm": ["first", "m"],
    "firstmlast": ["first", "m", "last"],
}

_SEP_CHARS = "._-"


def _resolve_atom(atom: str, v: NameVariant) -> str | None:
    """Resolve one template atom against a variant. Returns None (never a
    fabricated token) when the required name part is missing.

    For a South-Indian ``first + initial`` variant (no surname but trailing
    initials, e.g. ``Ashwath S``) the ``last`` / ``l`` atoms fall back to the
    joined initials so ``first.last`` renders ``ashwath.s`` and ``first.l``
    renders ``ashwath.s``. :func:`render` blocks surname-leading templates for
    such variants so they never produce junk like ``sashwath``.
    """
    inits = "".join(v.initials) if v.initials else ""
    if atom == "first":
        return v.first or None
    if atom == "last":
        return v.last or (inits or None)
    if atom == "f":
        return v.first[0] if v.first else None
    if atom == "l":
        if v.last:
            return v.last[0]
        return inits[0] if inits else None
    if atom == "m":
        return v.middle[0][0] if v.middle and v.middle[0] else None
    if atom == "middle":
        return "".join(v.middle) if v.middle else None
    return None


def _template_atoms(template: str) -> tuple[list[str], bool] | None:
    """Return (atoms, separated) for a template, or None if unrecognised.

    ``separated`` True means the atoms are joined with the caller-supplied
    separator; False means they are concatenated directly.
    """
    if any(c in template for c in _SEP_CHARS):
        atoms = re.split(r"[._-]", template)
        atoms = [a for a in atoms if a]
        return (atoms, True) if atoms else None
    if template in _CONCAT_ATOMS:
        return _CONCAT_ATOMS[template], False
    return None


def render(template: str, v: NameVariant, sep: str) -> str | None:
    """Render one local part from a template + variant + separator.

    Examples: ``render('first.last', <ajith kumar>, '.') -> 'ajith.kumar'``;
    ``render('flast', <ajith kumar>, '') -> 'akumar'``;
    ``render('first_l', <ajith kumar>, '_') -> 'ajith_k'``.

    Returns ``None`` when the variant lacks a token the template requires (e.g.
    ``flast`` on a mononym), so mononyms never fabricate a surname or initial.
    """
    parsed = _template_atoms(template)
    if parsed is None:
        return None
    atoms, separated = parsed

    # For an initial-only variant (a South-Indian first + trailing initial, no
    # real surname) only allow first-leading templates. This yields the intended
    # first.<init> / first<init> / first forms and blocks surname-leading junk
    # (flast -> "sashwath", last.first -> "s.ashwath", ...).
    if (not v.last) and v.initials and atoms and atoms[0] != "first":
        return None

    parts: list[str] = []
    for atom in atoms:
        piece = _resolve_atom(atom, v)
        if not piece:
            return None
        parts.append(piece)

    join_sep = sep if separated else ""
    local = join_sep.join(parts).lower()
    return local or None


@lru_cache(maxsize=1)
def global_priors() -> list[tuple[str, str, float]]:
    """Load ``data/global_priors.json`` into the dossier-1.2 ordered list of
    ``(template, forced_separator, prior)`` tuples.

    The seed forces a dot as the primary separator and keeps underscore/hyphen
    near-zero unless the per-domain KB says otherwise:
    ``('first.last', '.', 0.60), ('flast', '', 0.12), ('first', '', 0.08), ...``
    """
    raw = json.loads((_DATA_DIR / "global_priors.json").read_text())
    return [(str(t), str(s), float(p)) for t, s, p in raw["priors"]]


def _disambiguate_single_token(no_bounce_locals: list[str]) -> str:
    """Disambiguate an ambiguous ``single_token`` KB shape into a concrete
    concatenation template using a sample of verified locals.

    ``single_token`` conflates flast (opengov ``achauhan``), bare-first, and
    firstlast. We use the average length of the verified all-alpha locals as a
    weak signal and fall back to ``flast`` per the dossier default.
    """
    sample = [loc for loc in no_bounce_locals if loc and loc.isalpha()][:25]
    if not sample:
        return "flast"
    avg = sum(len(loc) for loc in sample) / len(sample)
    if avg >= 10.0:          # long concatenations look like first+last
        return "firstlast"
    if avg <= 4.0:           # very short locals look like bare first names
        return "first"
    return "flast"           # dossier default (initial + surname)


def template_for_kb(kb_entry: dict) -> tuple[str, str]:
    """Translate a KB entry's ``dominant_shape`` + ``dominant_separator`` into a
    concrete ``(template, separator)`` the renderer understands.

    Normalises the sentinel ``'(none)'`` to an empty separator. Structured
    two-token shapes (``first.last``, ``first_last``, ``first.l``, ``f.last`` ...)
    map straight through, carrying their literal separator. ``multi*`` shapes
    collapse to first.last. The ambiguous ``single_token`` shape is resolved via
    a sample of ``no_bounce_locals`` (opengov ``achauhan`` -> ``flast``), and
    non-name shapes (``name+digits``, ``other``) fall back to the global
    first.last dot default.

    Examples: opengov -> ``('flast', '')``; trimble -> ``('first_last', '_')``;
    purplle -> ``('first.l', '.')``.
    """
    sep = kb_entry.get("dominant_separator") or ""
    if sep == "(none)":
        sep = ""
    shape = (kb_entry.get("dominant_shape") or "").strip()

    # The ambiguous single_token shape (note: its own label contains '_') is
    # resolved from a sample of verified locals before the structural branch.
    if shape == "single_token":
        return _disambiguate_single_token(kb_entry.get("no_bounce_locals") or []), ""

    # Structured two-token shapes carry their own separator inside the label
    # (first.last, first_last, first.l, f.last, multi.).
    if any(c in shape for c in _SEP_CHARS):
        parts = [p for p in re.split(r"[._-]", shape) if p != ""]
        if any(p == "multi" for p in parts) or len(parts) != 2:
            parts = ["first", "last"]
        template = sep.join(parts) if sep else "".join(parts)
        return template, sep

    # name+digits (never fabricate digits), other, or empty -> global default.
    return "first.last", "."
