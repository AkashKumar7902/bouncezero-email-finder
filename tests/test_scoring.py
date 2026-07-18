"""Tests for emailfinder.scoring (dossier 5 confidence + status model).

Covers the contract test-plan bullet:
  honest 250 not-catch-all -> DELIVERABLE 90-98; catch-all -> RISKY capped <=58;
  M365 pattern-only -> UNKNOWN capped <=50; honest 5.1.1 -> UNDELIVERABLE ~2;
  known_bad -> UNDELIVERABLE; dns_failure -> 0; reasons[] populated.
  ASSERT M365/catch-all NEVER produce DELIVERABLE.
"""
from __future__ import annotations

import pytest

from emailfinder.config import ScoreConfig
from emailfinder.models import (
    Candidate,
    Provider,
    ScoredCandidate,
    SmtpResult,
    Status,
    VerifyStrategy,
)
from emailfinder.scoring import rank_scored, resolve_status, score_candidate


@pytest.fixture
def cfg() -> ScoreConfig:
    return ScoreConfig()


def _cand(local="ajith.kumar", template="first.last", sep=".", shape="first.last",
          prior=0.9, source="kb") -> Candidate:
    return Candidate(
        local_part=local,
        template=template,
        separator=sep,
        shape=shape,
        prior=prior,
        source=source,
        name_origin="as_given",
    )


def _smtp(verdict="unknown", code=None, enhanced=None, unavailable=False) -> SmtpResult:
    return SmtpResult(code=code, enhanced=enhanced, verdict=verdict,
                      reason="", unavailable=unavailable)


# --------------------------------------------------------------------------- #
# base scoring bands                                                          #
# --------------------------------------------------------------------------- #
def test_kb_match_base_band(cfg):
    sc = score_candidate(_cand(prior=0.9), Provider.GOOGLE_WORKSPACE,
                         VerifyStrategy.PROBE, None, None, {}, cfg, kb_match=True)
    # unverified pattern-only on a probe provider -> UNKNOWN, but high confidence.
    assert sc.status == Status.UNKNOWN
    assert cfg.kb_match_base[0] <= sc.score <= cfg.kb_match_base[1]
    assert sc.reasons


def test_global_prior_lower_than_kb(cfg):
    kb = score_candidate(_cand(prior=0.9, source="kb"), Provider.GOOGLE_WORKSPACE,
                         VerifyStrategy.PROBE, None, None, {}, cfg, kb_match=True)
    glob = score_candidate(_cand(prior=0.60, source="global"),
                           Provider.GOOGLE_WORKSPACE, VerifyStrategy.PROBE,
                           None, None, {}, cfg, kb_match=False)
    assert glob.score < kb.score
    assert cfg.global_prior_base[0] <= glob.score <= cfg.global_prior_base[1]


def test_unusual_shape_penalised(cfg):
    normal = score_candidate(_cand(prior=0.60, shape="first.last", source="global"),
                             Provider.GOOGLE_WORKSPACE, VerifyStrategy.PROBE,
                             None, None, {}, cfg, kb_match=False)
    weird = score_candidate(_cand(local="ajith99", template="name+digits", sep="",
                                  shape="name+digits", prior=0.60, source="global"),
                            Provider.GOOGLE_WORKSPACE, VerifyStrategy.PROBE,
                            None, None, {}, cfg, kb_match=False)
    assert weird.score < normal.score


# --------------------------------------------------------------------------- #
# honest 250 -> DELIVERABLE 90-98                                             #
# --------------------------------------------------------------------------- #
def test_honest_250_not_catchall_deliverable(cfg):
    sc = score_candidate(
        _cand(prior=0.9), Provider.GOOGLE_WORKSPACE, VerifyStrategy.PROBE,
        is_catch_all=False, smtp=_smtp(verdict="valid", code=250),
        flags={}, cfg=cfg, kb_match=True,
    )
    assert sc.status == Status.DELIVERABLE
    assert 90 <= sc.score <= 98
    assert any("DELIVERABLE" in r for r in sc.reasons)


# --------------------------------------------------------------------------- #
# catch-all -> RISKY capped                                                   #
# --------------------------------------------------------------------------- #
def test_catchall_risky_capped(cfg):
    sc = score_candidate(
        _cand(prior=0.9), Provider.GOOGLE_WORKSPACE, VerifyStrategy.PROBE,
        is_catch_all=True, smtp=_smtp(verdict="catch_all", code=250),
        flags={}, cfg=cfg, kb_match=True,
    )
    assert sc.status == Status.RISKY
    assert sc.score <= cfg.catchall_cap
    assert sc.is_catch_all is True


def test_catchall_never_deliverable_even_with_valid_verdict(cfg):
    # A 250 on a domain fingerprinted catch-all must never be DELIVERABLE.
    sc = score_candidate(
        _cand(prior=0.95), Provider.GOOGLE_WORKSPACE, VerifyStrategy.PROBE,
        is_catch_all=True, smtp=_smtp(verdict="valid", code=250),
        flags={}, cfg=cfg, kb_match=True,
    )
    assert sc.status != Status.DELIVERABLE
    assert sc.score <= cfg.catchall_cap


