"""I/O: MX / A / AAAA resolution — the ONE DNS entry point for the package.

Implements the dossier 2.1 fallback chain with a short timeout:

    MX lookup (sort ascending by preference, probe lowest first)
      └─ no MX?  implicit MX: fall back to A/AAAA as preference-0 host (RFC 5321 §5.1)
           └─ neither resolves? → error='dns_failure', do NOT probe (caller → UNDELIVERABLE)

Everything here is mockable in tests by patching :func:`resolve_mx`. dnspython is
an always-available dependency, so it is imported at module top level (unlike the
optional deps elsewhere in the package). NEVER performs any network I/O against
linkedin.com — company/domain slug work is purely offline (only a live MX confirm
against the *guessed* corporate domain touches the network).
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import dns.exception
import dns.resolver

from .config import Config
from .models import MXInfo

__all__ = ["resolve_mx", "resolve_domain_for_company"]

# DNS failures that are DEFINITIVE (the domain/records genuinely do not exist):
# these map to a permanent ``dns_failure`` -> UNDELIVERABLE.
_PERMANENT_DNS = (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.YXDOMAIN)
# DNS failures that are TRANSIENT (timeout / SERVFAIL / no reachable nameserver):
# these map to ``dns_timeout`` -> UNKNOWN, never a permanent negative verdict.
_TRANSIENT_DNS = (dns.exception.Timeout, dns.resolver.LifetimeTimeout,
                  dns.resolver.NoNameservers)

# Ordered public TLD guesses for a bare company name (dossier: .com first, then
# regional .in for the India-heavy audit corpus, then .io for tech startups).
_COMPANY_TLDS = (".com", ".in", ".io")

# Corporate-form / legal-suffix noise tokens dropped before slugifying a company
# name. Kept deliberately small; this is a best-effort guess, not a registry.
_COMPANY_STOPWORDS = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "llp",
        "ltd",
        "limited",
        "pvt",
        "private",
        "corp",
        "corporation",
        "co",
        "company",
        "gmbh",
        "plc",
        "the",
        "technologies",
        "technology",
        "solutions",
        "systems",
        "software",
        "labs",
        "group",
        "holdings",
        "international",
    }
)


def resolve_mx(domain: str, timeout: float = 5.0) -> MXInfo:
    """Resolve mail exchangers for ``domain`` per the dossier 2.1 fallback chain.

    Returns an :class:`MXInfo` with ``hosts`` sorted ascending by MX preference
    (lowest preference first, i.e. the primary mail server). When the domain has
    no MX records, falls back to treating the domain itself as an implicit
    preference-0 host if it has an A or AAAA record (RFC 5321 §5.1), with
    ``is_implicit=True``. When neither MX nor A/AAAA resolves — including for a
    non-existent domain (NXDOMAIN) — sets ``error='dns_failure'`` and returns an
    empty host list. NEVER raises for NXDOMAIN or any other DNS failure; the
    caller treats ``error`` as UNDELIVERABLE.

    A timeout or transient DNS failure is also reported as ``dns_failure`` (an
    unavailable/unknown signal), never silently as a resolvable domain.
    """
    name = _normalize_domain(domain)
    if not name:
        return MXInfo(domain=domain, hosts=[], is_implicit=False, error="dns_failure")

    resolver = _make_resolver(timeout)

    # 1) MX records, sorted ascending by preference.
    mx_hosts, mx_transient = _query_mx(resolver, name)
    if mx_hosts:
        return MXInfo(domain=name, hosts=mx_hosts, is_implicit=False, error=None)

    # 2) Implicit MX: the domain itself, if it has an A or AAAA address record.
    has_addr, addr_transient = _has_address_record(resolver, name)
    if has_addr:
        return MXInfo(domain=name, hosts=[name], is_implicit=True, error=None)

    # 3) Nothing resolved. Distinguish a DEFINITIVE negative (NXDOMAIN / no
    #    records -> permanent ``dns_failure`` -> UNDELIVERABLE) from a TRANSIENT
    #    failure (timeout / SERVFAIL -> ``dns_timeout`` -> UNKNOWN). A momentary
    #    resolver hiccup must never permanently condemn a real domain.
    error = "dns_timeout" if (mx_transient or addr_transient) else "dns_failure"
    return MXInfo(domain=name, hosts=[], is_implicit=False, error=error)


def resolve_domain_for_company(company: str, cfg: Config) -> str | None:
    """Best-effort corporate-domain guess from a company name (NO scraping).

    Slugifies ``company`` offline, then tries ``<slug>.com``, ``<slug>.in`` and
    ``<slug>.io`` in order, accepting a candidate ONLY when a live MX (or implicit
    A/AAAA) resolves for it. Returns the domain when exactly one candidate has a
    live mail exchanger. Returns ``None`` when the guess is ambiguous — no
    candidate resolves, or more than one does — so the calling surface can prompt
    the user for an explicit domain instead of silently guessing wrong.

    This is the only place a *guessed* domain is touched over the network, and it
    only ever resolves DNS for ``<slug>.<tld>``; it never contacts linkedin.com
    or performs any HTTP scraping.
    """
    # 1) Optional company->domain map (only if the user has provided one at
    #    <package_data_dir>/company_domains.json; none ships by default). A
    #    normalized-name hit is unambiguous and needs no DNS guessing.
    seed = _load_company_map(str(cfg.package_data_dir))
    key = _norm_company_key(company)
    if key and key in seed:
        return seed[key]

    slug = _slugify_company(company)
    if not slug:
        return None

    # 2) DNS guess: try each TLD in preference order (.com, .in, .io) and accept
    #    the FIRST one with a live mail exchanger. Preferring .com beats treating
    #    a brand that defensively registers several TLDs as "ambiguous -> None".
    for tld in _COMPANY_TLDS:
        candidate = slug + tld
        info = resolve_mx(candidate, timeout=cfg.dns_timeout)
        if info.error is None and info.hosts:
            return candidate
    return None


@lru_cache(maxsize=8)
def _load_company_map(data_dir_str: str) -> dict[str, str]:
    """Load the packaged ``company_domains.json`` seed as ``{norm_name: domain}``
    (cached). Returns ``{}`` when the seed is absent."""
    path = Path(data_dir_str) / "company_domains.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    out: dict[str, str] = {}
    for norm_key, entry in raw.items():
        domain = entry.get("domain") if isinstance(entry, dict) else None
        if domain:
            out[str(norm_key)] = str(domain).strip().lower()
    return out


def _norm_company_key(company: str) -> str:
    """Normalized company key matching the seed map's keys (alnum, lowercased)."""
    return re.sub(r"[^a-z0-9]", "", (company or "").lower())


