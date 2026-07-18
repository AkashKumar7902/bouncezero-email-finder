"""PURE confidence + status resolution (research dossier section 5).

Combines four inputs into a 0-100 confidence score and a Hunter-style
:class:`~emailfinder.models.Status` label:

    final = w_src * source_evidence + w_dom * domain_pattern_match
            + w_pat * global_pattern_prior + w_smtp * smtp_signal

The score (confidence in the *guess*) is deliberately kept separate from the
status (the deliverability *verdict*), and hard safety caps are applied so that
Microsoft 365 and catch-all domains can NEVER be reported DELIVERABLE — the
dossier's #1 bounce cause (``address_not_found``) is exactly the signal those
providers hide, so a strong pattern match still carries real bounce risk.

Safety invariants enforced here (dossier 2.2 / 4.3 / 5):
  * a timeout / 4xx / port-25 block is UNKNOWN, NEVER "invalid";
  * Microsoft 365 and catch-all domains never reach DELIVERABLE;
  * locals in ``known_bad_locals`` are forced UNDELIVERABLE;
  * an honest ``550 5.1.1/5.1.10`` (incl. DBEB-M365) is a trustworthy invalid.

This module is pure: no I/O, no network. It runs AFTER optional verification.
"""
from __future__ import annotations

from emailfinder.config import ScoreConfig
from emailfinder.models import (
    Candidate,
    Provider,
    ScoredCandidate,
    SmtpResult,
    Status,
    VerifyStrategy,
)

# Providers for which an edge 250 is meaningless (anti-harvest / post-accept
# rule evaluation) so RCPT can never certify DELIVERABLE (dossier 4.2).
_ACCEPT_ALL_PROVIDERS = frozenset(
    {Provider.YAHOO_AOL, Provider.AMAZON_SES}
)

# Candidate shape families considered "normal" for a person guess. Anything
# else (name+digits, other, multi-token) is an unusual shape and scores lower
# on the global-prior path (dossier 5, "unusual shape -> lower").
_STANDARD_SHAPE_HINTS = ("single_token", "first", "last")

# The top global prior (first.last ~= 0.60, dossier 1.2). Global base scores are
# scaled relative to this so first.last lands near the top of the base band.
_TOP_GLOBAL_PRIOR = 0.60

# A KB candidate at or above this prior is the domain's learned DOMINANT pattern;
# below it, it is a KB FALLBACK shape. Mirrors candidates.KB_DOMINANT_PRIOR (kept
# as a local constant so scoring depends only on models + config).
_KB_DOMINANT_PRIOR = 0.9

# Final status ranking for FindResult.best selection (dossier 5).
_STATUS_ORDER = {
    Status.DELIVERABLE: 0,
    Status.RISKY: 1,
    Status.UNKNOWN: 2,
    Status.UNDELIVERABLE: 3,
}


# --------------------------------------------------------------------------- #
# small flag helpers                                                          #
# --------------------------------------------------------------------------- #
def _flag(flags: dict, *keys: str, default: bool = False) -> bool:
    """Return the first present flag among ``keys`` as a bool (liberal reader).

    The engine assembles ``flags`` from a few upstream modules, so accept any of
    several equivalent key spellings and fall back to ``default``.
    """
    if not flags:
        return default
    for key in keys:
        if key in flags:
            return bool(flags[key])
    return default


def _syntax_or_mx_failed(flags: dict) -> bool:
    """True when the address can't leave the building: bad syntax or no MX."""
    if not _flag(flags, "syntax_ok", "syntax_valid", default=True):
        return True
    if _flag(flags, "mx_failure", "dns_failure", "no_mx"):
        return True
    if not _flag(flags, "mx_ok", "has_mx", "mx_resolved", default=True):
        return True
    return False


def _is_m365(provider: Provider) -> bool:
    return provider == Provider.MICROSOFT365


def _is_accept_all(provider: Provider) -> bool:
    return provider in _ACCEPT_ALL_PROVIDERS


