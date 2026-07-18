"""Tests for emailfinder.kb_store — KB load / lookup / upsert / atomic save.

Covers the contract test-plan bullet:
  * append_known_bad then get_entry shows the local
  * upsert_verified bumps shape_distribution + no_bounce_locals
  * save_kb round-trips losslessly (sets<->sorted lists, ''<->'(none)')
  * atomic write leaves valid JSON on a simulated crash
  * writes hit the per-user silo, NOT the packaged seed
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from emailfinder import kb_store
from emailfinder.models import Provider

PKG_SEED = Path(__file__).resolve().parent / "fixtures" / "sample_kb.json"


@pytest.fixture
def kb_paths(tmp_path):
    """Return (overlay_path, seed_path) with the real packaged seed."""
    return tmp_path / "domain_kb.json", PKG_SEED


# --------------------------------------------------------------------------- #
# load_kb
# --------------------------------------------------------------------------- #
def test_load_copies_seed_on_first_run(kb_paths):
    overlay, seed = kb_paths
    assert not overlay.exists()
    kb = kb_store.load_kb(overlay, seed)
    assert overlay.exists()                      # overlay materialized
    assert "underscore.example" in kb                   # seed content present
    # In-memory: locals are sets, separator is normalized to "".
    assert isinstance(kb["underscore.example"]["known_bad_locals"], set)
    assert isinstance(kb["underscore.example"]["no_bounce_locals"], set)
    assert kb["flat.example"]["dominant_separator"] == ""   # "(none)" -> ""


def test_load_both_absent_returns_empty(tmp_path):
    kb = kb_store.load_kb(tmp_path / "nope.json", tmp_path / "no_seed.json")
    assert kb == {}


def test_load_existing_overlay_ignores_seed(tmp_path):
    overlay = tmp_path / "kb.json"
    overlay.write_text(json.dumps({"acme.com": {"dominant_separator": "(none)"}}))
    kb = kb_store.load_kb(overlay, PKG_SEED)
    assert set(kb) == {"acme.com"}               # seed not merged in
    assert kb["acme.com"]["dominant_separator"] == ""


# --------------------------------------------------------------------------- #
# get_entry (case-insensitive)
# --------------------------------------------------------------------------- #
def test_get_entry_case_insensitive(kb_paths):
    kb = kb_store.load_kb(*kb_paths)
    assert kb_store.get_entry(kb, "UNDERSCORE.example") is kb["underscore.example"]
    assert kb_store.get_entry(kb, "  Underscore.example.  ") is kb["underscore.example"]
    assert kb_store.get_entry(kb, "does-not-exist.example") is None
    assert kb_store.get_entry(kb, "") is None


# --------------------------------------------------------------------------- #
# append_known_bad
# --------------------------------------------------------------------------- #
def test_append_known_bad_then_get_entry_shows_local(kb_paths):
    overlay, seed = kb_paths
    kb = kb_store.load_kb(overlay, seed)
    kb_store.append_known_bad(kb, overlay, "underscore.example", "New_Bad", "address_not_found")

    entry = kb_store.get_entry(kb, "underscore.example")
    assert "new_bad" in entry["known_bad_locals"]        # normalized + present
    assert entry["reason_classes"]["address_not_found"] >= 1

    # Persisted to the overlay and reloadable.
    reloaded = kb_store.load_kb(overlay, seed)
    assert "new_bad" in reloaded["underscore.example"]["known_bad_locals"]


def test_append_known_bad_dedup_and_empty(kb_paths):
    overlay, seed = kb_paths
    kb = kb_store.load_kb(overlay, seed)
    entry = kb_store.get_entry(kb, "underscore.example")
    before = int(entry["reason_classes"].get("address_not_found", 0))

    kb_store.append_known_bad(kb, overlay, "underscore.example", "dupe", "address_not_found")
    kb_store.append_known_bad(kb, overlay, "underscore.example", "DUPE", "address_not_found")
    # second (case-variant duplicate) must not double-count.
    assert entry["reason_classes"]["address_not_found"] == before + 1

    kb_store.append_known_bad(kb, overlay, "underscore.example", "   ", "address_not_found")
    assert "" not in entry["known_bad_locals"]


def test_append_known_bad_new_domain(kb_paths):
    overlay, seed = kb_paths
    kb = kb_store.load_kb(overlay, seed)
    kb_store.append_known_bad(kb, overlay, "brandnew.io", "jdoe", "recipient_rejected")
    entry = kb_store.get_entry(kb, "brandnew.io")
    assert entry is not None
    assert "jdoe" in entry["known_bad_locals"]
    assert entry["recipient_rejected"] == 1          # top-level counter bumped


# --------------------------------------------------------------------------- #
# upsert_verified
# --------------------------------------------------------------------------- #
def test_upsert_verified_bumps_shape_distribution_and_locals(kb_paths):
    overlay, seed = kb_paths
    kb = kb_store.load_kb(overlay, seed)
    entry = kb_store.get_entry(kb, "underscore.example")
    before_first_l = int(entry["shape_distribution"].get("first_l", 0))

    kb_store.upsert_verified(
        kb, overlay, "underscore.example",
        template="first_l", separator="_",
        provider=Provider.GOOGLE_WORKSPACE, example_local="newperson_x",
    )

    assert "newperson_x" in entry["no_bounce_locals"]
    assert entry["shape_distribution"]["first_l"] == before_first_l + 1
    assert entry["dominant_separator"] == "_"
    assert entry["provider"] == "google_workspace"


def test_upsert_verified_single_token_shape(kb_paths):
    overlay, seed = kb_paths
    kb = kb_store.load_kb(overlay, seed)
    # opengov-style flast single-token local.
    kb_store.upsert_verified(
        kb, overlay, "flat.example",
        template="flast", separator="",
        provider=Provider.PROOFPOINT, example_local="achauhan",
    )
    entry = kb_store.get_entry(kb, "flat.example")
    assert "achauhan" in entry["no_bounce_locals"]
    assert entry["dominant_shape"] == "single_token"     # schema-consistent shape
    assert entry["dominant_separator"] == ""


def test_upsert_verified_new_domain(kb_paths):
    overlay, seed = kb_paths
    kb = kb_store.load_kb(overlay, seed)
    kb_store.upsert_verified(
        kb, overlay, "fresh.co",
        template="first.last", separator=".",
        provider=Provider.MICROSOFT365, example_local="jane.doe",
    )
    entry = kb_store.get_entry(kb, "fresh.co")
    assert entry["dominant_shape"] == "first.last"
    assert entry["shape_distribution"]["first.last"] == 1
    assert entry["provider"] == "microsoft365"


def test_upsert_verified_idempotent_local(kb_paths):
    overlay, seed = kb_paths
    kb = kb_store.load_kb(overlay, seed)
    entry = kb_store.get_entry(kb, "underscore.example")
    kb_store.upsert_verified(
        kb, overlay, "underscore.example", "first_last", "_",
        Provider.GOOGLE_WORKSPACE, "unique_local",
    )
    dist_after_first = dict(entry["shape_distribution"])
    kb_store.upsert_verified(
        kb, overlay, "underscore.example", "first_last", "_",
        Provider.GOOGLE_WORKSPACE, "unique_local",
    )
    # re-adding the same local must not double-count the shape.
    assert entry["shape_distribution"] == dist_after_first


# --------------------------------------------------------------------------- #
# save_kb round-trip
# --------------------------------------------------------------------------- #
def test_save_kb_round_trips_losslessly(kb_paths):
    overlay, seed = kb_paths
    kb = kb_store.load_kb(overlay, seed)
    kb_store.save_kb(kb, overlay)
    kb2 = kb_store.load_kb(overlay, seed)
    # Full-KB equality: sets compare unordered, so lists->sets->lists is lossless.
    assert kb == kb2


def test_save_kb_none_separator_and_sorted_lists_on_disk(tmp_path):
    overlay = tmp_path / "kb.json"
    kb = {
        "x.com": {
            "dominant_separator": "",          # -> "(none)" on disk
            "dominant_shape": "single_token",
            "known_bad_locals": {"zeta", "alpha", "mu"},
            "no_bounce_locals": {"gamma"},
        }
    }
    kb_store.save_kb(kb, overlay)
    raw = json.loads(overlay.read_text())
    assert raw["x.com"]["dominant_separator"] == "(none)"          # "" -> "(none)"
    assert raw["x.com"]["known_bad_locals"] == ["alpha", "mu", "zeta"]   # sorted list
    assert isinstance(raw["x.com"]["no_bounce_locals"], list)

    back = kb_store.load_kb(overlay, tmp_path / "no_seed.json")
    assert back["x.com"]["dominant_separator"] == ""              # "(none)" -> ""
    assert back["x.com"]["known_bad_locals"] == {"alpha", "mu", "zeta"}


def test_dot_separator_preserved_not_treated_as_none(tmp_path):
    overlay = tmp_path / "kb.json"
    kb = {"a.com": {"dominant_separator": ".", "known_bad_locals": set(), "no_bounce_locals": set()}}
    kb_store.save_kb(kb, overlay)
    raw = json.loads(overlay.read_text())
    assert raw["a.com"]["dominant_separator"] == "."             # real "." untouched


# --------------------------------------------------------------------------- #
# atomicity + seed protection
# --------------------------------------------------------------------------- #
def test_atomic_write_leaves_valid_json_on_crash(tmp_path, monkeypatch):
    overlay = tmp_path / "kb.json"
    kb = {"a.com": {"dominant_separator": ".", "known_bad_locals": {"good"}, "no_bounce_locals": set()}}
    kb_store.save_kb(kb, overlay)          # a valid file now exists
    original = overlay.read_text()

    # Simulate a crash at the rename step.
    def boom(src, dst):
        raise OSError("simulated crash")

    monkeypatch.setattr(kb_store.os, "replace", boom)
    kb["a.com"]["known_bad_locals"].add("newbad")
    with pytest.raises(OSError):
        kb_store.save_kb(kb, overlay)

    # The pre-existing file is intact and still valid JSON.
    assert overlay.read_text() == original
    json.loads(overlay.read_text())
    # No leftover temp files in the silo.
    assert list(tmp_path.glob("*.tmp")) == []


def test_writes_never_mutate_packaged_seed(kb_paths):
    overlay, seed = kb_paths
    seed_before = seed.read_bytes()
    kb = kb_store.load_kb(overlay, seed)
    kb_store.append_known_bad(kb, overlay, "underscore.example", "poison", "address_not_found")
    kb_store.upsert_verified(
        kb, overlay, "underscore.example", "first_last", "_",
        Provider.GOOGLE_WORKSPACE, "another_one",
    )
    kb_store.save_kb(kb, overlay)
    assert seed.read_bytes() == seed_before        # packaged seed byte-identical
    # And the overlay is a different, mutated file.
    assert "poison" in json.loads(overlay.read_text())["underscore.example"]["known_bad_locals"]
