"""PURE token normalization + LOCAL-ONLY LinkedIn slug parsing.

Implements the dossier 1.1-1.3 clean steps (Unicode NFKD -> ASCII fold,
lowercase, punctuation/title/suffix stripping) and the clean-room red line
from dossier 8.1: a LinkedIn profile URL is only ever *slug-parsed locally*
with :mod:`urllib.parse` — this module performs **ZERO network I/O** by
construction (enforced by the import-guard test) and never touches
``urllib.request``/``socket``/``http``.

Transliteration uses stdlib :mod:`unicodedata` NFKD by default; if the optional
``anyascii`` or ``text-unidecode`` packages are importable they are used for
richer transliteration, with a graceful stdlib fallback when they are absent.
"""
from __future__ import annotations

import re
import unicodedata
import urllib.parse

__all__ = [
    "to_ascii",
    "strip_titles_suffixes",
    "clean_token",
    "is_linkedin_url",
    "parse_linkedin_slug",
    "slug_to_name",
    "romanizations",
]

# Honorific titles and generational/credential suffixes stripped from a name
# token list (dossier 1.3 step 2). Compared case-insensitively against the
# token reduced to its bare letters, so "Dr." / "Jr." / "III" all match.
_TITLES = frozenset(
    {"dr", "mr", "mrs", "ms", "miss", "mx", "prof", "professor", "sir", "madam"}
)
_SUFFIXES = frozenset(
    {"jr", "sr", "ii", "iii", "iv", "phd", "md", "esq", "dds", "dvm", "jd", "mba"}
)

_NON_ALNUM = re.compile(r"[^a-z0-9]")
_LETTERS_ONLY = re.compile(r"[^a-z]")


def to_ascii(text: str) -> str:
    """NFKD-decompose, drop combining marks and transliterate to lowercase ASCII.

    ``José`` -> ``jose``, ``Müller`` -> ``muller``, ``Nguyễn`` -> ``nguyen``.
    Prefers the optional ``anyascii`` / ``text-unidecode`` transliterators when
    importable (imported lazily so they remain optional); otherwise folds via
    stdlib :func:`unicodedata.normalize` ``NFKD`` and strips combining marks.
    """
    if not text:
        return ""

    folded: str | None = None
    # Optional richer transliteration — imported lazily, never at module top.
    try:  # anyascii is the preferred optional dependency
        from anyascii import anyascii  # type: ignore

        folded = anyascii(text)
    except Exception:
        folded = None
    if folded is None:
        try:  # text-unidecode exposes unidecode()
            from text_unidecode import unidecode  # type: ignore

            folded = unidecode(text)
        except Exception:
            folded = None
    if folded is None:
        # stdlib fallback: decompose then drop combining marks.
        decomposed = unicodedata.normalize("NFKD", text)
        folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))

    # Force to strict ASCII; anything non-representable is dropped.
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    return ascii_only.lower().strip()


def strip_titles_suffixes(tokens: list[str]) -> list[str]:
    """Remove honorific titles (Dr/Mr/Ms/Prof...) and suffixes (Jr/Sr/II/III/IV/PhD).

    Matching is case-insensitive and ignores trailing punctuation, so ``"Dr."``
    and ``"III"`` are dropped while ordinary name tokens and single-letter
    initials are preserved.
    """
    out: list[str] = []
    for tok in tokens:
        key = _LETTERS_ONLY.sub("", tok.lower())
        if key and (key in _TITLES or key in _SUFFIXES):
            continue
        out.append(tok)
    return out


def clean_token(tok: str) -> str:
    """Lowercase, ASCII-fold and drop punctuation/apostrophes from one token.

    ``O'Brien`` -> ``obrien``. Returns ``""`` when nothing alphanumeric remains
    (e.g. a lone ``"-"``).
    """
    if not tok:
        return ""
    ascii_tok = to_ascii(tok)  # already lowercased + folded
    return _NON_ALNUM.sub("", ascii_tok)


def _linkedin_host(value: str) -> str | None:
    """Return the lowercased LinkedIn host of ``value`` or None if not LinkedIn.

    Pure string/URL parsing — no DNS, no fetch. Accepts URLs with or without a
    scheme and any ``*.linkedin.com`` subdomain (``www.``, ``in.``, ...).
    """
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    # Prepend '//' when scheme-less so urlparse treats the first segment as host.
    to_parse = raw if "//" in raw else "//" + raw
    try:
        parsed = urllib.parse.urlparse(to_parse)
    except ValueError:
        return None
    host = (parsed.netloc or "").split("@")[-1].split(":")[0].lower()
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return host
    return None