# --------------------------------------------------------------------------- #
# M365 -> UNKNOWN capped, never DELIVERABLE                                   #
# --------------------------------------------------------------------------- #
def test_m365_pattern_only_unknown_capped(cfg):
    sc = score_candidate(
        _cand(prior=0.9), Provider.MICROSOFT365, VerifyStrategy.NO_PROBE,
        is_catch_all=None, smtp=None, flags={}, cfg=cfg, kb_match=True,
    )
    assert sc.status == Status.UNKNOWN
    assert sc.score <= cfg.m365_cap
    assert any("Microsoft 365" in r for r in sc.reasons)


def test_m365_never_deliverable_even_if_probe_leaked_valid(cfg):
    # Defense in depth: even a stray 'valid' verdict must not flip M365 to DELIVERABLE.
    sc = score_candidate(
        _cand(prior=0.95), Provider.MICROSOFT365, VerifyStrategy.NO_PROBE,
        is_catch_all=False, smtp=_smtp(verdict="valid", code=250),
        flags={}, cfg=cfg, kb_match=True,
    )
    assert sc.status != Status.DELIVERABLE
    assert sc.score <= cfg.m365_cap


# --------------------------------------------------------------------------- #
# honest 5.1.1 -> UNDELIVERABLE ~2                                            #
# --------------------------------------------------------------------------- #
def test_honest_551_undeliverable(cfg):
    sc = score_candidate(
        _cand(prior=0.9), Provider.GOOGLE_WORKSPACE, VerifyStrategy.PROBE,
        is_catch_all=False,
        smtp=_smtp(verdict="invalid", code=550, enhanced="5.1.1"),
        flags={}, cfg=cfg, kb_match=True,
    )
    assert sc.status == Status.UNDELIVERABLE
    assert sc.score <= 2


def test_m365_dbeb_511_still_undeliverable(cfg):
    # 550 5.1.1/5.1.10 is trustworthy even on M365 (DBEB).
    sc = score_candidate(
        _cand(prior=0.9), Provider.MICROSOFT365, VerifyStrategy.NO_PROBE,
        is_catch_all=None,
        smtp=_smtp(verdict="invalid", code=550, enhanced="5.1.10"),
        flags={}, cfg=cfg, kb_match=True,
    )
    assert sc.status == Status.UNDELIVERABLE
    assert sc.score <= 2


# --------------------------------------------------------------------------- #
# known_bad -> UNDELIVERABLE ; dns_failure -> 0                               #
# --------------------------------------------------------------------------- #
def test_known_bad_forces_undeliverable(cfg):
    sc = score_candidate(
        _cand(prior=0.9), Provider.GOOGLE_WORKSPACE, VerifyStrategy.PROBE,
        is_catch_all=False, smtp=None, flags={"known_bad": True},
        cfg=cfg, kb_match=True,
    )
    assert sc.status == Status.UNDELIVERABLE
    assert sc.score <= 2
    assert any("known_bad" in r for r in sc.reasons)


def test_dns_failure_zero(cfg):
    sc = score_candidate(
        _cand(prior=0.9), Provider.NONE_UNKNOWN, VerifyStrategy.PROBE,
        is_catch_all=None, smtp=None, flags={"mx_failure": True},
        cfg=cfg, kb_match=True,
    )
    assert sc.status == Status.UNDELIVERABLE
    assert sc.score == 0


def test_syntax_failure_zero(cfg):
    sc = score_candidate(
        _cand(prior=0.9), Provider.GOOGLE_WORKSPACE, VerifyStrategy.PROBE,
        is_catch_all=None, smtp=None, flags={"syntax_ok": False},
        cfg=cfg, kb_match=True,
    )
    assert sc.status == Status.UNDELIVERABLE
    assert sc.score == 0


# --------------------------------------------------------------------------- #
# verification unavailable never marks invalid                                #
# --------------------------------------------------------------------------- #
def test_verification_unavailable_not_invalid(cfg):
    sc = score_candidate(
        _cand(prior=0.9), Provider.GOOGLE_WORKSPACE, VerifyStrategy.PROBE,
        is_catch_all=None, smtp=_smtp(verdict="unknown", unavailable=True),
        flags={}, cfg=cfg, kb_match=True,
    )
    assert sc.status == Status.UNKNOWN
    assert sc.status != Status.UNDELIVERABLE
    assert any("unavailable" in r.lower() for r in sc.reasons)


# --------------------------------------------------------------------------- #
# role / disposable / greylist / accept-all overlays                         #
# --------------------------------------------------------------------------- #
def test_role_overlay_risky(cfg):
    sc = score_candidate(
        _cand(local="careers", template="single_token", sep="", shape="single_token"),
        Provider.GOOGLE_WORKSPACE, VerifyStrategy.PROBE, is_catch_all=False,
        smtp=_smtp(verdict="valid", code=250), flags={"is_role": True},
        cfg=cfg, kb_match=True,
    )
    assert sc.status == Status.RISKY   # role forces RISKY even with a 250
    assert sc.is_role is True
    assert sc.score <= cfg.catchall_cap