def _catch_all_active(is_catch_all: bool | None, smtp: SmtpResult | None) -> bool:
    """A domain is treated as catch-all if the fingerprint says so OR the live
    probe just returned catch_all in this session."""
    if is_catch_all is True:
        return True
    if smtp is not None and not smtp.unavailable and smtp.verdict == "catch_all":
        return True
    return False


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# --------------------------------------------------------------------------- #
# base pattern score                                                          #
# --------------------------------------------------------------------------- #
def _is_unusual_shape(shape: str) -> bool:
    """True for shapes that are not a plausible human name pattern."""
    if not shape:
        return False
    s = shape.lower()
    if "digit" in s or s == "other" or s.startswith("multi"):
        return True
    # dotted/underscored/hyphenated first/last forms and single tokens are fine.
    if any(hint in s for hint in _STANDARD_SHAPE_HINTS):
        return False
    if any(sep in s for sep in (".", "_", "-")):
        return False
    return True


def _base_score(cand: Candidate, cfg: ScoreConfig, kb_match: bool) -> tuple[int, str]:
    """Pattern-evidence base score + a human-readable reason.

    KB match  -> ``kb_match_base`` (75-85) scaled by the candidate prior.
    Otherwise -> ``global_prior_base`` (55-65) scaled by the prior relative to
    the dominant global first.last prior, minus a penalty for unusual shapes.
    """
    prior = _clamp(cand.prior, 0.0, 1.0)
    if kb_match:
        lo, hi = cfg.kb_match_base
        base = lo + (hi - lo) * prior
        kind = "dominant" if prior >= _KB_DOMINANT_PRIOR else "fallback"
        reason = (
            f"KB {kind} pattern '{cand.template}' (sep '{cand.separator or '(none)'}') "
            f"matched -> base {int(round(base))} (prior {prior:.2f})"
        )
        return int(round(_clamp(base, lo, hi))), reason

    lo, hi = cfg.global_prior_base
    frac = _clamp(prior / _TOP_GLOBAL_PRIOR, 0.0, 1.0)
    base = lo + (hi - lo) * frac
    reason = (
        f"global prior for '{cand.template}' -> base {int(round(base))} "
        f"(prior {prior:.2f})"
    )
    if _is_unusual_shape(cand.shape):
        base -= 5
        reason += f"; unusual shape '{cand.shape}' -5"
    return int(round(_clamp(base, 0, hi))), reason


def _deliverable_score(base: int, cfg: ScoreConfig) -> int:
    """Map a verified-honest-250 candidate onto the 90-98 DELIVERABLE band,
    stronger patterns landing higher."""
    lo_base, hi_base = cfg.global_prior_base[0], cfg.kb_match_base[1]
    frac = _clamp((base - lo_base) / max(hi_base - lo_base, 1), 0.0, 1.0)
    return int(round(90 + 8 * frac))


# --------------------------------------------------------------------------- #
# status decision table (dossier 5) — deterministic, independently testable    #
# --------------------------------------------------------------------------- #
def resolve_status(
    score: int,
    provider: Provider,
    is_catch_all: bool | None,
    smtp: SmtpResult | None,
    flags: dict,
) -> Status:
    """Deterministic status decision table, separated from score maths.

    Precedence (first match wins):
      1. UNDELIVERABLE — syntax/MX failure, ``known_bad`` local, honest invalid
         RCPT (550 5.1.1/5.1.10, incl. DBEB-M365), or a zeroed score.
      2. RISKY         — role/disposable overlay (regardless of SMTP), or a
         catch-all domain.
      3. DELIVERABLE   — an honest 250 on a trustworthy provider that is NOT
         Microsoft 365, NOT accept-all, NOT catch-all, NOT webmail.
      4. UNKNOWN       — everything else (M365, accept-all, greylist/retry,
         verification-unavailable, or an unverified pattern-only guess).

    M365 and catch-all can never reach DELIVERABLE because step 3 excludes them.
    """
    flags = flags or {}

    # 1. hard UNDELIVERABLE
    if _syntax_or_mx_failed(flags):
        return Status.UNDELIVERABLE
    if _flag(flags, "known_bad", "in_known_bad"):
        return Status.UNDELIVERABLE
    if smtp is not None and not smtp.unavailable and smtp.verdict == "invalid":
        # honest hard bounce (550 5.1.1/5.1.10) — trustworthy even on DBEB-M365.
        return Status.UNDELIVERABLE
    if score <= 0:
        return Status.UNDELIVERABLE

    # 2. RISKY overlays
    if _flag(flags, "is_role", "role") or _flag(flags, "is_disposable", "disposable"):
        return Status.RISKY
    if _catch_all_active(is_catch_all, smtp):
        return Status.RISKY

    # 3. DELIVERABLE — only an honest 250 on a trustworthy, non-M365,
    #    non-accept-all, non-webmail, non-catch-all provider.
    if (
        smtp is not None
        and not smtp.unavailable
        and smtp.verdict == "valid"
        and not _is_m365(provider)
        and not _is_accept_all(provider)
        and not _flag(flags, "webmail")
    ):
        return Status.DELIVERABLE

    # 4. everything else is honestly UNKNOWN
    return Status.UNKNOWN


