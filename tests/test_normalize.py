"""Tests for emailfinder.normalize — pure token normalization + local slug parse.

Covers the MODULE_CONTRACTS test-plan bullet for normalize.py:
  - to_ascii(Jose/Muller/Nguyen) -> jose/muller/nguyen
  - clean_token(O'Brien) -> obrien
  - strip_titles_suffixes drops Dr/Jr/III
  - parse_linkedin_slug does ZERO network I/O (under a socket-blocking guard)
  - romanizations returns <=2 for non-Latin, 1 for Latin
"""
from __future__ import annotations

import socket

import pytest

from emailfinder import normalize


# --------------------------------------------------------------------------- #
# to_ascii
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("José", "jose"),
        ("Müller", "muller"),
        ("Nguyễn", "nguyen"),
        ("García", "garcia"),
        ("Renée", "renee"),
        ("plain", "plain"),
        ("", ""),
    ],
)
def test_to_ascii_folds_diacritics_and_lowercases(raw, expected):
    assert normalize.to_ascii(raw) == expected


def test_to_ascii_strips_edges():
    assert normalize.to_ascii("  José  ") == "jose"


# --------------------------------------------------------------------------- #
# strip_titles_suffixes
# --------------------------------------------------------------------------- #
def test_strip_titles_suffixes_drops_titles_and_suffixes():
    tokens = ["Dr", "Ajith", "Kumar", "Jr", "III"]
    assert normalize.strip_titles_suffixes(tokens) == ["Ajith", "Kumar"]


def test_strip_titles_suffixes_ignores_trailing_punctuation():
    assert normalize.strip_titles_suffixes(["Dr.", "Jane", "Doe"]) == ["Jane", "Doe"]


def test_strip_titles_suffixes_preserves_initials():
    # A single-letter initial must NOT be mistaken for a suffix.
    assert normalize.strip_titles_suffixes(["Ashwath", "S"]) == ["Ashwath", "S"]


# --------------------------------------------------------------------------- #
# clean_token
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("O'Brien", "obrien"),
        ("D'Angelo", "dangelo"),
        ("Smith-Jones", "smithjones"),
        ("José", "jose"),
        ("  Kumar  ", "kumar"),
        ("---", ""),
        ("", ""),
    ],
)
def test_clean_token(raw, expected):
    assert normalize.clean_token(raw) == expected


# --------------------------------------------------------------------------- #
# is_linkedin_url
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/in/ajith-kumar-c-12ab34",
        "http://linkedin.com/in/jane-doe/",
        "linkedin.com/in/foo",
        "https://in.linkedin.com/in/bar",
        "https://www.linkedin.com/pub/legacy-person/1/2/3",
    ],
)
def test_is_linkedin_url_true(url):
    assert normalize.is_linkedin_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/company/acme",  # not a profile
        "https://example.com/in/foo",  # not linkedin
        "https://notlinkedin.com/in/foo",
        "acme.com",
        "",
        None,
    ],
)
def test_is_linkedin_url_false(url):
    assert normalize.is_linkedin_url(url) is False


# --------------------------------------------------------------------------- #
# parse_linkedin_slug
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.linkedin.com/in/ajith-kumar-c-12ab34", "ajith-kumar-c-12ab34"),
        ("http://linkedin.com/in/jane-doe/", "jane-doe"),
        ("linkedin.com/in/foo", "foo"),
        ("https://in.linkedin.com/in/bar/?trk=x", "bar"),
        ("https://www.linkedin.com/in/jos%C3%A9-garcia", "josé-garcia"),
    ],
)
def test_parse_linkedin_slug_extracts(url, expected):
    assert normalize.parse_linkedin_slug(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/company/acme",
        "https://example.com/in/foo",
        "https://www.linkedin.com/",
        "",
        None,
    ],
)
def test_parse_linkedin_slug_none(url):
    assert normalize.parse_linkedin_slug(url) is None


def test_parse_linkedin_slug_is_network_free(monkeypatch):
    """Slug parsing must be provably network-free (dossier 8.1 clean-room line).

    Any attempt to open a socket raises, so if the parser did any network I/O
    the test would fail loudly.
    """

    def _boom(*args, **kwargs):  # pragma: no cover - only fires on violation
        raise AssertionError("normalize made a network call")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)

    assert (
        normalize.parse_linkedin_slug("https://www.linkedin.com/in/ajith-kumar-c-12ab34")
        == "ajith-kumar-c-12ab34"
    )
    assert normalize.is_linkedin_url("https://www.linkedin.com/in/foo") is True
    assert normalize.slug_to_name("ajith-kumar-c-12ab34") == "ajith kumar c"


# --------------------------------------------------------------------------- #
# slug_to_name
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "slug, expected",
    [
        ("ajith-kumar-c-12ab34", "ajith kumar c"),
        ("jane-doe", "jane doe"),
        ("saravanan-gm-9f3a2", "saravanan gm"),
        ("foo", "foo"),
        ("", ""),
    ],
)
def test_slug_to_name(slug, expected):
    assert normalize.slug_to_name(slug) == expected


# --------------------------------------------------------------------------- #
# romanizations
# --------------------------------------------------------------------------- #
def test_romanizations_latin_returns_one():
    out = normalize.romanizations("José")
    assert out == ["jose"]
    assert len(out) == 1


def test_romanizations_plain_ascii_returns_one():
    assert normalize.romanizations("John Smith") == ["john smith"]


def test_romanizations_non_latin_at_most_two():
    # Cyrillic input: stdlib fallback yields a name-based romanization.
    out = normalize.romanizations("Влади")
    assert len(out) <= 2
    assert all(isinstance(x, str) and x for x in out)


def test_romanizations_respects_top_k():
    out = normalize.romanizations("Влади", top_k=1)
    assert len(out) <= 1


def test_romanizations_empty():
    assert normalize.romanizations("") == []
