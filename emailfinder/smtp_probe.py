"""OPTIONAL, off-by-default SMTP RCPT prober (research dossier sections 2-3).

This module is the ONLY place that talks SMTP. It is isolated so it is trivially
mockable, and so the rest of the package degrades gracefully when port 25 is not
reachable (the common case: residential ISPs, AWS/Azure/GCP defaults, and this
dev machine all block outbound :25).

Hard safety invariants (dossier sections 2.2, 2.3, 3.1, 10):
  * A port-25 block / connect-timeout / refused connection / read timeout is
    ``unavailable`` (verdict ``unknown``), NEVER ``invalid``.
  * Only an *honest* permanent ``550 5.1.1`` / ``5.1.10`` maps to ``invalid``.
  * ``4xx`` (greylist / defer, e.g. ``451 4.7.1``) is ``retry``, never invalid.
  * ``5.4.1`` (M365 DBEB edge decision) and ``5.7.x`` (sender-side policy) are
    ``non_signal`` pre-send; ``552`` (storage/size) is ``non_signal`` too.
  * DATA and VRFY/EXPN are NEVER sent. Stopping at RCPT transmits no mail.

The prober uses the stdlib :mod:`smtplib` (always available). ``aiosmtplib`` is
only useful for high-concurrency async probing, which the synchronous engine
pipeline does not need, so it is intentionally not required here.
"""
from __future__ import annotations

import random
import re
import string
import time

from .config import Config
from .models import MXInfo, SmtpResult, VerifyStrategy

__all__ = [
    "port25_open",
    "probe_domain_catchall",
    "detect_catchall",
    "probe_rcpt",
    "verify",
]

# Well-known always-on MX used purely as a reachability canary. If :25 is open
# to Gmail's inbound MX it is open in general; if it hangs, the host blocks :25.
_CANARY_HOST = "gmail-smtp-in.l.google.com"

# Cached per run so an entire batch degrades instantly instead of each row
# re-hanging for the full connect timeout when :25 is blocked.
_PORT25_CACHE: dict[str, bool] = {}

# RFC 3463 enhanced status code, e.g. "5.1.1" / "4.7.1".
_ENHANCED_RE = re.compile(r"\b([245]\.\d{1,3}\.\d{1,3})\b")


def _reset_port25_cache() -> None:
    """Clear the per-run reachability cache (test/harness helper)."""
    _PORT25_CACHE.clear()


def port25_open(host: str = _CANARY_HOST, timeout: float = 6.0) -> bool:
    """Fast pre-flight TCP connect to ``host:25``.

    Returns ``True`` only when the TCP handshake completes within ``timeout``.
    A hang/timeout (no RST) or any socket error -> ``False`` (blocked). The
    result is cached per host for the life of the process so the whole batch
    degrades instantly rather than hanging once per row.
    """
    if host in _PORT25_CACHE:
        return _PORT25_CACHE[host]

    ok = _tcp_connect_ok(host, 25, timeout)
    _PORT25_CACHE[host] = ok
    return ok


def _tcp_connect_ok(host: str, port: int, timeout: float) -> bool:
    """Attempt a TCP connect within a single OVERALL wall-clock ``timeout``.

    ``socket.create_connection`` applies its timeout PER resolved address, so on
    a dual-stack host (IPv6 + IPv4) a blocked port takes ~2x the timeout. We
    resolve the addresses ourselves and share one budget across them, so a
    blocked host is detected in ~``timeout`` seconds regardless of how many
    addresses it resolves to.
    """
    import socket
    import time

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False

    deadline = time.monotonic() + timeout
    per_attempt = max(1.0, timeout / len(infos))
    for family, socktype, proto, _canon, sockaddr in infos:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(min(per_attempt, remaining))
        try:
            sock.connect(sockaddr)
            sock.close()
            return True
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
            continue
    return False


def probe_domain_catchall(
    host: str, mail_from: str, domain: str, cfg: Config
) -> bool | None:
    """Catch-all detection (dossier 3.1).

    RCPT a high-entropy guaranteed-fake local part plus 2 more randoms in ONE
    session (same triplet, same server mood):
      * all accepted (250) -> ``True`` (catch-all: any specific 250 is meaningless)
      * all consistently rejected (permanent) -> ``False``
      * inconsistent, transient, or unreachable -> ``None`` (unknown)
    """
    fake_addrs = [f"{_random_local()}@{domain}" for _ in range(3)]
    results = _probe_session(host, mail_from, fake_addrs, cfg)
    if results is None:
        return None

    verdicts = [r.verdict for r in results]
    if all(v == "valid" for v in verdicts):
        return True
    if verdicts and all(v in ("invalid", "non_signal") for v in verdicts):
        return False
    return None