def test_disposable_overlay_risky(cfg):
    sc = score_candidate(
        _cand(prior=0.9), Provider.GOOGLE_WORKSPACE, VerifyStrategy.PROBE,
        is_catch_all=False, smtp=None, flags={"is_disposable": True},
        cfg=cfg, kb_match=True,
    )
    assert sc.status == Status.RISKY
    assert sc.is_disposable is True


def test_greylist_retry_unknown_capped(cfg):
    sc = score_candidate(
        _cand(prior=0.9), Provider.GOOGLE_WORKSPACE, VerifyStrategy.PROBE,
        is_catch_all=False, smtp=_smtp(verdict="retry", code=451, enhanced="4.7.1"),
        flags={}, cfg=cfg, kb_match=True,
    )
    assert sc.status == Status.UNKNOWN
    assert sc.score <= cfg.m365_cap


def test_accept_all_provider_capped_unknown(cfg):
    sc = score_candidate(
        _cand(prior=0.9), Provider.YAHOO_AOL, VerifyStrategy.NO_PROBE_ACCEPT_ALL,
        is_catch_all=None, smtp=None, flags={}, cfg=cfg, kb_match=True,
    )
    assert sc.status == Status.UNKNOWN
    assert sc.score <= cfg.accept_all_cap


def test_webmail_not_deliverable(cfg):
    sc = score_candidate(
        _cand(prior=0.9), Provider.CONSUMER_GMAIL, VerifyStrategy.PROBE,
        is_catch_all=False, smtp=_smtp(verdict="valid", code=250),
        flags={"webmail": True}, cfg=cfg, kb_match=True,
    )
    assert sc.status != Status.DELIVERABLE
    assert sc.webmail is True


# --------------------------------------------------------------------------- #
# INVARIANT: M365 / catch-all NEVER DELIVERABLE (exhaustive sweep)            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("verdict", ["valid", "catch_all", "retry", "unknown", "non_signal"])
def test_invariant_m365_never_deliverable(cfg, verdict):
    sc = score_candidate(
        _cand(prior=0.99), Provider.MICROSOFT365, VerifyStrategy.NO_PROBE,
        is_catch_all=None, smtp=_smtp(verdict=verdict, code=250),
        flags={}, cfg=cfg, kb_match=True,
    )
    assert sc.status != Status.DELIVERABLE


@pytest.mark.parametrize("provider", list(Provider))
def test_invariant_catchall_never_deliverable(cfg, provider):
    sc = score_candidate(
        _cand(prior=0.99), provider, VerifyStrategy.PROBE,
        is_catch_all=True, smtp=_smtp(verdict="valid", code=250),
        flags={}, cfg=cfg, kb_match=True,
    )
    assert sc.status != Status.DELIVERABLE
    assert sc.score <= cfg.catchall_cap


# --------------------------------------------------------------------------- #
# resolve_status is independently testable                                    #
# --------------------------------------------------------------------------- #
def test_resolve_status_table():
    assert resolve_status(0, Provider.GOOGLE_WORKSPACE, None, None,
                          {"mx_failure": True}) == Status.UNDELIVERABLE
    assert resolve_status(80, Provider.GOOGLE_WORKSPACE, None, None,
                          {"known_bad": True}) == Status.UNDELIVERABLE
    assert resolve_status(80, Provider.GOOGLE_WORKSPACE, True, None,
                          {}) == Status.RISKY
    assert resolve_status(80, Provider.GOOGLE_WORKSPACE, False,
                          SmtpResult(verdict="valid", code=250),
                          {}) == Status.DELIVERABLE
    assert resolve_status(80, Provider.MICROSOFT365, False,
                          SmtpResult(verdict="valid", code=250),
                          {}) == Status.UNKNOWN
    assert resolve_status(40, Provider.GOOGLE_WORKSPACE, None, None,
                          {}) == Status.UNKNOWN


# --------------------------------------------------------------------------- #
# rank_scored ordering                                                        #
# --------------------------------------------------------------------------- #
def test_rank_scored_orders_by_status_then_score():
    def mk(status, score):
        return ScoredCandidate(candidate=_cand(), score=score, status=status)

    items = [
        mk(Status.UNDELIVERABLE, 2),
        mk(Status.UNKNOWN, 48),
        mk(Status.DELIVERABLE, 92),
        mk(Status.RISKY, 55),
        mk(Status.DELIVERABLE, 96),
        mk(Status.UNKNOWN, 84),
    ]
    ranked = rank_scored(items)
    statuses = [s.status for s in ranked]
    assert statuses == [
        Status.DELIVERABLE, Status.DELIVERABLE, Status.RISKY,
        Status.UNKNOWN, Status.UNKNOWN, Status.UNDELIVERABLE,
    ]
    # within DELIVERABLE, higher score first
    assert ranked[0].score == 96
    # within UNKNOWN, higher score first
    assert ranked[3].score == 84
