"""Tests for emailfinder.templates + emailfinder.candidates.

Covers the MODULE_CONTRACTS test-plan bullet:
  - template_for_kb: opengov (single_token, sample 'achauhan') -> ('flast','');
    trimble -> ('first_last','_'); purplle -> ('first.l','.')
  - global_priors: first.last dot forced
  - render: literal template rendering + None on missing tokens (mononym)
  - candidates cross-product deduped by local_part keeping highest prior
"""
from __future__ import annotations

from emailfinder.candidates import (
    KB_DOMINANT_PRIOR,
    generate_from_kb,
    generate_from_priors,
)
from emailfinder.models import NameVariant
from emailfinder.templates import global_priors, render, template_for_kb


def _v(first, last, *, middle=None, origin="as_given") -> NameVariant:
    return NameVariant(
        first=first,
        last=last,
        middle=list(middle or []),
        initials=[],
        origin=origin,
    )


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #
def test_render_literal_forms():
    v = _v("ajith", "kumar")
    assert render("first.last", v, ".") == "ajith.kumar"
    assert render("flast", v, "") == "akumar"
    assert render("first_l", v, "_") == "ajith_k"
    assert render("firstlast", v, "") == "ajithkumar"
    assert render("f.last", v, ".") == "a.kumar"
    assert render("last.first", v, ".") == "kumar.ajith"
    assert render("first", v, "") == "ajith"


def test_render_underscore_separator_not_dot():
    # trimble-style underscore literal, last token a single letter -> 'ajith_c'
    assert render("first_last", _v("ajith", "c"), "_") == "ajith_c"


def test_render_lowercases():
    assert render("first.last", _v("Ajith", "Kumar"), ".") == "ajith.kumar"


def test_render_mononym_never_fabricates_surname():
    mono = _v("ajith", None)
    # bare-first works...
    assert render("first", mono, "") == "ajith"
    # ...but anything needing a surname/initial returns None (no fabrication)
    assert render("flast", mono, "") is None
    assert render("first.last", mono, ".") is None
    assert render("first.l", mono, ".") is None


def test_render_unknown_template_returns_none():
    assert render("zzz", _v("ajith", "kumar"), "") is None


def test_render_middle_forms():
    v = _v("john", "public", middle=["quincy"])
    assert render("first.middle.last", v, ".") == "john.quincy.public"
    assert render("first.m.last", v, ".") == "john.q.public"


# --------------------------------------------------------------------------- #
# global_priors
# --------------------------------------------------------------------------- #
def test_global_priors_dot_forced_and_ordered():
    priors = global_priors()
    # first entry is first.last with a forced dot separator
    assert priors[0] == ("first.last", ".", 0.6)
    # flast is a bare single-token concatenation (empty separator)
    assert ("flast", "", 0.12) in priors
    # every entry is (str, str, float)
    for tmpl, sep, prior in priors:
        assert isinstance(tmpl, str) and isinstance(sep, str) and isinstance(prior, float)


# --------------------------------------------------------------------------- #
# template_for_kb
# --------------------------------------------------------------------------- #
def test_template_for_kb_single_token_flast(sample_kb):
    assert template_for_kb(sample_kb["flat.example"]) == ("flast", "")


def test_template_for_kb_underscore(sample_kb):
    assert template_for_kb(sample_kb["underscore.example"]) == ("first_last", "_")


def test_template_for_kb_first_l(sample_kb):
    assert template_for_kb(sample_kb["initials.example"]) == ("first.l", ".")


def test_template_for_kb_roundtrips_through_render(sample_kb):
    t, s = template_for_kb(sample_kb["flat.example"])
    assert render(t, _v("ashish", "chauhan"), s) == "achauhan"
    t, s = template_for_kb(sample_kb["underscore.example"])
    assert render(t, _v("ajith", "c"), s) == "ajith_c"
    t, s = template_for_kb(sample_kb["initials.example"])
    assert render(t, _v("abhishek", "kumar"), s) == "abhishek.k"


