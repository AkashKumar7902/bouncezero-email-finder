"""Tests for emailfinder.compliance — the clean-room legal gate.

Covers the contract test-plan bullet:
  - is_suppressed blocks a listed identity (email OR normalized name@domain)
  - provenance.jsonl gets one line per find (build + log)
  - purge_expired removes an aged record
  - silos are per-user isolated (private kb/cache/provenance, shared suppression)
"""
from __future__ import annotations

import json
import time

import pytest

from emailfinder.compliance import Compliance
from emailfinder.models import Candidate, MXInfo


@pytest.fixture
def base_dir(tmp_path):
    d = tmp_path / "efdata"
    d.mkdir()
    return d


def _candidate() -> Candidate:
    return Candidate(
        local_part="ajith_kumar",
        template="first_last",
        separator="_",
        shape="first_last",
        prior=0.6,
        source="kb",
    )


def _mx() -> MXInfo:
    return MXInfo(domain="acme.example", hosts=["mx1.acme.example"])


# -- silo layout -------------------------------------------------------


def test_silo_created_and_paths(base_dir):
    c = Compliance("alice", base_dir)
    assert c.silo_dir.exists()
    paths = c.silo_paths()
    assert set(paths) == {"kb", "cache", "suppression", "provenance"}
    # per-user files live under the user's silo dir
    for key in ("kb", "cache", "provenance"):
        assert c.silo_dir in paths[key].parents
    # suppression is the SHARED global list at base_dir root
    assert paths["suppression"] == base_dir / "global_suppression.jsonl"


def test_silos_are_per_user_isolated(base_dir):
    a = Compliance("alice", base_dir)
    b = Compliance("bob", base_dir)
    assert a.silo_paths()["provenance"] != b.silo_paths()["provenance"]
    assert a.silo_paths()["kb"] != b.silo_paths()["kb"]
    # but the suppression list is shared
    assert a.silo_paths()["suppression"] == b.silo_paths()["suppression"]


def test_user_id_sanitized_for_path(base_dir):
    c = Compliance("Ünsafe/User Id!!", base_dir)
    # directory name is filesystem-safe (single dir, no slashes)
    assert c.silo_dir.parent == base_dir / "users"
    assert "/" not in c.silo_dir.name


# -- suppression -------------------------------------------------------


def test_is_suppressed_false_when_empty(base_dir):
    c = Compliance("alice", base_dir)
    assert c.is_suppressed("x@acme.example", "Ajith Kumar", "acme.example") is False


def test_add_and_check_email_suppression(base_dir):
    c = Compliance("alice", base_dir)
    c.add_suppression("Ajith.Kumar@Acme.Example", None, None, source="optout")
    # case-insensitive match
    assert c.is_suppressed("ajith.kumar@acme.example", None, None) is True
    assert c.is_suppressed("other@acme.example", None, None) is False


def test_add_and_check_identity_suppression(base_dir):
    c = Compliance("alice", base_dir)
    c.add_suppression(None, "Ajith Kumar", "acme.example", source="dsn")
    # diacritics + case + whitespace normalized
    assert c.is_suppressed(None, "  Ajith   Kumar ", "ACME.example") is True
    assert c.is_suppressed(None, "Someone Else", "acme.example") is False


def test_suppression_shared_across_users(base_dir):
    a = Compliance("alice", base_dir)
    b = Compliance("bob", base_dir)
    a.add_suppression("opt@out.com", None, None, source="optout")
    # bob sees alice's opt-out because the list is global
    assert b.is_suppressed("opt@out.com", None, None) is True


def test_add_suppression_noop_without_keys(base_dir):
    c = Compliance("alice", base_dir)
    c.add_suppression(None, "OnlyName", None, source="x")  # no domain -> no key
    assert not c.global_suppression_path.exists()


def test_malformed_suppression_line_ignored(base_dir):
    c = Compliance("alice", base_dir)
    c.global_suppression_path.write_text(
        "not json\n" + json.dumps({"email": "real@x.com"}) + "\n"
    )
    assert c.is_suppressed("real@x.com", None, None) is True


# -- provenance --------------------------------------------------------


def test_build_provenance_shape(base_dir):
    c = Compliance("alice", base_dir)
    rec = c.build_provenance(
        query={"name": "Ajith Kumar", "domain": "acme.example",
               "linkedin_url": "https://linkedin.com/in/ajith", "provider": "microsoft365"},
        mx=_mx(),
        chosen=_candidate(),
        verification_mode="none",
        reasons=["kb dominant pattern"],
    )
    assert rec["source"] == "user-entered name + public MX"
    assert rec["linkedin_slug_local_only"] is True
    assert rec["template"] == "first_last"
    assert rec["separator"] == "_"
    assert rec["domain"] == "acme.example"
    assert rec["provider"] == "microsoft365"
    assert rec["user_id"] == "alice"
    assert rec["reasons"] == ["kb dominant pattern"]
    assert "timestamp" in rec


def test_build_provenance_no_linkedin_no_chosen(base_dir):
    c = Compliance("alice", base_dir)
    rec = c.build_provenance(
        query={"name": "X", "domain": "d.com"},
        mx=None,
        chosen=None,
        verification_mode="smtp",
        reasons=[],
    )
    assert rec["linkedin_slug_local_only"] is False
    assert rec["template"] is None
    assert rec["local_part"] is None
    assert rec["domain"] == "d.com"


def test_log_provenance_one_line_per_find(base_dir):
    c = Compliance("alice", base_dir)
    prov_path = c.silo_paths()["provenance"]
    for _ in range(3):
        rec = c.build_provenance({"name": "N", "domain": "d.com"}, _mx(),
                                 _candidate(), "none", [])
        rid = c.log_provenance(rec)
        assert rid == rec["id"]
    lines = [l for l in prov_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 3
    # every line is valid JSON with an id
    assert all(json.loads(l)["id"] for l in lines)


def test_log_provenance_generates_id_if_missing(base_dir):
    c = Compliance("alice", base_dir)
    rid = c.log_provenance({"user_id": "alice", "domain": "d.com"})
    assert rid
    line = c.provenance_path.read_text().splitlines()[0]
    assert json.loads(line)["id"] == rid


# -- retention purge ---------------------------------------------------


def test_purge_expired_removes_aged_rows(base_dir):
    c = Compliance("alice", base_dir, retention_days=90)
    old_ts = time.time() - 200 * 86400
    fresh_ts = time.time()
    # one aged row, one fresh row
    c.log_provenance({"id": "old", "timestamp": old_ts})
    c.log_provenance({"id": "new", "timestamp": fresh_ts})

    purged = c.purge_expired()
    assert purged == 1
    remaining = [json.loads(l) for l in c.provenance_path.read_text().splitlines() if l.strip()]
    ids = {r["id"] for r in remaining}
    assert ids == {"new"}


def test_purge_expired_noop_when_all_fresh(base_dir):
    c = Compliance("alice", base_dir, retention_days=90)
    c.log_provenance({"id": "a", "timestamp": time.time()})
    assert c.purge_expired() == 0
    assert len(c.provenance_path.read_text().splitlines()) == 1


def test_purge_expired_missing_file(base_dir):
    c = Compliance("alice", base_dir)
    assert c.purge_expired() == 0


def test_purge_keeps_rows_without_timestamp(base_dir):
    c = Compliance("alice", base_dir, retention_days=1)
    c.provenance_path.write_text(json.dumps({"id": "no_ts"}) + "\n")
    assert c.purge_expired() == 0
    assert c.provenance_path.exists()