# --------------------------------------------------------------------------- #
# main scorer                                                                 #
# --------------------------------------------------------------------------- #
def score_candidate(
    cand: Candidate,
    provider: Provider,
    strategy: VerifyStrategy,
    is_catch_all: bool | None,
    smtp: SmtpResult | None,
    flags: dict,
    cfg: ScoreConfig,
    kb_match: bool,
) -> ScoredCandidate:
    """Score one candidate into a 0-100 confidence + Status with reasons trail.

    Applies, in order: pattern base -> hard failures (syntax/MX, known_bad,
    honest invalid RCPT) -> honest-250 DELIVERABLE -> catch-all / M365 /
    accept-all / greylist / verification-unavailable caps -> role/disposable
    overlay. Every applied rule is appended to ``reasons``.

    Microsoft 365 and catch-all are hard-capped and can never be DELIVERABLE.
    """
    flags = flags or {}
    reasons: list[str] = []

    is_role = _flag(flags, "is_role", "role")
    is_disposable = _flag(flags, "is_disposable", "disposable")
    webmail = _flag(flags, "webmail")
    known_bad = _flag(flags, "known_bad", "in_known_bad")
    catch_all = _catch_all_active(is_catch_all, smtp)

    # --- pattern base ---------------------------------------------------- #
    base, base_reason = _base_score(cand, cfg, kb_match)
    reasons.append(base_reason)
    score = base

    # --- 1. hard UNDELIVERABLE cases ------------------------------------- #
    if _syntax_or_mx_failed(flags):
        reasons.append("syntax/MX resolution failed -> UNDELIVERABLE (0)")
        return _finalize(cand, 0, Status.UNDELIVERABLE, catch_all,
                         is_role, is_disposable, webmail, reasons)

    if known_bad:
        reasons.append("local part in known_bad_locals -> forced UNDELIVERABLE")
        return _finalize(cand, 2, Status.UNDELIVERABLE, catch_all,
                         is_role, is_disposable, webmail, reasons)

    # --- 2. honest SMTP signals (only trustworthy when we actually probed) - #
    if smtp is not None and not smtp.unavailable:
        if smtp.verdict == "invalid":
            reasons.append(
                f"honest hard bounce {smtp.code or ''} {smtp.enhanced or ''}".rstrip()
                + " (mailbox not found) -> UNDELIVERABLE"
            )
            return _finalize(cand, 2, Status.UNDELIVERABLE, catch_all,
                             is_role, is_disposable, webmail, reasons)
        if (
            smtp.verdict == "valid"
            and not catch_all
            and not _is_m365(provider)
            and not _is_accept_all(provider)
            and not webmail
            and not is_role       # role/disposable are a RISKY overlay,
            and not is_disposable  # regardless of a 250 (dossier 5)
        ):
            deliv = _deliverable_score(base, cfg)
            reasons.append(
                f"honest 250 accept, not catch-all -> DELIVERABLE ({deliv})"
            )
            return _finalize(cand, deliv, Status.DELIVERABLE, catch_all,
                             is_role, is_disposable, webmail, reasons)
    elif smtp is not None and smtp.unavailable:
        reasons.append(
            "verification unavailable (port-25 blocked/timeout) -> pattern-only, "
            "never marked invalid"
        )

    # --- 3. caps for unverifiable / unreliable classes -------------------- #
    if catch_all:
        if score > cfg.catchall_cap:
            reasons.append(
                f"catch-all domain -> RISKY, capped at {cfg.catchall_cap} "
                "(pattern-only, never verified)"
            )
        score = min(score, cfg.catchall_cap)
    if _is_m365(provider):
        if score > cfg.m365_cap:
            reasons.append(
                f"Microsoft 365 not RCPT-verifiable -> UNKNOWN, capped at "
                f"{cfg.m365_cap}"
            )
        score = min(score, cfg.m365_cap)
    elif _is_accept_all(provider):
        if score > cfg.accept_all_cap:
            reasons.append(
                f"accept-all provider ({provider.value}) -> edge 250 meaningless, "
                f"capped at {cfg.accept_all_cap}"
            )
        score = min(score, cfg.accept_all_cap)

    if _flag(flags, "dns_unavailable"):
        # A transient DNS timeout/SERVFAIL is NOT proof the domain is dead;
        # pattern-only, capped to UNKNOWN — never a permanent UNDELIVERABLE.
        if score > cfg.m365_cap:
            reasons.append(
                "DNS temporarily unresolvable (transient) -> UNKNOWN, "
                f"capped at {cfg.m365_cap}"
            )
        score = min(score, cfg.m365_cap)

    if smtp is not None and not smtp.unavailable and smtp.verdict == "retry":
        if score > cfg.m365_cap:
            reasons.append(
                f"greylisted/rate-limited (transient 4xx) -> UNKNOWN, capped at "
                f"{cfg.m365_cap}"
            )
        score = min(score, cfg.m365_cap)
    if smtp is not None and not smtp.unavailable and smtp.verdict == "non_signal":
        reasons.append(
            "5.4.1/5.7.x is a non-signal for mailbox existence -> UNKNOWN"
        )

    # --- 4. role/disposable RISKY overlay (regardless of SMTP) ------------ #
    if is_role:
        reasons.append("role/functional local -> RISKY overlay")
    if is_disposable:
        reasons.append("disposable domain -> RISKY overlay")
    if webmail:
        reasons.append("webmail domain -> UNKNOWN (per-mailbox unverifiable)")

    # --- final status + numeric reconciliation --------------------------- #
    status = resolve_status(score, provider, is_catch_all, smtp, flags)
    if status == Status.RISKY:
        # RISKY confidence must never exceed the catch-all cap.
        score = min(score, cfg.catchall_cap)
    elif status == Status.UNDELIVERABLE:
        score = min(score, 2)

    return _finalize(cand, score, status, catch_all,
                     is_role, is_disposable, webmail, reasons)


def _finalize(
    cand: Candidate,
    score: int,
    status: Status,
    is_catch_all: bool,
    is_role: bool,
    is_disposable: bool,
    webmail: bool,
    reasons: list[str],
) -> ScoredCandidate:
    """Assemble the ScoredCandidate, clamping the score to 0-100."""
    return ScoredCandidate(
        candidate=cand,
        score=int(_clamp(score, 0, 100)),
        status=status,
        is_catch_all=bool(is_catch_all),
        is_role=bool(is_role),
        is_disposable=bool(is_disposable),
        webmail=bool(webmail),
        reasons=reasons,
    )


# --------------------------------------------------------------------------- #
# final ordering                                                              #
# --------------------------------------------------------------------------- #
def rank_scored(scored: list[ScoredCandidate]) -> list[ScoredCandidate]:
    """Order candidates DELIVERABLE > RISKY > UNKNOWN > UNDELIVERABLE, then by
    score descending. Stable so equal keys keep generation order; the first
    element is FindResult.best."""
    return sorted(
        scored,
        key=lambda sc: (_STATUS_ORDER.get(sc.status, 99), -sc.score),
    )
