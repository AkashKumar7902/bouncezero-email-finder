"""PURE provider classification + strategy mapping from an MX host LIST.

No DNS happens here — ``dns_mx`` supplies the (already preference-sorted) host
list, keeping this unit trivially testable. This module ports the audit's
``classify_provider`` and extends it with the dossier-4.1 precedence rules
(security gateways win over their backend; SES-inbound loses to a
lower-preference backend), then maps each :class:`Provider` to its dossier-4.2
:class:`VerifyStrategy`.

The suffix -> provider table is data-driven (``data/provider_map.json``) so the
precedence order is editable without touching code.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from emailfinder.config import PACKAGE_DATA_DIR
from emailfinder.models import MXInfo, Provider, VerifyStrategy

# Dossier 4.2 reliability class per provider. Providers not listed here fall
# back to the guarded-probe default (safest: never trust a bare 250).
_STRATEGY_BY_PROVIDER: dict[Provider, VerifyStrategy] = {
    Provider.GOOGLE_WORKSPACE: VerifyStrategy.PROBE,
    Provider.PROOFPOINT: VerifyStrategy.PROBE,
    Provider.MIMECAST: VerifyStrategy.PROBE,
    Provider.CISCO_IRONPORT: VerifyStrategy.PROBE,
    Provider.CONSUMER_GMAIL: VerifyStrategy.PROBE,  # honest 5.1.1 (rate-limited)
    Provider.MICROSOFT365: VerifyStrategy.NO_PROBE,
    Provider.ZOHO: VerifyStrategy.PROBE_WITH_CATCHALL_GUARD,
    Provider.BARRACUDA: VerifyStrategy.PROBE_WITH_CATCHALL_GUARD,
    Provider.OTHER: VerifyStrategy.PROBE_WITH_CATCHALL_GUARD,
    Provider.NONE_UNKNOWN: VerifyStrategy.PROBE_WITH_CATCHALL_GUARD,
    Provider.YAHOO_AOL: VerifyStrategy.NO_PROBE_ACCEPT_ALL,
    Provider.AMAZON_SES: VerifyStrategy.NO_PROBE_ACCEPT_ALL,
}


def _default_map_path() -> Path:
    return PACKAGE_DATA_DIR / "provider_map.json"


@lru_cache(maxsize=8)
def _load_map_cached(path_str: str) -> tuple[tuple[str, Provider, bool], ...]:
    """Cached JSON load keyed by the resolved path string."""
    raw = json.loads(Path(path_str).read_text())
    rows: list[tuple[str, Provider, bool]] = []
    for suffix, provider_value, is_gateway in raw.get("rows", []):
        rows.append((str(suffix).lower(), Provider(provider_value), bool(is_gateway)))
    return tuple(rows)


def load_provider_map(path: Path | None = None) -> list[tuple[str, Provider, bool]]:
    """Load ``data/provider_map.json`` as ordered ``(suffix, Provider, is_gateway)``.

    Gateways are listed first so precedence stays data-driven and editable. The
    result is cached per resolved path.
    """
    resolved = Path(path) if path is not None else _default_map_path()
    return list(_load_map_cached(str(resolved)))


def _match_host(host: str, rows: list[tuple[str, Provider, bool]]) -> tuple[Provider, bool] | None:
    """Return the (provider, is_gateway) for the LONGEST matching suffix.

    Longest-suffix wins so a specific host like ``gmail-smtp-in.l.google.com``
    (consumer_gmail) is not swallowed by the broader ``google.com`` suffix
    (google_workspace). A match is either exact or a dotted-label suffix, so
    ``notgoogle.com`` never matches ``google.com``.
    """
    h = host.strip().lower().rstrip(".")
    if not h:
        return None
    best: tuple[int, Provider, bool] | None = None
    for suffix, provider, is_gateway in rows:
        if h == suffix or h.endswith("." + suffix):
            if best is None or len(suffix) > best[0]:
                best = (len(suffix), provider, is_gateway)
    if best is None:
        return None
    return best[1], best[2]


def classify_provider(mx_hosts: list[str]) -> Provider:
    """Classify the mail provider from a preference-sorted MX host list.

    Precedence (dossier 4.1):

    1. Any security-gateway suffix (pphosted/proofpoint/mimecast/iphmx/cisco/
       barracuda) WINS over the backend it fronts (e.g. opengov's
       ``pphosted`` + ``aspmx`` -> PROOFPOINT).
    2. Otherwise the first non-gateway backend match in preference order wins,
       EXCEPT an Amazon-SES-inbound MX loses to any lower-preference (earlier,
       primary) non-SES backend (navi/rapido -> GOOGLE_WORKSPACE).
    3. Empty host list -> NONE_UNKNOWN; hosts present but nothing matches ->
       OTHER.
    """
    if not mx_hosts:
        return Provider.NONE_UNKNOWN

    rows = load_provider_map()
    gateway_matches: list[Provider] = []
    backend_matches: list[Provider] = []  # in host (preference) order
    for host in mx_hosts:
        matched = _match_host(host, rows)
        if matched is None:
            continue
        provider, is_gateway = matched
        if is_gateway:
            gateway_matches.append(provider)
        else:
            backend_matches.append(provider)

    # Rule 1: a gateway anywhere in the MX set wins over any backend.
    if gateway_matches:
        return gateway_matches[0]

    if not backend_matches:
        return Provider.OTHER

    # Rule 2: SES-inbound loses to a lower-preference (earlier) non-SES backend.
    for provider in backend_matches:
        if provider is not Provider.AMAZON_SES:
            return provider
    # Only SES-inbound backends present.
    return backend_matches[0]


def strategy_for(provider: Provider) -> VerifyStrategy:
    """Map a :class:`Provider` to its dossier-4.2 :class:`VerifyStrategy`.

    ``{google_workspace, proofpoint, mimecast, cisco_ironport}`` -> PROBE;
    ``microsoft365`` -> NO_PROBE; ``{zoho, barracuda, other}`` ->
    PROBE_WITH_CATCHALL_GUARD; ``{yahoo_aol, amazon_ses}`` ->
    NO_PROBE_ACCEPT_ALL. Anything unlisted defaults to the guarded probe.
    """
    return _STRATEGY_BY_PROVIDER.get(provider, VerifyStrategy.PROBE_WITH_CATCHALL_GUARD)


def pick_probe_host(mx: MXInfo) -> str | None:
    """Return the lowest-preference host to probe (dossier 2.1).

    ``mx.hosts`` is already sorted ascending by preference, so the primary host
    is ``hosts[0]``. This honors the implicit-MX A/AAAA fallback (which arrives
    as a single preference-0 host). Returns None when there is no usable host
    (DNS failure / empty list).
    """
    if mx is None or mx.error or not mx.hosts:
        return None
    return mx.hosts[0]
