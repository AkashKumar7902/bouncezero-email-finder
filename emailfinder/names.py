"""PURE India-aware name-variant expansion (research dossier 1.3-1.5).

Splits a *cleaned* full-name string into a :class:`ParsedName`, then generates
an ordered, deduplicated list of :class:`NameVariant` forms:

* **nicknames** — bidirectional table lookup (Bob<->Robert, Bill<->William...);
* **compound / hyphenated surnames** — join-all / last-token / first-token
  (``Van Der Berg`` -> ``vanderberg`` / ``berg`` / ``van``; ``Smith-Jones`` ->
  ``smithjones`` / ``smith`` / ``jones``);
* **drop-middle** (weighted highest) plus a first.middle.last keep-middle form;
* **South-Indian first + initial(s)** — LinkedIn shows a trailing initial that
  expands to a father's/village name; we emit ``first.<init>``, ``first<init>``
  and ``first``-only (``Ashwath S`` -> ``ashwath.s`` / ``ashwaths`` / ``ashwath``);
* **mononyms** — a lone given name yields only a ``first``-only variant.

Hard invariants (dossier 1.5): this module NEVER fabricates a surname for a
mononym and NEVER appends digits. Only the tokens actually present in the input
(and their vetted nickname alternates) are ever used.

Depends only on :mod:`emailfinder.models` and :mod:`emailfinder.normalize`
(a pure peer, no network I/O).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from emailfinder.models import NameVariant, ParsedName
from emailfinder.normalize import strip_titles_suffixes, to_ascii

# Surname particles (Dutch/German/Portuguese/Spanish/Arabic/Celtic...). When one
# appears, it and everything after it up to the end of the name form a compound
# surname (dossier 1.4). Deliberately conservative: only well-established
# particles so ordinary given/last names are not misclassified.
_PARTICLES = frozenset(
    {
        "van", "von", "der", "den", "de", "del", "della", "di", "da", "das",
        "dos", "du", "la", "le", "bin", "ibn", "mac", "mc", "san", "santa",
        "st", "ter", "ten", "vander",
    }
)

# Keep lowercase letters/digits and hyphens (so a hyphenated surname survives
# long enough to be split); everything else (apostrophes, dots, ...) is dropped.
_LIGHT_CLEAN = re.compile(r"[^a-z0-9-]")


def _light_clean(tok: str) -> str:
    """Lowercase/ASCII-fold a single raw token but PRESERVE internal hyphens.

    Unlike :func:`normalize.clean_token` (which strips hyphens too) this keeps a
    hyphenated surname joinable, so ``Smith-Jones`` -> ``smith-jones`` rather than
    collapsing to ``smithjones`` before we can emit the ``smith`` / ``jones``
    alternates. Leading/trailing and doubled hyphens are trimmed.
    """
    folded = to_ascii(tok)  # already lowercased, diacritics removed
    cleaned = _LIGHT_CLEAN.sub("", folded)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned


def _dedup(seq: list[str]) -> list[str]:
    """Order-preserving de-duplication, dropping empties."""
    out: list[str] = []
    for item in seq:
        if item and item not in out:
            out.append(item)
    return out


def parse_name(name: str) -> ParsedName:
    """Tokenize a (possibly still messy) full name into a :class:`ParsedName`.

    Steps: ASCII-fold + lowercase (via :func:`normalize.to_ascii`), split on
    whitespace, strip honorific titles/suffixes, then classify each token —

    * single-letter tokens become **initials** (South-Indian pattern);
    * a hyphenated token or a surname *particle* (``van``/``de``/...) marks a
      **compound surname**, whose component tokens are stored in
      ``extra_tokens`` for :func:`expand_variants`;
    * a single remaining name token => ``is_mononym=True`` (``last is None``).

    Never fabricates tokens; ``extra_tokens`` is non-empty only when the surname
    genuinely has more than one component.
    """
    raw = name or ""
    tokens = strip_titles_suffixes(to_ascii(raw).split())

    name_tokens: list[str] = []
    initials: list[str] = []
    for tok in tokens:
        cleaned = _light_clean(tok)
        if not cleaned:
            continue
        if "-" in cleaned:
            name_tokens.append(cleaned)  # hyphenated surname — split later
        elif len(cleaned) == 1:
            initials.append(cleaned)
        else:
            name_tokens.append(cleaned)

    # Degenerate inputs: nothing but initials, or empty.
    if not name_tokens:
        if initials:
            return ParsedName(
                raw=raw, first="".join(initials), last=None,
                middle=[], initials=[], is_mononym=True, extra_tokens=[],
            )
        return ParsedName(
            raw=raw, first=None, last=None,
            middle=[], initials=[], is_mononym=False, extra_tokens=[],
        )

    first: str | None
    last: str | None
    middle: list[str]
    components: list[str]
    is_mononym = False

    hy_idx = next((i for i, t in enumerate(name_tokens) if "-" in t), None)
    particle_idx = next(
        (i for i, t in enumerate(name_tokens) if t in _PARTICLES), None
    )

    if hy_idx is not None:
        # A hyphenated token is the (compound) surname.
        components = [p for p in name_tokens[hy_idx].split("-") if p]
        before = name_tokens[:hy_idx]
        after = name_tokens[hy_idx + 1:]
        if before:
            first = before[0]
            middle = before[1:] + after
        else:
            first = None
            middle = after
        last = components[-1] if components else None
    elif particle_idx is not None:
        # Particle-led compound surname: particle .. end are the surname.
        if particle_idx == 0:
            first = None
            middle = []
            components = list(name_tokens)
        else:
            first = name_tokens[0]
            middle = name_tokens[1:particle_idx]
            components = name_tokens[particle_idx:]
        last = components[-1] if components else None
    elif len(name_tokens) == 1:
        first = name_tokens[0]
        last = None
        middle = []
        components = []
        # A lone name token is a mononym only when no initials accompany it;
        # 'Ashwath S' is a first + initial, not a mononym.
        is_mononym = not initials
    else:
        first = name_tokens[0]
        last = name_tokens[-1]
        middle = name_tokens[1:-1]
        components = [last]

    extra_tokens = components if len(components) > 1 else []
    return ParsedName(
        raw=raw,
        first=first,
        last=last,
        middle=middle,
        initials=initials,
        is_mononym=is_mononym,
        extra_tokens=extra_tokens,
    )


def _surname_forms(pn: ParsedName) -> list[str]:
    """Ordered surname alternatives for ``pn``.

    Compound surname (``extra_tokens`` set): join-all, last-token, first-token.
    Simple surname: just ``[last]``. Mononym / all-initials: empty.
    """
    if pn.extra_tokens:
        comps = pn.extra_tokens
        return _dedup(["".join(comps), comps[-1], comps[0]])
    if pn.last:
        return [pn.last]
    return []


def expand_variants(
    pn: ParsedName, nickname_table: dict[str, list[str]]
) -> list[NameVariant]:
    """Deduped, ordered variant set (as-given first) for a parsed name.

    Emits, per first-name form (as-given, then each bidirectional nickname):
    the ``first.last`` (drop-middle) form, the keep-middle ``first.middle.last``
    form, any compound-surname alternates, the South-Indian first + initial(s)
    form, and a ``first``-only fallback. For an all-surname input (e.g. a bare
    ``Van Der Berg``) only the surname alternates are emitted.

    NEVER invents a surname for a mononym and NEVER appends digits — every token
    used comes from the input or the vetted nickname table.
    """
    variants: list[NameVariant] = []
    seen: set[tuple] = set()

    def add(
        first: str | None,
        last: str | None,
        middle: list[str],
        initials: list[str],
        origin: str,
    ) -> None:
        key = (first, last, tuple(middle), tuple(initials))
        if key in seen:
            return
        seen.add(key)
        variants.append(
            NameVariant(
                first=first,
                last=last,
                middle=list(middle),
                initials=list(initials),
                origin=origin,
            )
        )

    surname_forms = _surname_forms(pn)

    # All-surname input (no given first name): emit surname alternates only.
    if pn.first is None:
        for sform in surname_forms:
            add(sform, None, [], [], "surname_expansion")
        return variants

    # Build first-name forms: as-given, then bidirectional nicknames.
    first_forms: list[tuple[str, str]] = [(pn.first, "as_given")]
    for alt in nickname_table.get(pn.first, []):
        first_forms.append((alt, "nickname"))

    primary_last = surname_forms[0] if surname_forms else None

    for fv, forigin in first_forms:
        # 1. first.last (drop middle) — highest-weight canonical form; or, when
        #    there is no surname at all, the first-only form.
        if primary_last is not None:
            add(fv, primary_last, [], [], forigin)
        else:
            add(fv, None, [], [], "mononym" if pn.is_mononym else forigin)

        # 2. keep-middle first.middle.last form.
        if pn.middle and pn.last:
            add(fv, pn.last, pn.middle, [], forigin)

        # 3. compound-surname alternates (last-token, first-token, ...).
        for sform in surname_forms[1:]:
            add(fv, sform, [], [], "surname_expansion")

        # 4. South-Indian first + initial(s).
        if pn.initials:
            add(fv, None, [], pn.initials, "first_initial")

        # 5. first-only fallback (also the sole form for a mononym).
        add(fv, None, [], [], "mononym" if pn.is_mononym else forigin)

    return variants


@lru_cache(maxsize=None)
def _load_nicknames_cached(path_str: str) -> dict[str, tuple[str, ...]]:
    """Read the vendored nickname JSON and build a bidirectional index (cached).

    Every member of every group maps to the union of all *other* members across
    every group it appears in, so a lookup works in both directions (bob ->
    robert AND robert -> bob). Tuples are stored so the cached value is
    hashable/immutable; :func:`load_nicknames` returns list copies.
    """
    data = json.loads(Path(path_str).read_text(encoding="utf-8"))
    index: dict[str, list[str]] = {}
    for group in data.get("groups", []):
        for member in group:
            bucket = index.setdefault(member, [])
            for other in group:
                if other != member and other not in bucket:
                    bucket.append(other)
    return {name: tuple(others) for name, others in index.items()}


def load_nicknames(path: Path) -> dict[str, list[str]]:
    """Load the vendored bidirectional nickname table into a lookup (cached).

    Returns ``{name: [other_names...]}`` usable in both directions. The result is
    freshly copied per call so callers may mutate it safely without disturbing
    the cache.
    """
    cached = _load_nicknames_cached(str(Path(path)))
    return {name: list(others) for name, others in cached.items()}
