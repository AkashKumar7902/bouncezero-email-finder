"""Tests for emailfinder.names — India-aware name-variant expansion.

Covers the contract test-plan bullet:
    mononym -> only first-only variant, NO fabricated surname, NO digits;
    South-Indian 'Ashwath S' -> {ashwath.s, ashwaths, ashwath};
    'Saravanan GM' -> saravanan.gm;
    compound 'Van Der Berg' -> {vanderberg, berg, van};
    hyphenated 'Smith-Jones' -> {smithjones, smith, jones};
    nickname 'Bob' -> robert included.
"""
from __future__ import annotations

from pathlib import Path

from emailfinder.models import NameVariant, ParsedName
from emailfinder.names import expand_variants, load_nicknames, parse_name

PKG_DATA = Path(__file__).resolve().parent.parent / "emailfinder" / "data"
NICKNAMES = PKG_DATA / "nicknames.json"


def _render(variants: list[NameVariant]) -> set[str]:
    """Approximate the template renderer to derive the concrete local parts a
    variant set would produce (first.last / firstlast / flast / first.<init> /
    first<init> / first-only). Kept independent of templates.py so this test
    exercises names.py alone."""
    locals_: set[str] = set()
    for v in variants:
        if v.first and v.last:
            locals_.add(f"{v.first}.{v.last}")
            locals_.add(f"{v.first}{v.last}")
            locals_.add(f"{v.first[0]}{v.last}")
        if v.first and v.initials:
            joined = "".join(v.initials)
            locals_.add(f"{v.first}.{joined}")
            locals_.add(f"{v.first}{joined}")
        if v.first and not v.last and not v.initials:
            locals_.add(v.first)
    return locals_


# --------------------------------------------------------------------------- #
# parse_name
# --------------------------------------------------------------------------- #

def test_single_token_is_mononym():
    pn = parse_name("Madonna")
    assert pn.is_mononym is True
    assert pn.first == "madonna"
    assert pn.last is None
    assert pn.initials == []


def test_single_letter_tokens_are_initials():
    pn = parse_name("Ashwath S")
    assert pn.first == "ashwath"
    assert pn.initials == ["s"]
    assert pn.last is None
    assert pn.is_mononym is False


def test_titles_and_suffixes_stripped():
    pn = parse_name("Dr. Aman Sharma Jr")
    assert pn.first == "aman"
    assert pn.last == "sharma"


def test_two_letter_trailing_token_is_last_not_initial():
    # 'GM' is length 2 -> treated as an ordinary surname token.
    pn = parse_name("Saravanan GM")
    assert pn.first == "saravanan"
    assert pn.last == "gm"
    assert pn.initials == []


# --------------------------------------------------------------------------- #
# expand_variants — required local-part sets
# --------------------------------------------------------------------------- #

def test_mononym_only_first_only_no_fabricated_surname_no_digits():
    variants = expand_variants(parse_name("Madonna"), {})
    # No variant may carry a surname or initials.
    assert all(v.last is None and not v.initials for v in variants)
    rendered = _render(variants)
    assert rendered == {"madonna"}
    # Never fabricates a surname, never appends digits.
    assert not any(any(ch.isdigit() for ch in loc) for loc in rendered)


def test_south_indian_first_initial():
    rendered = _render(expand_variants(parse_name("Ashwath S"), {}))
    assert {"ashwath.s", "ashwaths", "ashwath"} <= rendered
    assert not any(any(ch.isdigit() for ch in loc) for loc in rendered)


def test_saravanan_gm():
    rendered = _render(expand_variants(parse_name("Saravanan GM"), {}))
    assert "saravanan.gm" in rendered


def test_compound_particle_surname():
    rendered = _render(expand_variants(parse_name("Van Der Berg"), {}))
    assert {"vanderberg", "berg", "van"} <= rendered


def test_hyphenated_surname():
    rendered = _render(expand_variants(parse_name("Smith-Jones"), {}))
    assert {"smithjones", "smith", "jones"} <= rendered


def test_compound_surname_with_given_name():
    pn = parse_name("Maria Van Der Berg")
    assert pn.first == "maria"
    assert pn.extra_tokens == ["van", "der", "berg"]
    rendered = _render(expand_variants(pn, {}))
    assert "maria.vanderberg" in rendered
    assert "maria.berg" in rendered
    assert "maria.van" in rendered


def test_nickname_expansion_includes_formal():
    table = load_nicknames(NICKNAMES)
    rendered = _render(expand_variants(parse_name("Bob"), table))
    assert "robert" in rendered
    # all variants stay first-only (a mononym is never given a surname)
    assert rendered == {n for n in rendered if "." not in n}


def test_nickname_with_surname():
    table = load_nicknames(NICKNAMES)
    rendered = _render(expand_variants(parse_name("Bob Smith"), table))
    assert "bob.smith" in rendered
    assert "robert.smith" in rendered


def test_drop_middle_highest_and_keep_middle_present():
    pn = parse_name("John Michael Smith")
    assert pn.first == "john"
    assert pn.middle == ["michael"]
    assert pn.last == "smith"
    variants = expand_variants(pn, {})
    # drop-middle form comes before the keep-middle form (as-given first).
    dropped = NameVariant(first="john", last="smith", middle=[], initials=[])
    kept = NameVariant(
        first="john", last="smith", middle=["michael"], initials=[]
    )
    keys = [(v.first, v.last, tuple(v.middle)) for v in variants]
    assert (dropped.first, dropped.last, ()) in keys
    assert (kept.first, kept.last, ("michael",)) in keys
    assert keys.index((dropped.first, dropped.last, ())) < keys.index(
        (kept.first, kept.last, ("michael",))
    )


# --------------------------------------------------------------------------- #
# load_nicknames — bidirectional index
# --------------------------------------------------------------------------- #

def test_load_nicknames_bidirectional():
    table = load_nicknames(NICKNAMES)
    assert "robert" in table["bob"]
    assert "bob" in table["robert"]
    assert "william" in table["bill"]
    # a name appearing in two groups unions both (chris -> christopher + christine)
    assert "christopher" in table["chris"]
    assert "christine" in table["chris"]


def test_load_nicknames_returns_mutable_copy():
    table = load_nicknames(NICKNAMES)
    table["bob"].append("sentinel")
    fresh = load_nicknames(NICKNAMES)
    assert "sentinel" not in fresh["bob"]


def test_as_given_variant_is_first():
    variants = expand_variants(parse_name("Aman Sharma"), {})
    assert variants[0].origin == "as_given"
    assert variants[0].first == "aman"
    assert variants[0].last == "sharma"