def probe_rcpt(host: str, mail_from: str, email: str, cfg: Config) -> SmtpResult:
    """Single-address RCPT probe.

    Open :25 with a short connect timeout, read the banner, EHLO a real FQDN
    (HELO fallback on 5xx), MAIL FROM a deliverable probe address, RCPT TO the
    candidate, then QUIT. NEVER sends DATA/VRFY. Maps code + enhanced code:
    250->valid, 550 5.1.1/5.1.10->invalid, 4xx->retry, 5.4.1/5.7.x/552->non_signal,
    connect/read timeout or refused->unavailable (unknown, NEVER invalid).
    """
    results = _probe_session(host, mail_from, [email], cfg)
    if results is None:
        return SmtpResult(
            verdict="unknown",
            reason="verification_unavailable",
            unavailable=True,
        )
    return results[0]


def detect_catchall(mx: MXInfo, strategy: VerifyStrategy, cfg: Config) -> bool | None:
    """Detect a domain's catch-all status ONCE (dossier 3.4 fingerprint-once).

    Returns the tri-state (True/False/None) so the engine can cache it in the
    domain fingerprint and pass it to :func:`verify` for every candidate/row,
    instead of re-running the 3-RCPT catch-all guard per address. Returns None
    (unknown) whenever probing is uninformative or unavailable.
    """
    if strategy in (VerifyStrategy.NO_PROBE, VerifyStrategy.NO_PROBE_ACCEPT_ALL):
        return None
    if not port25_open(timeout=cfg.smtp_connect_timeout):
        return None
    host = _pick_host(mx)
    if host is None:
        return None
    mail_from = cfg.mail_from or _default_mail_from(cfg)
    domain = mx.domain if mx is not None else ""
    if not domain:
        return None
    return probe_domain_catchall(host, mail_from, domain, cfg)


def verify(
    email: str,
    mx: MXInfo,
    strategy: VerifyStrategy,
    cfg: Config,
    known_catch_all: bool | None = None,
) -> SmtpResult:
    """Top-level provider-aware verification entry point.

    Guard first: if the provider strategy makes RCPT uninformative
    (``NO_PROBE`` for M365, ``NO_PROBE_ACCEPT_ALL`` for Yahoo/SES) or port 25 is
    not open from this host, return ``SmtpResult(unavailable=True)`` WITHOUT
    probing. Otherwise run the real-address RCPT on the chosen host; a 250 on a
    confirmed catch-all domain is downgraded to ``catch_all`` (never ``valid``).

    ``known_catch_all`` lets a caller supply an already-detected catch-all
    tri-state (see :func:`detect_catchall`) so the catch-all guard runs ONCE per
    domain rather than once per candidate; when it is None the guard runs here.
    """
    if strategy in (VerifyStrategy.NO_PROBE, VerifyStrategy.NO_PROBE_ACCEPT_ALL):
        return SmtpResult(
            verdict="unknown",
            reason="verification_unavailable",
            unavailable=True,
        )

    # Run-wide reachability check; cached so a blocked host degrades instantly.
    if not port25_open(timeout=cfg.smtp_connect_timeout):
        return SmtpResult(
            verdict="unknown",
            reason="verification_unavailable",
            unavailable=True,
        )

    host = _pick_host(mx)
    if host is None:
        return SmtpResult(
            verdict="unknown",
            reason="verification_unavailable",
            unavailable=True,
        )

    mail_from = cfg.mail_from or _default_mail_from(cfg)
    domain = mx.domain if mx is not None else email.rsplit("@", 1)[-1]

    # Use a caller-supplied catch-all verdict when present (fingerprint-once);
    # otherwise run the catch-all guard in a session of its own (best-effort).
    if known_catch_all is None:
        is_catch_all = probe_domain_catchall(host, mail_from, domain, cfg)
    else:
        is_catch_all = known_catch_all

    result = probe_rcpt(host, mail_from, email, cfg)

    # A 250 on a confirmed catch-all domain proves nothing about this mailbox.
    if is_catch_all is True and result.verdict == "valid":
        result.verdict = "catch_all"
        result.reason = "catch_all_domain"
    return result


# --------------------------------------------------------------------------- #
# Module-private helpers                                                       #
# --------------------------------------------------------------------------- #


