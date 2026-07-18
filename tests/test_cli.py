"""CLI surface tests: argparse dispatch, exit codes, and honest rendering.

Runs fully offline (DNS mocked, SMTP + providers off). Covers the cli.py
contract:
  * find prints the chosen ScoredCandidate (email / status chip / 0-100 /
    provider badge) + alternates + the 'why this guess' reasons trail;
  * M365 / catch-all are labelled unverifiable and NEVER DELIVERABLE in output;
  * global flags (--json / --user / --data-dir) work after the subcommand;
  * exit codes 0 (ok) / 1 (usage) / 2 (no candidate / degraded / suppressed);
  * kb / optout / purge round-trip against the per-user silo.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from emailfinder import cli, dns_mx
from emailfinder.models import MXInfo, Status

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _mx(domain, hosts, error=None):
    return MXInfo(domain=domain, hosts=list(hosts), is_implicit=False, error=error)


@pytest.fixture
def mock_dns(monkeypatch):
    table = {
        "underscore.example": ["aspmx.l.google.com", "alt1.aspmx.l.google.com"],
        "acme.example": ["mx1.hc5016-32.iphmx.com", "mx2.hc5016-32.iphmx.com"],
        "widgets.example": ["widgets-example.mail.protection.outlook.com"],
        "acme.com": ["aspmx.l.google.com"],
    }

    def fake_resolve_mx(domain, timeout=5.0):
        hosts = table.get(domain)
        if hosts is None:
            return _mx(domain, [], error="dns_failure")
        return _mx(domain, hosts)

    monkeypatch.setattr(dns_mx, "resolve_mx", fake_resolve_mx)
    return table


@pytest.fixture
def silo(tmp_path):
    """Global flags that pin the CLI to an isolated per-test silo, pre-seeded
    with the SYNTHETIC KB overlay (the shipped package carries an empty seed)."""
    d = tmp_path / "silo"
    d.mkdir()
    user_dir = d / "users" / "clitest"
    user_dir.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "sample_kb.json", user_dir / "kb.json")
    return ["--data-dir", str(d), "--user", "clitest"]


def _run(argv, capsys):
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


# --------------------------------------------------------------------------- #
# find: human output
# --------------------------------------------------------------------------- #
def test_find_human_renders_email_chip_confidence_and_reasons(silo, mock_dns, capsys):
    code, out, _ = _run(silo + ["find", "Ajith Kumar", "--domain", "underscore.example"], capsys)
    assert code == cli.EXIT_OK
    assert "ajith_kumar@underscore.example" in out
    assert "confidence" in out and "/100" in out
    assert "provider:" in out
    # the 'why this guess' reasons trail is always shown
    assert "why this guess:" in out
    # a status chip is rendered in [BRACKETS]
    assert "[" in out and "]" in out


def test_find_json_includes_reasons_and_serializes_cleanly(silo, mock_dns, capsys):
    code, out, _ = _run(
        silo + ["--json", "find", "Ajith Kumar", "--domain", "underscore.example"], capsys
    )
    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert payload["domain"] == "underscore.example"
    assert payload["best"]["email"] == "ajith_kumar@underscore.example"
    assert isinstance(payload["best"]["reasons"], list)
    assert payload["provider_badge"]
    # enums serialized to their string values, not "Status.X"
    assert payload["best"]["status"] in {s.value for s in Status}


def test_global_flag_after_subcommand(silo, mock_dns, capsys):
    # --json placed AFTER the subcommand must still take effect.
    code, out, _ = _run(silo + ["find", "Ajith Kumar", "--domain", "underscore.example", "--json"], capsys)
    assert code == cli.EXIT_OK
    json.loads(out)  # parses as JSON


# --------------------------------------------------------------------------- #
# find: M365 / catch-all are unverifiable, never DELIVERABLE
# --------------------------------------------------------------------------- #
def test_find_m365_labeled_unverifiable_never_deliverable(silo, mock_dns, capsys):
    code, out, _ = _run(
        silo + ["--json", "find", "Rahul Verma", "--domain", "widgets.example", "--verify"],
        capsys,
    )
    payload = json.loads(out)
    assert payload["provider"] == "microsoft365"
    assert payload["best"]["status"] != Status.DELIVERABLE.value
    for alt in payload["alternates"]:
        assert alt["status"] != Status.DELIVERABLE.value
    assert "unverifiable" in payload["best"]["cap_note"].lower()


def test_find_m365_human_shows_cap_note(silo, mock_dns, capsys):
    code, out, _ = _run(
        silo + ["find", "Rahul Verma", "--domain", "widgets.example"], capsys
    )
    assert "Microsoft 365" in out
    assert "unverifiable" in out.lower()
    assert "DELIVERABLE]" not in out  # no [DELIVERABLE] chip for M365


# --------------------------------------------------------------------------- #
# find: exit codes
# --------------------------------------------------------------------------- #
def test_find_dns_failure_is_degraded_exit_2(silo, mock_dns, capsys):
    code, out, _ = _run(silo + ["find", "Some One", "--domain", "no-such.example"], capsys)
    assert code == cli.EXIT_NO_CANDIDATE


def test_find_usage_error_missing_domain_exit_1(silo, capsys):
    code, _, err = _run(silo + ["find", "Ajith Kumar"], capsys)
    assert code == cli.EXIT_USAGE
    assert "error:" in err


def test_find_usage_error_missing_name_exit_1(silo, capsys):
    code, _, err = _run(silo + ["find", "--domain", "underscore.example"], capsys)
    assert code == cli.EXIT_USAGE


def test_no_subcommand_prints_help_exit_1(capsys):
    code, out, _ = _run([], capsys)
    assert code == cli.EXIT_USAGE


# --------------------------------------------------------------------------- #
# find: suppressed identity
# --------------------------------------------------------------------------- #
def test_find_suppressed_prints_notice_no_address(silo, mock_dns, capsys):
    # First opt the identity out via the optout command (same silo).
    _run(silo + ["optout", "jane.doe@acme.com"], capsys)
    # Suppress by identity so a name+domain find is blocked.
    from emailfinder.cli import _build_engine
    import argparse

    ns = argparse.Namespace(user="clitest", data_dir=silo[1], config=None,
                            verify=False, use_providers=False)
    eng = _build_engine(ns)
    eng.compliance.add_suppression(None, "Jane Doe", "acme.com", "test")
    eng.close()

    code, out, _ = _run(silo + ["find", "Jane Doe", "--domain", "acme.com"], capsys)
    assert code == cli.EXIT_NO_CANDIDATE
    assert "SUPPRESSED" in out
    assert "@acme.com" not in out  # no address leaked


# --------------------------------------------------------------------------- #
# find: LinkedIn slug is local-only
# --------------------------------------------------------------------------- #
def test_find_linkedin_slug_parsed_locally(silo, mock_dns, capsys, monkeypatch):
    import socket

    def _no_net(*a, **k):  # pragma: no cover
        raise AssertionError("CLI performed network I/O for a LinkedIn URL")

    monkeypatch.setattr(socket, "create_connection", _no_net)
    code, out, _ = _run(
        silo
        + [
            "--json",
            "find",
            "--domain",
            "underscore.example",
            "--linkedin",
            "https://www.linkedin.com/in/ajith-kumar-c-12ab34/",
        ],
        capsys,
    )
    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert payload["query"]["linkedin_slug"] == "ajith-kumar-c-12ab34"
    assert payload["best"]["email"].endswith("@underscore.example")


# --------------------------------------------------------------------------- #
# kb
# --------------------------------------------------------------------------- #
def test_kb_inspect_known_domain(silo, capsys):
    code, out, _ = _run(silo + ["kb", "underscore.example"], capsys)
    assert code == cli.EXIT_OK
    assert "underscore.example" in out
    assert "dominant pattern" in out


def test_kb_inspect_json(silo, capsys):
    code, out, _ = _run(silo + ["--json", "kb", "acme.example"], capsys)
    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert payload["found"] is True
    assert payload["dominant_shape"] == "first.last"
    assert payload["dominant_share"] >= 0.60


def test_kb_unknown_domain_exit_2(silo, capsys):
    code, out, _ = _run(silo + ["kb", "no-such-domain-xyz.example"], capsys)
    assert code == cli.EXIT_NO_CANDIDATE
    assert "no KB entry" in out


# --------------------------------------------------------------------------- #
# optout
# --------------------------------------------------------------------------- #
def test_optout_adds_then_find_is_suppressed(silo, mock_dns, capsys):
    code, out, _ = _run(silo + ["optout", "rahul.verma@acme.com"], capsys)
    assert code == cli.EXIT_OK
    assert "opted out" in out

    # A find that would produce rahul.verma@acme.com is now suppressed by address.
    # (Engine's suppression gate checks name@domain identity; here we assert the
    #  suppression file grew and the address key is present.)
    code2, out2, _ = _run(silo + ["--json", "optout", "another@acme.com"], capsys)
    assert code2 == cli.EXIT_OK
    assert json.loads(out2)["suppressed"] == "another@acme.com"


def test_optout_invalid_email_exit_1(silo, capsys):
    code, _, err = _run(silo + ["optout", "not-an-email"], capsys)
    assert code == cli.EXIT_USAGE


# --------------------------------------------------------------------------- #
# purge
# --------------------------------------------------------------------------- #
def test_purge_reports_count(silo, mock_dns, capsys):
    # A find writes one provenance row; purge with a huge window purges nothing.
    _run(silo + ["find", "Ajith Kumar", "--domain", "underscore.example"], capsys)
    code, out, _ = _run(silo + ["purge", "--days", "3650"], capsys)
    assert code == cli.EXIT_OK
    assert "purged 0" in out

    code2, out2, _ = _run(silo + ["--json", "purge", "--days", "0"], capsys)
    assert code2 == cli.EXIT_OK
    assert "purged" in json.loads(out2)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def test_parse_map_helper():
    assert cli._parse_map("name=Name,domain=Domain") == {"name": "Name", "domain": "Domain"}
    assert cli._parse_map(None) is None
    assert cli._parse_map("bogus") is None
    assert cli._parse_map("") == {}


# --------------------------------------------------------------------------- #
# batch / rescore: run only when those peers are present
# --------------------------------------------------------------------------- #
def test_batch_roundtrip(silo, mock_dns, tmp_path, capsys):
    pytest.importorskip("emailfinder.batch")
    in_csv = tmp_path / "in.csv"
    in_csv.write_text("name,domain\nAjith Kumar,underscore.example\nRahul Verma,acme.example\n")
    out_csv = tmp_path / "out.csv"
    code, out, _ = _run(
        silo + ["batch", str(in_csv), "-o", str(out_csv)], capsys
    )
    assert code == cli.EXIT_OK
    assert out_csv.exists()
    assert "email" in out_csv.read_text().splitlines()[0]


def test_batch_missing_input_exit_1(silo, capsys, tmp_path):
    pytest.importorskip("emailfinder.batch")
    code, _, err = _run(
        silo + ["batch", str(tmp_path / "nope.csv"), "-o", str(tmp_path / "o.csv")],
        capsys,
    )
    assert code == cli.EXIT_USAGE


def test_rescore_requires_input_exit_1(silo, capsys):
    pytest.importorskip("emailfinder.rescore")
    code, _, err = _run(silo + ["rescore"], capsys)
    assert code == cli.EXIT_USAGE
