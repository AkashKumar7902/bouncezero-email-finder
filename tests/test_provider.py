"""Tests for emailfinder.provider — pure MX-list classification + strategy.

Covers the MODULE_CONTRACTS test-plan bullet for provider.py:
  classify_provider gateway precedence (opengov -> PROOFPOINT), lowest-pref
  backend tie-break over SES-inbound (navi -> GOOGLE_WORKSPACE), iphmx ->
  CISCO_IRONPORT, outlook -> MICROSOFT365, mimecast -> MIMECAST, empty ->
  NONE_UNKNOWN, unmatched -> OTHER, and strategy_for M365 -> NO_PROBE.
"""
from __future__ import annotations

from emailfinder.models import MXInfo, Provider, VerifyStrategy
from emailfinder.provider import (
    classify_provider,
    load_provider_map,
    pick_probe_host,
    strategy_for,
)


# --- classify_provider ------------------------------------------------------

def test_gateway_wins_over_backend_opengov():
    # opengov lists both pphosted (gateway) + aspmx (google backend).
    hosts = ["mx0a-00229901.pphosted.com", "aspmx.l.google.com"]
    assert classify_provider(hosts) is Provider.PROOFPOINT


def test_ses_inbound_loses_to_lower_pref_backend_navi():
    # navi: google primary (lower preference) + SES-inbound secondary.
    hosts = ["aspmx.l.google.com", "inbound-smtp.us-east-1.amazonaws.com"]
    assert classify_provider(hosts) is Provider.GOOGLE_WORKSPACE


def test_ironport_iphmx():
    assert classify_provider(["dc-1a2b.iphmx.com"]) is Provider.CISCO_IRONPORT


def test_microsoft365_outlook():
    hosts = ["amadeus-com.mail.protection.outlook.com"]
    assert classify_provider(hosts) is Provider.MICROSOFT365


def test_mimecast():
    assert classify_provider(["us-smtp-inbound-1.mimecast.com"]) is Provider.MIMECAST


def test_empty_is_none_unknown():
    assert classify_provider([]) is Provider.NONE_UNKNOWN


def test_unmatched_is_other():
    assert classify_provider(["mail.vanitymx.example"]) is Provider.OTHER


def test_case_insensitive():
    assert classify_provider(["ASPMX.L.GOOGLE.COM"]) is Provider.GOOGLE_WORKSPACE


def test_trailing_dot_and_whitespace_tolerated():
    assert classify_provider([" aspmx.l.google.com. "]) is Provider.GOOGLE_WORKSPACE


def test_consumer_gmail_specific_suffix_not_swallowed_by_google_com():
    # gmail-smtp-in.l.google.com must resolve to consumer_gmail, not the broader
    # google.com -> google_workspace suffix (longest-suffix precedence).
    assert classify_provider(["gmail-smtp-in.l.google.com"]) is Provider.CONSUMER_GMAIL


def test_zoho_and_yahoo_and_ses_only():
    assert classify_provider(["mx.zoho.in"]) is Provider.ZOHO
    assert classify_provider(["mta5.am0.yahoodns.net"]) is Provider.YAHOO_AOL
    assert classify_provider(["inbound-smtp.eu-west-1.amazonaws.com"]) is Provider.AMAZON_SES


# --- strategy_for -----------------------------------------------------------

def test_strategy_probe_family():
    for p in (
        Provider.GOOGLE_WORKSPACE,
        Provider.PROOFPOINT,
        Provider.MIMECAST,
        Provider.CISCO_IRONPORT,
    ):
        assert strategy_for(p) is VerifyStrategy.PROBE


def test_strategy_m365_no_probe():
    assert strategy_for(Provider.MICROSOFT365) is VerifyStrategy.NO_PROBE


def test_strategy_catchall_guard_family():
    for p in (Provider.ZOHO, Provider.BARRACUDA, Provider.OTHER, Provider.NONE_UNKNOWN):
        assert strategy_for(p) is VerifyStrategy.PROBE_WITH_CATCHALL_GUARD


def test_strategy_accept_all_family():
    for p in (Provider.YAHOO_AOL, Provider.AMAZON_SES):
        assert strategy_for(p) is VerifyStrategy.NO_PROBE_ACCEPT_ALL


# --- load_provider_map ------------------------------------------------------

def test_load_provider_map_shape():
    rows = load_provider_map()
    assert rows, "provider map should not be empty"
    suffix, provider, is_gateway = rows[0]
    assert isinstance(suffix, str)
    assert isinstance(provider, Provider)
    assert isinstance(is_gateway, bool)
    # Gateways listed first (precedence): first row is a gateway.
    assert rows[0][2] is True


# --- pick_probe_host --------------------------------------------------------

def test_pick_probe_host_lowest_preference_first():
    mx = MXInfo(domain="acme.com", hosts=["primary.mx.acme.com", "backup.mx.acme.com"])
    assert pick_probe_host(mx) == "primary.mx.acme.com"


def test_pick_probe_host_implicit_a_fallback():
    mx = MXInfo(domain="acme.com", hosts=["acme.com"], is_implicit=True)
    assert pick_probe_host(mx) == "acme.com"


def test_pick_probe_host_none_on_dns_failure():
    mx = MXInfo(domain="acme.com", hosts=[], error="dns_failure")
    assert pick_probe_host(mx) is None


def test_pick_probe_host_none_on_empty_hosts():
    mx = MXInfo(domain="acme.com", hosts=[])
    assert pick_probe_host(mx) is None
