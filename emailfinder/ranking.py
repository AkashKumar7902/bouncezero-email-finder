"""PURE ranking: pick the generation path and order the candidates.

Implements the dossier-1.1 two-tier rule:

* if a domain's KB ``dominant_shape`` accounts for at least ``threshold`` (default
  0.60) of its verified addresses, emit the literal KB template + separator (plus
  the 1-2 next-most-common shapes as fallbacks) — this is what makes
  opengov=flast, trimble=underscore and purplle=first.l render correctly;
* otherwise fall back to the ordered dossier-1.2 global priors (dot forced).

An optional SMB size-conditioning boost lives behind ``enable_smb_size_conditioning``
and is deliberately conservative: it NEVER bumps ``first.l`` domain-class-wide for
``.in`` (the dossier 1.5 correction), so a size hint can only re-order, never
invent, a pattern. This module is pure: no I/O, no network.

Depends only on :mod:`emailfinder.models`, :mod:`emailfinder.candidates` and
:mod:`emailfinder.templates`.
"""
from __future__ import annotations

from . import candidates, templates
from .models import Candidate, NameVariant

_SEP_CHARS = "._-"
# Non-structural shape labels carry NO separator; "single_token" literally
# contains an underscore, so it must be excluded explicitly.
_NO_SEP_SHAPES = frozenset({"single_token", "name+digits", "other", ""})


def _sep_from_shape(shape: str) -> str:
    """Return the literal separator embedded in a STRUCTURAL shape label
    (``first_l`` -> ``_``); ``""`` for single_token / name+digits / other."""
    if shape in _NO_SEP_SHAPES:
        return ""
    for ch in _SEP_CHARS:
        if ch in shape:
            return ch
    return ""


def dominant_share(kb_entry: dict) -> tuple[str, float]:
    """Compute ``(dominant_shape, share)`` from a KB entry's ``shape_distribution``.

    The share is the dominant shape's count over the total observed shapes, so
    the 60% gate is explicit and independently testable. When the distribution
    is empty, falls back to the stored ``dominant_shape`` (share 0.0), and when
    there is nothing at all returns ``("", 0.0)``.
    """
    dist = (kb_entry or {}).get("shape_distribution") or {}
    total = sum(int(v) for v in dist.values())
    if total <= 0:
        return (kb_entry or {}).get("dominant_shape", "") or "", 0.0
    shape, count = max(dist.items(), key=lambda kv: int(kv[1]))
    return shape, int(count) / total


def _fallback_specs(
    kb_entry: dict, exclude_shape: str, k: int = 2
) -> list[tuple[str, str]]:
    """The ``k`` next-most-common shapes as literal ``(template, separator)`` pairs.

    Each fallback shape is translated through :func:`templates.template_for_kb`
    (reusing the same single_token disambiguation) so a fallback renders with the
    exact literal template + separator the KB observed for it.
    """
    dist = (kb_entry or {}).get("shape_distribution") or {}
    ordered = sorted(dist.items(), key=lambda kv: int(kv[1]), reverse=True)
    no_bounce = list((kb_entry or {}).get("no_bounce_locals") or [])
    specs: list[tuple[str, str]] = []
    for shape, _count in ordered:
        if shape == exclude_shape:
            continue
        mini = {
            "dominant_shape": shape,
            "dominant_separator": _sep_from_shape(shape),
            "no_bounce_locals": no_bounce,
        }
        specs.append(templates.template_for_kb(mini))
        if len(specs) >= k:
            break
    return specs


def rank(
    variants: list[NameVariant],
    kb_entry: dict | None,
    priors: list[tuple[str, str, float]],
    threshold: float = 0.60,
    size_hint: int | None = None,
) -> list[Candidate]:
    """Pick the generation path and return candidates sorted by prior descending.

    When ``kb_entry`` is present and its dominant shape share is at least
    ``threshold``, generate from the KB dominant template + separator (translated
    by :func:`templates.template_for_kb`) plus 1-2 fallbacks. Otherwise generate
    from the ordered global priors. ``size_hint`` is accepted for API symmetry
    with the optional SMB conditioning path but never bumps ``first.l``
    domain-class-wide (dossier 1.5).
    """
    if kb_entry:
        shape, share = dominant_share(kb_entry)
        if share >= threshold:
            # Render the dominant template from the SAME shape the 60% gate used
            # (the shape_distribution argmax), not the stored ``dominant_shape``
            # field — the two can diverge after a feedback upsert, and re-reading
            # the stale field would drop the true dominant pattern entirely.
            dominant_entry = {
                "dominant_shape": shape,
                "dominant_separator": _sep_from_shape(shape),
                "no_bounce_locals": list((kb_entry or {}).get("no_bounce_locals") or []),
            }
            template, separator = templates.template_for_kb(dominant_entry)
            fallbacks = _fallback_specs(kb_entry, exclude_shape=shape, k=2)
            return candidates.generate_from_kb(
                variants, template, separator, fallbacks
            )
    return candidates.generate_from_priors(variants, priors)
