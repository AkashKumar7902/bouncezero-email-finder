"""Tests for emailfinder.smtp_probe.

Covers the contract test-plan bullet:
  * port25_open returns False fast (<6s) when :25 is blocked, and is cached.
  * verify returns unavailable=True on NO_PROBE / port-25 blocked, and NEVER
    flips a candidate to invalid.
  * mock server: 550 5.1.1 -> invalid, 451 4.7.1 -> retry, 250 -> valid,
    connect-timeout -> unavailable, all-random-250 -> catch_all.
  * DATA is never sent.
"""
from __future__ import annotations

import socket
import time

import pytest

from emailfinder import smtp_probe
from emailfinder.config import Config
from emailfinder.models import MXInfo, VerifyStrategy


@pytest.fixture
def cfg() -> Config:
    return Config(smtp_connect_timeout=0.5, smtp_cmd_timeout=1.0, ehlo_hostname="probe.example.com",
                  mail_from="probe@probe.example.com")


@pytest.fixture(autouse=True)
def clear_port25_cache():
    smtp_probe._reset_port25_cache()
    yield
    smtp_probe._reset_port25_cache()


# --------------------------------------------------------------------------- #
# Fake SMTP server                                                            #
# --------------------------------------------------------------------------- #


class _FakeSock:
    def settimeout(self, t):
        pass


class FakeSMTP:
    """Minimal smtplib.SMTP stand-in driven by a per-local reply table.

    ``replies`` maps a local-part -> (code, message-bytes). ``connect_error`` if
    set is raised on connect() to simulate a blocked/timed-out port.
    """

    replies: dict[str, tuple[int, bytes]] = {}
    connect_error: BaseException | None = None
    calls: list[str] = []

    def __init__(self, host="", port=0, timeout=None):
        self.sock = _FakeSock()
        type(self).calls = []

    def connect(self, host, port):
        type(self).calls.append(f"connect:{host}:{port}")
        if type(self).connect_error is not None:
            raise type(self).connect_error
        return (220, b"fake ready")

    def ehlo(self, name=""):
        type(self).calls.append("ehlo")
        return (250, b"fake hello")

    def helo(self, name=""):
        type(self).calls.append("helo")
        return (250, b"fake helo")

    def mail(self, sender, options=()):
        type(self).calls.append(f"mail:{sender}")
        return (250, b"ok")

    def rcpt(self, recip, options=()):
        type(self).calls.append(f"rcpt:{recip}")
        local = recip.split("@", 1)[0]
        # random/high-entropy fake locals start with "zzq"
        for key, val in type(self).replies.items():
            if key == local or (key == "__random__" and local.startswith("zzq")):
                return val
        return type(self).replies.get("__default__", (250, b"2.1.5 ok"))

    def data(self, msg):  # pragma: no cover - must never be called
        type(self).calls.append("data")
        raise AssertionError("DATA must never be sent by the prober")

    def docmd(self, cmd, args=""):  # pragma: no cover
        type(self).calls.append(f"docmd:{cmd}")
        if cmd.upper() in ("DATA", "VRFY", "EXPN"):
            raise AssertionError(f"{cmd} must never be sent by the prober")
        return (250, b"ok")

    def quit(self):
        type(self).calls.append("quit")
        return (221, b"bye")

    def close(self):
        type(self).calls.append("close")


def _install_fake(monkeypatch, replies, connect_error=None):
    import smtplib

    FakeSMTP.replies = replies
    FakeSMTP.connect_error = connect_error
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)


# --------------------------------------------------------------------------- #
# port25_open                                                                 #
# --------------------------------------------------------------------------- #


def test_port25_open_false_fast_when_blocked(monkeypatch):
    def fake_create_connection(addr, timeout=None):
        raise socket.timeout("blocked")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    start = time.monotonic()
    assert smtp_probe.port25_open(host="blocked.example.com", timeout=0.5) is False
    assert time.monotonic() - start < 6.0


class _FakeSock:
    """Minimal socket stand-in for the getaddrinfo-based port25_open path."""

    def __init__(self, *a, ok=True):
        self._ok = ok

    def settimeout(self, _t):
        pass

    def connect(self, _addr):
        if not self._ok:
            raise OSError("blocked")

    def close(self):
        pass


