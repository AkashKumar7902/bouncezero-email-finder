"""Tests for emailfinder.batch (read_input_csv / run_batch / write_enriched_csv).

Covers the batch.py contract:
  * ENRICHED_COLUMNS is the exact frozen column set and order;
  * read_input_csv honors an optional --map column-rename override;
  * run_batch preserves input order and fingerprints each distinct domain ONCE
    (via Engine.find_batch), and BatchStats returns per-status + distinct-domain
    counts;
  * write_enriched_csv emits every ENRICHED_COLUMN;
  * M365 / catch-all rows are NEVER written DELIVERABLE;
  * suppressed rows carry status 'suppressed' and no address.
"""
from __future__ import annotations

import copy
import csv

import pytest

from emailfinder import dns_mx
from emailfinder.batch import (
    ENRICHED_COLUMNS,
    BatchStats,
    read_input_csv,
    run_batch,
    write_enriched_csv,
)
from emailfinder.models import MXInfo


def _mx(domain, hosts, error=None):
    return MXInfo(domain=domain, hosts=list(hosts), is_implicit=False, error=error)


@pytest.fixture(autouse=True)
def _load_sample_kb(engine, sample_kb):
    engine.kb.update(copy.deepcopy(sample_kb))
    return engine


@pytest.fixture
def mock_dns(monkeypatch):
    """Route resolve_mx through a table + call counter (mirrors test_engine)."""
    table = {
        "underscore.example": ["aspmx.l.google.com", "alt1.aspmx.l.google.com"],
        "acme.example": ["mx1.hc5016-32.iphmx.com"],
        "widgets.example": ["widgets-example.mail.protection.outlook.com"],
        "acme.com": ["aspmx.l.google.com"],
    }
    calls = {"by_domain": {}}

    def fake_resolve_mx(domain, timeout=5.0):
        calls["by_domain"][domain] = calls["by_domain"].get(domain, 0) + 1
        hosts = table.get(domain)
        if hosts is None:
            return _mx(domain, [], error="dns_failure")
        return _mx(domain, hosts)

    monkeypatch.setattr(dns_mx, "resolve_mx", fake_resolve_mx)
    return calls


