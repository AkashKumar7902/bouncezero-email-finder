"""Tests for emailfinder.filters — pure static-set filters.

Covers the contract test-plan bullet:
  in_known_bad on trimble 'ashok_kumar' -> True -> UNDELIVERABLE; role local
  'careers'/'hr' filtered; disposable/webmail flags set correctly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from emailfinder import filters

PKG_DATA = Path(__file__).resolve().parent.parent / "emailfinder" / "data"


@pytest.fixture(scope="module")
def sets() -> dict[str, set[str]]:
    return filters.load_static_sets(PKG_DATA)


# ---------------------------------------------------------------- load_static_sets

def test_load_static_sets_shape(sets):
    assert set(sets) == {"role", "disposable", "webmail"}
    for name in ("role", "disposable", "webmail"):
        assert isinstance(sets[name], set)
        assert sets[name], f"{name} set should be non-empty"


def test_load_static_sets_is_cached(sets):
    # Same resolved dir -> identical underlying cached data, distinct copies.
    a = filters.load_static_sets(PKG_DATA)
    b = filters.load_static_sets(PKG_DATA)
    assert a == b
    assert a["role"] is not b["role"]  # fresh copies, not the cached frozenset


def test_load_static_sets_copy_is_isolated():
    a = filters.load_static_sets(PKG_DATA)
    a["role"].add("__mutated__")
    b = filters.load_static_sets(PKG_DATA)
    assert "__mutated__" not in b["role"]


def test_load_static_sets_missing_dir(tmp_path):
    result = filters.load_static_sets(tmp_path / "empty")
    assert result == {"role": set(), "disposable": set(), "webmail": set()}


# ---------------------------------------------------------------- is_role_local

@pytest.mark.parametrize("local", ["careers", "hr", "info", "admin", "support"])
def test_role_locals_flagged(sets, local):
    assert filters.is_role_local(local, sets["role"]) is True


def test_role_local_case_insensitive(sets):
    assert filters.is_role_local("Careers", sets["role"]) is True
    assert filters.is_role_local("  HR ", sets["role"]) is True


def test_person_local_not_role(sets):
    assert filters.is_role_local("ajith.kumar", sets["role"]) is False


def test_empty_local_not_role(sets):
    assert filters.is_role_local("", sets["role"]) is False


# ---------------------------------------------------------------- disposable

def test_disposable_domain_flagged(sets):
    assert filters.is_disposable_domain("0-mail.com", sets["disposable"]) is True


def test_disposable_case_and_trailing_dot(sets):
    assert filters.is_disposable_domain("0-Mail.com.", sets["disposable"]) is True


def test_real_domain_not_disposable(sets):
    assert filters.is_disposable_domain("trimble.com", sets["disposable"]) is False


def test_empty_domain_not_disposable(sets):
    assert filters.is_disposable_domain("", sets["disposable"]) is False


# ---------------------------------------------------------------- webmail

@pytest.mark.parametrize("domain", ["gmail.com", "yahoo.com", "outlook.com"])
def test_webmail_flagged(sets, domain):
    assert filters.is_webmail(domain, sets["webmail"]) is True


def test_webmail_case_insensitive(sets):
    assert filters.is_webmail("GMAIL.COM", sets["webmail"]) is True


def test_corporate_not_webmail(sets):
    assert filters.is_webmail("trimble.com", sets["webmail"]) is False


# ---------------------------------------------------------------- in_known_bad

def test_in_known_bad_trimble():
    kb_entry = {"known_bad_locals": ["ashok_kumar", "someone_else"]}
    assert filters.in_known_bad("ashok_kumar", kb_entry) is True


def test_in_known_bad_case_insensitive():
    kb_entry = {"known_bad_locals": ["Ashok_Kumar"]}
    assert filters.in_known_bad("ashok_kumar", kb_entry) is True


def test_not_in_known_bad():
    kb_entry = {"known_bad_locals": ["ashok_kumar"]}
    assert filters.in_known_bad("ajith_c", kb_entry) is False


def test_known_bad_none_entry():
    assert filters.in_known_bad("anything", None) is False


def test_known_bad_missing_or_empty_list():
    assert filters.in_known_bad("anything", {}) is False
    assert filters.in_known_bad("anything", {"known_bad_locals": []}) is False