def _patch_addrinfo(monkeypatch, ok):
    # One IPv4 + one IPv6 address (dual-stack) so the shared-deadline logic is
    # exercised; a blocked host must not connect on either.
    infos = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 25)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 25, 0, 0)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: infos)
    monkeypatch.setattr(socket, "socket", lambda *a, **k: _FakeSock(ok=ok))


def test_port25_open_true_when_reachable(monkeypatch):
    smtp_probe._reset_port25_cache()
    _patch_addrinfo(monkeypatch, ok=True)
    assert smtp_probe.port25_open(host="open.example.com") is True


def test_port25_open_cached(monkeypatch):
    smtp_probe._reset_port25_cache()
    calls = {"n": 0}

    class _CountingSock(_FakeSock):
        def connect(self, _addr):
            calls["n"] += 1
            raise OSError("blocked")

    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 25))],
    )
    monkeypatch.setattr(socket, "socket", lambda *a, **k: _CountingSock())
    smtp_probe.port25_open(host="cached.example.com")
    smtp_probe.port25_open(host="cached.example.com")
    assert calls["n"] == 1  # second call served from cache (one address, one attempt)


# --------------------------------------------------------------------------- #
# probe_rcpt code mapping                                                     #
# --------------------------------------------------------------------------- #


def test_probe_rcpt_valid(monkeypatch, cfg):
    _install_fake(monkeypatch, {"alice": (250, b"2.1.5 Recipient OK")})
    r = smtp_probe.probe_rcpt("mx.acme.com", "probe@x.com", "alice@acme.com", cfg)
    assert r.verdict == "valid"
    assert r.unavailable is False


def test_probe_rcpt_invalid_511(monkeypatch, cfg):
    _install_fake(monkeypatch, {"bob": (550, b"5.1.1 User unknown")})
    r = smtp_probe.probe_rcpt("mx.acme.com", "probe@x.com", "bob@acme.com", cfg)
    assert r.verdict == "invalid"
    assert r.enhanced == "5.1.1"
    assert r.unavailable is False


def test_probe_rcpt_invalid_5110(monkeypatch, cfg):
    _install_fake(monkeypatch, {"carol": (550, b"5.1.10 RESOLVER.ADR.RecipientNotFound")})
    r = smtp_probe.probe_rcpt("mx.acme.com", "probe@x.com", "carol@acme.com", cfg)
    assert r.verdict == "invalid"
    assert r.enhanced == "5.1.10"


def test_probe_rcpt_retry_451(monkeypatch, cfg):
    _install_fake(monkeypatch, {"dan": (451, b"4.7.1 Greylisted, try again")})
    r = smtp_probe.probe_rcpt("mx.acme.com", "probe@x.com", "dan@acme.com", cfg)
    assert r.verdict == "retry"
    assert r.unavailable is False


def test_probe_rcpt_541_non_signal(monkeypatch, cfg):
    _install_fake(monkeypatch, {"erin": (550, b"5.4.1 Access denied AS(201806281)")})
    r = smtp_probe.probe_rcpt("mx.acme.com", "probe@x.com", "erin@acme.com", cfg)
    assert r.verdict == "non_signal"


def test_probe_rcpt_57x_non_signal(monkeypatch, cfg):
    _install_fake(monkeypatch, {"frank": (550, b"5.7.1 Message rejected by policy")})
    r = smtp_probe.probe_rcpt("mx.acme.com", "probe@x.com", "frank@acme.com", cfg)
    assert r.verdict == "non_signal"


def test_probe_rcpt_552_non_signal(monkeypatch, cfg):
    _install_fake(monkeypatch, {"gina": (552, b"5.2.2 Mailbox full")})
    r = smtp_probe.probe_rcpt("mx.acme.com", "probe@x.com", "gina@acme.com", cfg)
    assert r.verdict == "non_signal"


def test_probe_rcpt_connect_timeout_unavailable(monkeypatch, cfg):
    _install_fake(monkeypatch, {}, connect_error=socket.timeout("blocked"))
    r = smtp_probe.probe_rcpt("mx.acme.com", "probe@x.com", "h@acme.com", cfg)
    assert r.unavailable is True
    assert r.verdict == "unknown"
    # A block must NEVER be read as invalid.
    assert r.verdict != "invalid"