def is_linkedin_url(s: str) -> bool:
    """True when ``s`` looks like a public LinkedIn profile URL.

    The engine uses this purely to *route* the input to local slug parsing —
    never to any fetch. Requires a ``linkedin.com`` host and an ``/in/`` (or
    legacy ``/pub/``) profile path segment.
    """
    host = _linkedin_host(s)
    if host is None:
        return False
    to_parse = s.strip()
    to_parse = to_parse if "//" in to_parse else "//" + to_parse
    path = urllib.parse.urlparse(to_parse).path or ""
    segments = {seg.lower() for seg in path.split("/") if seg}
    return "in" in segments or "pub" in segments


def parse_linkedin_slug(url: str) -> str | None:
    """Extract the ``/in/<slug>`` segment from a LinkedIn URL, locally.

    Uses :mod:`urllib.parse` ONLY — no network I/O (the import-guard test proves
    this). Returns None when ``url`` is not a ``linkedin.com`` ``/in/`` profile
    URL. URL-encoded slugs are unquoted.
    """
    host = _linkedin_host(url)
    if host is None:
        return None
    raw = url.strip()
    to_parse = raw if "//" in raw else "//" + raw
    parts = [p for p in urllib.parse.urlparse(to_parse).path.split("/") if p]
    for i, seg in enumerate(parts):
        if seg.lower() == "in" and i + 1 < len(parts):
            slug = urllib.parse.unquote(parts[i + 1]).strip()
            return slug or None
    return None


def slug_to_name(slug: str) -> str:
    """Turn a LinkedIn slug into a human name string, locally.

    ``ajith-kumar-c-12ab34`` -> ``ajith kumar c``: URL-unquote, split on hyphens,
    drop trailing hash tokens (any token containing a digit — LinkedIn appends a
    random alphanumeric disambiguator), and join the remaining tokens with spaces.
    """
    if not slug:
        return ""
    tokens = [t for t in urllib.parse.unquote(slug).strip().split("-") if t]
    # LinkedIn appends a random hash (e.g. '12ab34', 'a1b2'); strip trailing
    # tokens that contain a digit so the name tokens survive.
    while tokens and any(ch.isdigit() for ch in tokens[-1]):
        tokens.pop()
    return " ".join(tokens).strip()


def _is_latin(text: str) -> bool:
    """True when every alphabetic char is Latin script (or there are no letters)."""
    for ch in text:
        if ch.isalpha():
            try:
                name = unicodedata.name(ch)
            except ValueError:
                return False
            if not name.startswith("LATIN"):
                return False
    return True


def _romanize_by_name(text: str) -> str:
    """Best-effort per-character romanization from Unicode names (stdlib only).

    For scripts whose code points carry a ``... LETTER <name>`` Unicode name
    (Cyrillic, Greek, ...) this approximates a transliteration when no optional
    transliterator is installed. Ideographic scripts (CJK) yield nothing here and
    are simply skipped, so the caller returns fewer candidates rather than junk.
    """
    out: list[str] = []
    for ch in text:
        if ord(ch) < 128:
            out.append(ch)
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue
        if " LETTER " in name:
            token = name.rsplit(" LETTER ", 1)[1]
            token = token.split(" WITH ")[0]
            token = _LETTERS_ONLY.sub("", token.lower())
            out.append(token)
    return "".join(out).strip()


def romanizations(text: str, top_k: int = 2) -> list[str]:
    """Up to ``top_k`` ASCII romanizations for non-Latin input; 1 for Latin.

    Latin (including accented) input returns exactly ``[to_ascii(text)]``.
    Non-Latin scripts return up to ``top_k`` distinct, non-empty candidates
    (the best-available transliteration plus a Unicode-name fallback), and may
    return fewer (even zero for scripts with no stdlib romanization).
    """
    if not text:
        return []
    if _is_latin(text):
        return [to_ascii(text)]

    candidates: list[str] = []
    primary = to_ascii(text)
    if primary:
        candidates.append(primary)
    alt = _romanize_by_name(text)
    if alt:
        candidates.append(alt)

    # Dedupe preserving order, drop empties, cap at top_k.
    seen: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.append(c)
    return seen[: max(0, top_k)]
