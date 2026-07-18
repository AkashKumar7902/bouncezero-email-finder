"""PURE candidate generation (dossier 1.3 step 5).

Cross every :class:`NameVariant` with a set of literal templates, render each,
drop the empties (a template that a variant lacks tokens for), and DEDUPE the
variant x template cross-product by local part, keeping the highest-prior
producer and its provenance.

Two entry points:
  - :func:`generate_from_kb` — the per-domain KB dominant template (+ 1-2
    fallbacks), used when a domain has a confident learned pattern.
  - :func:`generate_from_priors` — the full dossier-1.2 ordered global list,
    used when the domain is unknown (dot forced).

Depends only on :mod:`emailfinder.models` and :mod:`emailfinder.templates`.
"""
from __future__ import annotations

import re

from .models import Candidate, NameVariant
from .templates import render

# Prior assigned to a KB dominant template (a confident learned pattern is far
# stronger than any global prior). Fallbacks descend from there.
KB_DOMINANT_PRIOR = 0.9
_KB_FALLBACK_START = 0.5
_KB_FALLBACK_STEP = 0.1
_KB_FALLBACK_FLOOR = 0.2

_NAME_DIGITS = re.compile(r"[a-z]+\d+")


def _shape_of(local: str) -> str:
    """Structural shape label for a rendered local part.

    Mirrors :func:`emailfinder.shapes.shape` (which generation must NOT import,
    per the module contract) so the informational ``Candidate.shape`` field uses
    the same taxonomy the KB does.
    """
    for sep in (".", "_", "-"):
        if sep in local:
            toks = local.split(sep)
            if len(toks) == 2:
                a, b = toks
                if len(a) == 1 and b.isalpha():
                    return f"f{sep}last"
                if len(b) == 1 and a.isalpha():
                    return f"first{sep}l"
                return f"first{sep}last"
            return f"multi{sep}"
    if local.isalpha():
        return "single_token"
    if _NAME_DIGITS.fullmatch(local):
        return "name+digits"
    return "other"


def _dedupe(cands: list[Candidate]) -> list[Candidate]:
    """Dedupe by local part keeping the highest-prior producer (ties keep the
    first-seen, i.e. the as-given variant / dominant template), then sort by
    prior descending (stable)."""
    best: dict[str, Candidate] = {}
    for cand in cands:
        existing = best.get(cand.local_part)
        if existing is None or cand.prior > existing.prior:
            best[cand.local_part] = cand
    result = list(best.values())
    result.sort(key=lambda c: c.prior, reverse=True)
    return result


def generate_from_kb(
    variants: list[NameVariant],
    dominant_template: str,
    dominant_separator: str,
    fallbacks: list[tuple[str, str]],
) -> list[Candidate]:
    """Emit the KB dominant template (prior ~0.9) plus 1-2 fallbacks across all
    variants; dedupe by local part keeping the highest prior + its provenance.

    ``fallbacks`` is an ordered list of ``(template, separator)`` pairs (the
    next-most-common shapes ranking.py picked); each gets a descending prior.
    Empty renders (missing tokens, e.g. flast on a mononym) are dropped.
    """
    specs: list[tuple[str, str, float]] = [
        (dominant_template, dominant_separator, KB_DOMINANT_PRIOR)
    ]
    for i, (tmpl, sep) in enumerate(fallbacks):
        prior = max(_KB_FALLBACK_START - i * _KB_FALLBACK_STEP, _KB_FALLBACK_FLOOR)
        specs.append((tmpl, sep, prior))

    cands: list[Candidate] = []
    for variant in variants:
        for tmpl, sep, prior in specs:
            local = render(tmpl, variant, sep)
            if not local:
                continue
            cands.append(
                Candidate(
                    local_part=local,
                    template=tmpl,
                    separator=sep,
                    shape=_shape_of(local),
                    prior=prior,
                    source="kb",
                    name_origin=variant.origin,
                )
            )
    return _dedupe(cands)


def generate_from_priors(
    variants: list[NameVariant],
    priors: list[tuple[str, str, float]],
) -> list[Candidate]:
    """Emit the full dossier-1.2 ordered prior set across all variants (dot
    forced by the seed separators), deduped by local part keeping the highest
    prior + its provenance (``source='global'``)."""
    cands: list[Candidate] = []
    for variant in variants:
        for tmpl, sep, prior in priors:
            local = render(tmpl, variant, sep)
            if not local:
                continue
            cands.append(
                Candidate(
                    local_part=local,
                    template=tmpl,
                    separator=sep,
                    shape=_shape_of(local),
                    prior=prior,
                    source="global",
                    name_origin=variant.origin,
                )
            )
    return _dedupe(cands)