def test_probe_never_sends_data(monkeypatch, cfg):
    _install_fake(monkeypatch, {"ivy": (250, b"2.1.5 ok")})
    smtp_probe.probe_rcpt("mx.acme.com", "probe@x.com", "ivy@acme.com", cfg)
    assert "data" not in FakeSMTP.calls
    assert not any(c.startswith("docmd:DATA") for c in FakeSMTP.calls)


# --------------------------------------------------------------------------- #
# catch-all                                                                   #
# --------------------------------------------------------------------------- #


def test_catchall_true_when_all_random_250(monkeypatch, cfg):
    _install_fake(monkeypatch, {"__random__": (250, b"2.1.5 accepted")})
    assert smtp_probe.probe_domain_catchall("mx.acme.com", "probe@x.com", "acme.com", cfg) is True


def test_catchall_false_when_random_rejected(monkeypatch, cfg):
    _install_fake(monkeypatch, {"__random__": (550, b"5.1.1 no such user")})
    assert smtp_probe.probe_domain_catchall("mx.acme.com", "probe@x.com", "acme.com", cfg) is False


def test_catchall_none_when_transient(monkeypatch, cfg):
    _install_fake(monkeypatch, {"__random__": (451, b"4.7.1 try later")})
    assert smtp_probe.probe_domain_catchall("mx.acme.com", "probe@x.com", "acme.com", cfg) is None


def test_catchall_none_when_unavailable(monkeypatch, cfg):
    _install_fake(monkeypatch, {}, connect_error=OSError("refused"))
    assert smtp_probe.probe_domain_catchall("mx.acme.com", "probe@x.com", "acme.com", cfg) is None


# --------------------------------------------------------------------------- #
# verify guard                                                                #
# --------------------------------------------------------------------------- #


def _mx():
    return MXInfo(domain="acme.com", hosts=["mx1.acme.com", "mx2.acme.com"])


def test_verify_unavailable_on_no_probe(cfg):
    r = smtp_probe.verify("a@acme.com", _mx(), VerifyStrategy.NO_PROBE, cfg)
    assert r.unavailable is True
    assert r.reason == "verification_unavailable"


def test_verify_unavailable_on_no_probe_accept_all(cfg):
    r = smtp_probe.verify("a@acme.com", _mx(), VerifyStrategy.NO_PROBE_ACCEPT_ALL, cfg)
    assert r.unavailable is True


def test_verify_unavailable_when_port25_blocked(monkeypatch, cfg):
    monkeypatch.setattr(smtp_probe, "port25_open", lambda **kw: False)
    r = smtp_probe.verify("a@acme.com", _mx(), VerifyStrategy.PROBE, cfg)
    assert r.unavailable is True
    assert r.verdict != "invalid"


def test_verify_probe_valid_when_open(monkeypatch, cfg):
    monkeypatch.setattr(smtp_probe, "port25_open", lambda **kw: True)
    # random fakes rejected (not catch-all), real address accepted -> valid
    _install_fake(monkeypatch, {"__random__": (550, b"5.1.1 nope"), "alice": (250, b"2.1.5 ok")})
    r = smtp_probe.verify("alice@acme.com", _mx(), VerifyStrategy.PROBE, cfg)
    assert r.verdict == "valid"


def test_verify_downgrades_to_catchall(monkeypatch, cfg):
    monkeypatch.setattr(smtp_probe, "port25_open", lambda **kw: True)
    # every recipient (randoms and real) accepted -> catch-all -> downgrade
    _install_fake(monkeypatch, {"__default__": (250, b"2.1.5 ok"), "__random__": (250, b"2.1.5 ok")})
    r = smtp_probe.verify("alice@acme.com", _mx(), VerifyStrategy.PROBE, cfg)
    assert r.verdict == "catch_all"


def test_verify_blocked_in_this_env_fast():
    """Integration-ish: real :25 is blocked here, so verify degrades fast."""
    start = time.monotonic()
    r = smtp_probe.verify("a@acme.com", _mx(), VerifyStrategy.PROBE,
                          Config(smtp_connect_timeout=3.0))
    assert r.unavailable is True
    assert r.verdict != "invalid"
    assert time.monotonic() - start < 10.0