def _write_csv(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def _read_csv(path):
    with path.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# --------------------------------------------------------------------------- #
# ENRICHED_COLUMNS
# --------------------------------------------------------------------------- #
def test_enriched_columns_are_frozen():
    assert ENRICHED_COLUMNS == [
        "email",
        "first",
        "last",
        "domain",
        "company",
        "template",
        "separator",
        "provider",
        "status",
        "confidence",
        "is_catch_all",
        "is_role",
        "is_disposable",
        "webmail",
        "alt_candidates",
        "verification_mode",
        "provenance_id",
    ]


# --------------------------------------------------------------------------- #
# read_input_csv
# --------------------------------------------------------------------------- #
def test_read_input_csv_canonical_headers(tmp_path):
    p = tmp_path / "in.csv"
    _write_csv(
        p,
        ["name", "domain", "company"],
        [["Ajith Kumar", "underscore.example", "Trimble"], ["Priya Nair", "acme.example", ""]],
    )
    rows = read_input_csv(p)
    assert rows[0] == {"name": "Ajith Kumar", "domain": "underscore.example", "company": "Trimble"}
    # Empty cells are dropped (not carried as "").
    assert rows[1] == {"name": "Priya Nair", "domain": "acme.example"}


def test_read_input_csv_map_override(tmp_path):
    p = tmp_path / "in.csv"
    _write_csv(
        p,
        ["Full Name", "Company Domain"],
        [["Ajith Kumar", "underscore.example"]],
    )
    rows = read_input_csv(p, mapping={"name": "Full Name", "domain": "Company Domain"})
    assert rows == [{"name": "Ajith Kumar", "domain": "underscore.example"}]


def test_read_input_csv_map_is_case_insensitive(tmp_path):
    p = tmp_path / "in.csv"
    _write_csv(p, ["FULLNAME", "DOMAIN"], [["Ajith Kumar", "underscore.example"]])
    rows = read_input_csv(p, mapping={"name": "fullname"})
    assert rows[0]["name"] == "Ajith Kumar"
    # 'domain' has no mapping -> falls back to same-named (case-insensitive) col.
    assert rows[0]["domain"] == "underscore.example"


# --------------------------------------------------------------------------- #
# run_batch: order + fingerprint-once + stats
# --------------------------------------------------------------------------- #
def test_run_batch_order_and_fingerprint_once(engine, mock_dns, tmp_path):
    in_csv = tmp_path / "in.csv"
    out_csv = tmp_path / "out.csv"
    _write_csv(
        in_csv,
        ["name", "domain"],
        [
            ["Ajith Kumar", "underscore.example"],
            ["Rahul Verma", "underscore.example"],
            ["Priya Nair", "acme.example"],
        ],
    )
    stats = run_batch(engine, in_csv, out_csv)

    assert isinstance(stats, BatchStats)
    assert stats.total == 3
    assert stats.distinct_domains == 2
    # MX resolved once per DISTINCT domain (cache-backed fingerprint-once).
    assert mock_dns["by_domain"]["underscore.example"] == 1
    assert mock_dns["by_domain"]["acme.example"] == 1

    out_rows = _read_csv(out_csv)
    # Header matches the frozen columns, in order.
    assert list(out_rows[0].keys()) == ENRICHED_COLUMNS
    # Input order preserved.
    assert [r["first"] for r in out_rows] == ["Ajith", "Rahul", "Priya"]
    # Per-status counts sum to total.
    assert sum(stats.by_status.values()) == 3


def test_run_batch_enriched_row_contents(engine, mock_dns, tmp_path):
    in_csv = tmp_path / "in.csv"
    out_csv = tmp_path / "out.csv"
    _write_csv(in_csv, ["name", "domain", "company"], [["Ajith Kumar", "underscore.example", "Trimble"]])
    run_batch(engine, in_csv, out_csv)
    row = _read_csv(out_csv)[0]
    assert row["email"] == "ajith_kumar@underscore.example"
    assert row["domain"] == "underscore.example"
    assert row["company"] == "Trimble"
    assert row["separator"] == "_"
    assert row["provider"] == "google_workspace"
    assert row["provenance_id"]  # provenance recorded


# --------------------------------------------------------------------------- #
# safety invariant: M365 never DELIVERABLE in the enriched output
# --------------------------------------------------------------------------- #
def test_run_batch_m365_never_deliverable(engine, mock_dns, tmp_path):
    in_csv = tmp_path / "in.csv"
    out_csv = tmp_path / "out.csv"
    _write_csv(in_csv, ["name", "domain"], [["Rahul Verma", "widgets.example"]])
    run_batch(engine, in_csv, out_csv, verify=True)
    row = _read_csv(out_csv)[0]
    assert row["provider"] == "microsoft365"
    assert row["status"] != "deliverable"


# --------------------------------------------------------------------------- #
# suppressed rows carry status 'suppressed' and no address
# --------------------------------------------------------------------------- #
def test_run_batch_suppressed_row(engine, mock_dns, tmp_path):
    engine.compliance.add_suppression(None, "Jane Doe", "acme.com", "test-optout")
    in_csv = tmp_path / "in.csv"
    out_csv = tmp_path / "out.csv"
    _write_csv(in_csv, ["name", "domain"], [["Jane Doe", "acme.com"]])
    stats = run_batch(engine, in_csv, out_csv)
    row = _read_csv(out_csv)[0]
    assert row["status"] == "suppressed"
    assert row["email"] == ""
    assert stats.suppressed == 1
    assert stats.by_status.get("suppressed") == 1


# --------------------------------------------------------------------------- #
# write_enriched_csv standalone
# --------------------------------------------------------------------------- #
def test_write_enriched_csv_emits_all_columns(engine, mock_dns, tmp_path):
    results = list(engine.find_batch([{"name": "Ajith Kumar", "domain": "underscore.example"}]))
    out_csv = tmp_path / "out.csv"
    write_enriched_csv(results, out_csv)
    rows = _read_csv(out_csv)
    assert len(rows) == 1
    assert set(rows[0].keys()) == set(ENRICHED_COLUMNS)