# --------------------------------------------------------------------------- #
# Module-private helpers
# --------------------------------------------------------------------------- #
def _make_resolver(timeout: float) -> dns.resolver.Resolver:
    """A resolver bounded by a short per-query and total (lifetime) timeout so a
    blackholed nameserver cannot hang the whole batch."""
    resolver = dns.resolver.Resolver()
    # ``timeout`` bounds a single nameserver attempt; ``lifetime`` bounds the
    # whole resolution across retries/multiple nameservers.
    resolver.timeout = timeout
    resolver.lifetime = timeout
    return resolver


def _query_mx(resolver: dns.resolver.Resolver, name: str) -> tuple[list[str], bool]:
    """Return ``(hosts, transient)``: MX exchange hostnames sorted ascending by
    preference, and whether the (empty) result was due to a TRANSIENT failure.

    A definitive negative (NXDOMAIN / no records) returns ``([], False)``; a
    timeout / SERVFAIL / no-reachable-nameserver returns ``([], True)`` so the
    caller can surface ``dns_timeout`` instead of a permanent ``dns_failure``.
    """
    try:
        answer = resolver.resolve(name, "MX")
    except _PERMANENT_DNS:
        return [], False
    except _TRANSIENT_DNS:
        return [], True
    except dns.exception.DNSException:
        return [], True  # unclassified DNS error -> treat as transient (safe)

    records = sorted(answer, key=lambda r: r.preference)
    hosts: list[str] = []
    for rec in records:
        host = str(rec.exchange).rstrip(".")
        # A single "." exchange is RFC 7505 "null MX" — the domain explicitly
        # accepts no mail; treat as no usable host.
        if host and host not in hosts:
            hosts.append(host)
    return hosts, False


def _has_address_record(resolver: dns.resolver.Resolver, name: str) -> tuple[bool, bool]:
    """Return ``(has_record, transient)`` for A/AAAA lookups (implicit-MX check).

    ``has_record`` is True if an A or AAAA record exists. ``transient`` is True
    only when every attempt failed transiently (timeout/SERVFAIL) and none was a
    definitive negative — so a real address record is never masked by a hiccup.
    """
    transient = False
    for rrtype in ("A", "AAAA"):
        try:
            answer = resolver.resolve(name, rrtype)
        except _PERMANENT_DNS:
            continue
        except (_TRANSIENT_DNS + (dns.exception.DNSException,)):
            transient = True
            continue
        if len(answer) > 0:
            return True, False
    return False, transient


def _normalize_domain(domain: str) -> str:
    """Lowercase, strip whitespace and a trailing dot; '' for junk input."""
    if not domain:
        return ""
    name = domain.strip().lower().rstrip(".")
    # Guard against an accidental scheme / path being passed as a "domain".
    name = name.split("/")[0].split("@")[-1]
    return name.strip()


def _slugify_company(company: str) -> str:
    """Offline slug from a company name: ASCII-fold, drop legal/noise suffixes,
    keep alphanumerics concatenated (e.g. 'Acme Technologies, Inc.' -> 'acme')."""
    if not company:
        return ""
    # ASCII-fold accents with stdlib (optional romanizers live in normalize.py).
    folded = unicodedata.normalize("NFKD", company)
    folded = folded.encode("ascii", "ignore").decode("ascii")
    tokens = re.split(r"[^a-z0-9]+", folded.lower())
    kept = [t for t in tokens if t and t not in _COMPANY_STOPWORDS]
    if not kept:
        # Everything was a stopword; fall back to the raw alphanumerics so we
        # still produce *some* guess rather than nothing.
        kept = [t for t in tokens if t]
    return "".join(kept)
