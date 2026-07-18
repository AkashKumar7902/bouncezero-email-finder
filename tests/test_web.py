"""Smoke tests for the stdlib-only local web UI (emailfinder/web.py).

Covers the web.py test-plan bullet:
  * POST /api/find returns the same FindResult JSON (incl. reasons) as the core;
  * POST /api/optout adds to suppression and a subsequent /api/find is suppressed;
  * batch upload round-trips to an enriched CSV (skipped if batch.py is absent);
  * the served HTML references NO external http/https URLs;
  * the server binds 127.0.0.1 only.
Plus the JSON contract helpers (render_result_json, provider label, cap note).
"""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest
import copy

from emailfinder import dns_mx, web
from emailfinder.models import MXInfo, Provider


@pytest.fixture(autouse=True)
def _load_sample_kb(engine, sample_kb):
    engine.kb.update(copy.deepcopy(sample_kb))

# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _mx(domain, hosts, error=None):
    return MXInfo(domain=domain, hosts=list(hosts), is_implicit=False, error=error)


@pytest.fixture
def mock_dns(monkeypatch):
    table = {
        "underscore.example": ["aspmx.l.google.com", "alt1.aspmx.l.google.com"],
        "acme.example": ["mx1.hc5016-32.iphmx.com"],
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
def server(engine, mock_dns):
    """A live loopback server bound to a random free port."""
    handler = web.create_handler(engine)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    host, port = httpd.server_address[:2]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield host, port
    httpd.shutdown()
    httpd.server_close()


def _request(port, method, path, body=None, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    payload = None
    hdrs = dict(headers or {})
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=payload, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, resp.getheader("Content-Type"), data


# --------------------------------------------------------------------------- #
# page / binding
# --------------------------------------------------------------------------- #
def test_index_has_no_external_urls(server):
    _host, port = server
    status, ctype, data = _request(port, "GET", "/")
    assert status == 200
    assert "text/html" in ctype
    html = data.decode("utf-8")
    # CSP-safe: no external http(s) resources of any kind.
    assert "http://" not in html
    assert "https://" not in html
    # The core pieces the contract requires are present.
    assert "confidence" not in html.lower() or "bar" in html.lower()
    assert 'id="findbtn"' in html
    assert "Why this guess" in html


def test_binds_loopback_only(server):
    host, _port = server
    assert host == "127.0.0.1"


def test_serve_defaults_to_loopback():
    import inspect

    sig = inspect.signature(web.serve)
    assert sig.parameters["host"].default == "127.0.0.1"


# --------------------------------------------------------------------------- #
# /api/find parity with the engine
# --------------------------------------------------------------------------- #
def test_api_find_matches_engine(server, engine):
    _host, port = server
    status, ctype, data = _request(
        port, "POST", "/api/find", {"name": "Ajith Kumar", "domain": "underscore.example"}
    )
    assert status == 200
    assert "application/json" in ctype
    payload = json.loads(data)

    # Same core call the CLI would make.
    result = engine.find("Ajith Kumar", "underscore.example")
    expected = web.render_result_json(result)

    assert payload["best"]["email"] == expected["best"]["email"]
    assert payload["best"]["email"] == "ajith_kumar@underscore.example"
    assert payload["best"]["reasons"]  # reasons[] always travel with the result
    assert payload["provider"] == expected["provider"]
    assert payload["best"]["separator"] == "_"


def test_api_find_m365_cap_note_never_deliverable(server):
    _host, port = server
    status, _c, data = _request(
        port, "POST", "/api/find",
        {"name": "Rahul Verma", "domain": "widgets.example", "verify": True},
    )
    assert status == 200
    payload = json.loads(data)
    assert payload["provider"] == Provider.MICROSOFT365.value
    assert payload["best"]["status"] != "deliverable"
    assert payload["best"]["cap_note"] == "capped: Microsoft 365 not RCPT-verifiable"


# --------------------------------------------------------------------------- #
# opt-out -> suppression -> suppressed find
# --------------------------------------------------------------------------- #
def test_optout_then_find_is_suppressed(server):
    _host, port = server
    status, _c, _d = _request(
        port, "POST", "/api/optout",
        {"name": "Jane Doe", "domain": "acme.com"},
    )
    assert status == 204

    status, _c, data = _request(
        port, "POST", "/api/find", {"name": "Jane Doe", "domain": "acme.com"}
    )
    assert status == 200
    payload = json.loads(data)
    assert payload["suppressed"] is True
    assert payload["best"] is None


def test_optout_requires_identity(server):
    _host, port = server
    status, _c, _d = _request(port, "POST", "/api/optout", {})
    assert status == 400


# --------------------------------------------------------------------------- #
# feedback + kb
# --------------------------------------------------------------------------- #
def test_feedback_banks_known_bad(server, engine):
    _host, port = server
    status, _c, _d = _request(
        port, "POST", "/api/feedback",
        {"email": "madeup.local@acme.com", "domain": "acme.com", "deliverable": False},
    )
    assert status == 200
    # A subsequent find should now force that local UNDELIVERABLE.
    result = engine.find("Madeup Local", "acme.com")
    hit = [
        sc for sc in ([result.best] + result.alternates)
        if sc.candidate.local_part == "madeup.local"
    ]
    assert hit and hit[0].status.value == "undeliverable"


def test_kb_endpoint_returns_entry(server):
    _host, port = server
    status, _c, data = _request(port, "GET", "/api/kb/underscore.example")
    assert status == 200
    payload = json.loads(data)
    assert payload["domain"] == "underscore.example"
    assert "known_bad_locals" in payload["entry"]


def test_kb_endpoint_404_on_unknown(server):
    _host, port = server
    status, _c, _d = _request(port, "GET", "/api/kb/no-such-domain-xyz.example")
    assert status == 404


# --------------------------------------------------------------------------- #
# batch round-trip (optional: needs batch.py)
# --------------------------------------------------------------------------- #
def test_batch_roundtrip(server):
    pytest.importorskip("emailfinder.batch")
    from emailfinder import batch as _batch  # noqa: F401

    _host, port = server
    csv_in = "name,domain\nAjith Kumar,underscore.example\nRahul Verma,acme.example\n"
    boundary = "----bztest"
    parts = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="in.csv"\r\n'
        "Content-Type: text/csv\r\n\r\n"
        f"{csv_in}\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="verify"\r\n\r\n0\r\n'
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    conn = HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request(
        "POST", "/api/batch", body=parts,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    assert resp.status == 200
    payload = json.loads(data)
    assert "email" in payload["columns"]
    assert len(payload["rows"]) == 2
    assert payload["csv"].startswith("email,")

    # The enriched CSV is now downloadable via GET /api/export.
    status, ctype, exp = _request(port, "GET", "/api/export")
    assert status == 200
    assert "text/csv" in ctype
    assert exp.decode("utf-8").startswith("email,")


# --------------------------------------------------------------------------- #
# rescore round-trip (optional: needs rescore.py)
# --------------------------------------------------------------------------- #
def test_rescore_roundtrip(server):
    pytest.importorskip("emailfinder.rescore")

    _host, port = server
    csv_in = (
        "company,email,domain,local,shape,sep,bounce_status,reason_class\n"
        "Trimble,ashok_kumar@underscore.example,underscore.example,ashok_kumar,first_last,_,"
        "550 5.1.1 user unknown,address_not_found\n"
    )
    boundary = "----bzres"
    parts = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="bounced.csv"\r\n'
        "Content-Type: text/csv\r\n\r\n"
        f"{csv_in}\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="apply_kb"\r\n\r\n0\r\n'
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    conn = HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request(
        "POST", "/api/rescore", body=parts,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    assert resp.status == 200
    payload = json.loads(data)
    assert payload["count"] >= 1
    assert "verdict" in payload["columns"]
    assert payload["csv"].startswith("email,")


# --------------------------------------------------------------------------- #
# pure serialization helpers
# --------------------------------------------------------------------------- #
def test_render_result_json_shape(engine, mock_dns):
    result = engine.find("Ajith Kumar", "underscore.example")
    payload = web.render_result_json(result)
    for key in ("provider", "provider_label", "strategy", "verification_mode",
                "best", "alternates", "notes", "mx", "domain"):
        assert key in payload
    assert isinstance(payload["best"]["reasons"], list)


def test_provider_label_and_cap_note():
    assert web._provider_label(Provider.MICROSOFT365) == "Microsoft 365"
    assert web._cap_note(Provider.MICROSOFT365, False) == (
        "capped: Microsoft 365 not RCPT-verifiable"
    )
    assert web._cap_note(Provider.GOOGLE_WORKSPACE, True) == "catch-all: pattern-only"
    assert web._cap_note(Provider.GOOGLE_WORKSPACE, False) is None


def test_export_404_before_batch(server):
    _host, port = server
    status, _c, _d = _request(port, "GET", "/api/export")
    assert status == 404