def _probe_session(
    host: str, mail_from: str, addrs: list[str], cfg: Config
) -> list[SmtpResult] | None:
    """Open one SMTP session and RCPT each address in ``addrs`` sequentially.

    Reuses a single EHLO + MAIL FROM triplet for all recipients (same server
    mood), never sending DATA/VRFY. Returns one :class:`SmtpResult` per address,
    or ``None`` if the connection could not be established / the session broke
    (treated as ``unavailable`` by callers).
    """
    import smtplib
    import socket

    smtp = None
    try:
        # Short connect timeout so a port-25 block is detected fast; the socket
        # timeout is relaxed to the command timeout once connected (dossier 2.4).
        smtp = smtplib.SMTP(timeout=cfg.smtp_connect_timeout)
        smtp.connect(host, 25)
        try:
            if smtp.sock is not None:
                smtp.sock.settimeout(cfg.smtp_cmd_timeout)
        except OSError:
            pass

        ehlo_name = cfg.ehlo_hostname or socket.getfqdn()
        code, _ = smtp.ehlo(ehlo_name)
        if code >= 400:
            smtp.helo(ehlo_name)

        smtp.mail(mail_from)

        out: list[SmtpResult] = []
        for addr in addrs:
            code, msg = smtp.rcpt(addr)
            enhanced = _parse_enhanced(msg)
            verdict, reason = _map_rcpt(code, enhanced)
            out.append(
                SmtpResult(
                    code=code,
                    enhanced=enhanced,
                    verdict=verdict,
                    reason=reason,
                    unavailable=False,
                )
            )

        try:
            smtp.quit()  # graceful; never DATA
        except (smtplib.SMTPException, OSError):
            pass
        return out
    except (socket.timeout, TimeoutError, ConnectionError, OSError, smtplib.SMTPException):
        if smtp is not None:
            try:
                smtp.close()
            except OSError:
                pass
        return None


def _map_rcpt(code: int | None, enhanced: str | None) -> tuple[str, str]:
    """Map an SMTP reply (code + RFC 3463 enhanced code) to (verdict, reason).

    Never returns ``invalid`` for anything but an honest ``5.1.1`` / ``5.1.10``.
    """
    if code is None:
        return ("unknown", "no_response")
    if 200 <= code < 300:  # 250 / 251
        return ("valid", "accepted")
    if 400 <= code < 500:  # transient: greylist / defer (451 4.7.1, 421, 450, 452)
        return ("retry", "transient_deferral")
    if code >= 500:
        if enhanced in ("5.1.1", "5.1.10"):
            return ("invalid", "mailbox_not_found")
        if enhanced == "5.4.1":
            # M365 DBEB / edge access decision — not a reliable pre-send signal.
            return ("non_signal", "edge_access_decision")
        if enhanced and enhanced.startswith("5.7"):
            return ("non_signal", "sender_policy")
        if code == 552:
            return ("non_signal", "storage_or_size")
        # Any other permanent reply: stay conservative, not a mailbox signal.
        return ("non_signal", "unrecognized_permanent")
    return ("unknown", "unmapped")


def _parse_enhanced(msg) -> str | None:
    """Extract an RFC 3463 enhanced status code from an SMTP reply message."""
    if msg is None:
        return None
    if isinstance(msg, (bytes, bytearray)):
        text = msg.decode("utf-8", "replace")
    else:
        text = str(msg)
    m = _ENHANCED_RE.search(text)
    return m.group(1) if m else None


def _pick_host(mx: MXInfo | None) -> str | None:
    """Choose the host to probe.

    Defers to :func:`emailfinder.provider.pick_probe_host` when that peer module
    is present, otherwise falls back to the lowest-preference (first) MX host.
    """
    if mx is None:
        return None
    try:
        from . import provider  # peer; may not exist yet during isolated builds

        host = provider.pick_probe_host(mx)
        if host:
            return host
    except Exception:
        pass
    return mx.hosts[0] if mx.hosts else None


def _random_local() -> str:
    """High-entropy guaranteed-fake local part (dossier 3.1)."""
    rnd = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    return f"zzq{rnd}-noexist-{int(time.time())}"


def _default_mail_from(cfg: Config) -> str:
    """Synthesize a MAIL FROM when none is configured.

    A real deliverable probe address on an SPF/PTR-clean domain you control is
    strongly preferred (dossier 2.5); this fallback only keeps the session valid.
    """
    import socket

    host = cfg.ehlo_hostname or socket.getfqdn() or "localhost"
    return f"probe@{host}"