def test_template_for_kb_none_separator_sentinel():
    entry = {"dominant_shape": "single_token", "dominant_separator": "(none)",
             "no_bounce_locals": ["achauhan", "asingh", "adubey"]}
    assert template_for_kb(entry) == ("flast", "")


def test_template_for_kb_name_digits_falls_back_to_first_last():
    entry = {"dominant_shape": "name+digits", "dominant_separator": "(none)",
             "no_bounce_locals": ["john123"]}
    assert template_for_kb(entry) == ("first.last", ".")


def test_template_for_kb_multi_collapses_to_first_last():
    entry = {"dominant_shape": "multi.", "dominant_separator": ".",
             "no_bounce_locals": []}
    assert template_for_kb(entry) == ("first.last", ".")


def test_disambiguate_bare_first_for_short_locals():
    entry = {"dominant_shape": "single_token", "dominant_separator": "(none)",
             "no_bounce_locals": ["ravi", "amit", "raj", "anu"]}
    assert template_for_kb(entry) == ("first", "")


# --------------------------------------------------------------------------- #
# candidates
# --------------------------------------------------------------------------- #
def test_generate_from_priors_deduped_by_local():
    # duplicate variants must not produce duplicate local parts
    cands = generate_from_priors([_v("ajith", "kumar"), _v("ajith", "kumar")],
                                 global_priors())
    locals_ = [c.local_part for c in cands]
    assert len(locals_) == len(set(locals_))
    assert all(c.source == "global" for c in cands)


def test_generate_from_priors_keeps_highest_prior_on_collision():
    # For 'sam sam': bare-'first' (0.08) and 'last' (0.01) both render 'sam'.
    cands = generate_from_priors([_v("sam", "sam")], global_priors())
    by_local = {c.local_part: c for c in cands}
    assert by_local["sam"].prior == 0.08
    assert by_local["sam"].template == "first"
    # output sorted by prior descending
    priors = [c.prior for c in cands]
    assert priors == sorted(priors, reverse=True)


def test_generate_from_priors_shape_field_matches_taxonomy():
    cands = generate_from_priors([_v("ajith", "kumar")], global_priors())
    by_local = {c.local_part: c for c in cands}
    assert by_local["ajith.kumar"].shape == "first.last"
    assert by_local["akumar"].shape == "single_token"  # flast render has no sep


def test_generate_from_kb_dominant_prior_and_fallbacks():
    variants = [_v("ajith", "kumar", origin="as_given")]
    cands = generate_from_kb(variants, "first_last", "_",
                             fallbacks=[("first.last", "."), ("flast", "")])
    by_local = {c.local_part: c for c in cands}
    dominant = by_local["ajith_kumar"]
    assert dominant.prior == KB_DOMINANT_PRIOR
    assert dominant.template == "first_last"
    assert dominant.separator == "_"
    assert dominant.source == "kb"
    assert dominant.name_origin == "as_given"
    # fallbacks present with lower priors, deduped, sorted desc
    assert "ajith.kumar" in by_local
    assert "akumar" in by_local
    assert by_local["ajith.kumar"].prior < KB_DOMINANT_PRIOR
    priors = [c.prior for c in cands]
    assert priors == sorted(priors, reverse=True)


def test_generate_from_kb_mononym_drops_surname_templates():
    cands = generate_from_kb([_v("madonna", None)], "first_last", "_",
                             fallbacks=[("first", "")])
    locals_ = [c.local_part for c in cands]
    # dominant needs a surname -> dropped; only bare-first survives
    assert locals_ == ["madonna"]


def test_generate_from_kb_propagates_name_origin():
    cands = generate_from_kb([_v("robert", "smith", origin="nickname")],
                             "first.last", ".", fallbacks=[])
    assert cands[0].name_origin == "nickname"
