"""HostedFinder — the public web app's find/opt-out/KB service over the PURE core.

A deliberately SIMPLER sibling of :class:`emailfinder.engine.Engine`: no SMTP
probe, no paid providers, no per-user silo. Every accuracy decision still comes
from the shared pure modules (normalize / names / templates / candidates /
ranking / provider / filters / scoring); this module only orchestrates them
against a :class:`~webapp.store.Store` for persistence and returns a fully
JSON-serializable dict.

Hard invariants (inherited from the core, honored here):
  * Verification is OFF — this module NEVER imports or calls
    :mod:`emailfinder.smtp_probe`; ``smtp`` is always ``None`` and
    ``verification_mode`` is always ``"none"``.
  * Microsoft 365 and catch-all domains are hard-capped by the scorer and can
    never be reported DELIVERABLE.
  * A LinkedIn URL is ONLY ever slug-parsed locally — zero network I/O against
    linkedin.com.
  * The global suppression list is honored before AND after generation.
"""
from __future__ import annotations

import time

from emailfinder import (
    dns_mx,
    filters,
    names,
    normalize,
    provider as provider_mod,
    ranking,
    scoring,
    templates,
)
from emailfinder.config import Config, load_config
from emailfinder.models import (
    DomainFingerprint,
    MXInfo,
    Provider,
    ScoredCandidate,
    VerifyStrategy,
)

# --------------------------------------------------------------------------- #
# Human-facing labels (copied verbatim from emailfinder/web.py — same wording).
# --------------------------------------------------------------------------- #
_PROVIDER_LABELS: dict[str, str] = {
    Provider.MICROSOFT365.value: "Microsoft 365",
    Provider.GOOGLE_WORKSPACE.value: "Google Workspace",
    Provider.CONSUMER_GMAIL.value: "Gmail",
    Provider.PROOFPOINT.value: "Proofpoint",
    Provider.MIMECAST.value: "Mimecast",
    Provider.CISCO_IRONPORT.value: "Cisco IronPort",
    Provider.BARRACUDA.value: "Barracuda",
    Provider.ZOHO.value: "Zoho",
    Provider.AMAZON_SES.value: "Amazon SES",
    Provider.YAHOO_AOL.value: "Yahoo / AOL",
    Provider.OTHER.value: "Other",
    Provider.NONE_UNKNOWN.value: "Unknown",
}


def _provider_label(provider: Provider) -> str:
    """Human-readable badge label for a Provider enum."""
    return _PROVIDER_LABELS.get(provider.value, provider.value)


def _cap_note(provider: Provider, is_catch_all: bool) -> str | None:
    """Return the honest cap note for M365 / catch-all, else None.

    These are the two cases the scorer hard-caps and the UI MUST surface so a
    user never mistakes a pattern-only guess for a verified address.
    """
    if provider == Provider.MICROSOFT365:
        return "capped: Microsoft 365 not RCPT-verifiable"
    if is_catch_all:
        return "catch-all: pattern-only"
    return None


# --------------------------------------------------------------------------- #
# Serialization helpers
# --------------------------------------------------------------------------- #
def _mx_to_dict(mx: MXInfo | None) -> dict | None:
    """Serialize an MXInfo for the JSON response (or None)."""
    if mx is None:
        return None
    return {
        "domain": mx.domain,
        "hosts": list(mx.hosts),
        "is_implicit": mx.is_implicit,
        "error": mx.error,
    }


def _scored_to_dict(
    sc: ScoredCandidate | None, domain: str | None, provider: Provider
) -> dict | None:
    """Serialize one ScoredCandidate with its reasons[] + honest cap note."""
    if sc is None:
        return None
    cand = sc.candidate
    local = cand.local_part
    email = f"{local}@{domain}" if domain else local
    return {
        "email": email,
        "local_part": local,
        "template": cand.template,
        "separator": cand.separator,
        "score": sc.score,
        "status": sc.status.value,
        "is_catch_all": sc.is_catch_all,
        "is_role": sc.is_role,
        "is_disposable": sc.is_disposable,
        "webmail": sc.webmail,
        "reasons": list(sc.reasons),
        "cap_note": _cap_note(provider, sc.is_catch_all),
    }


class HostedFinder:
    """Stateless-per-call finder wrapping the pure core + a persistence Store.

    The expensive, reusable resources (static filter sets, nickname table,
    global priors) are built ONCE at construction and shared across every
    :meth:`find`; all mutable state lives in the injected :class:`Store`.
    """

    def __init__(self, store, cfg: Config | None = None) -> None:
        self.store = store
        self.cfg = cfg if cfg is not None else load_config()
        self.static_sets = filters.load_static_sets(self.cfg.package_data_dir)
        self.nicknames = names.load_nicknames(
            self.cfg.package_data_dir / "nicknames.json"
        )
        self.priors = templates.global_priors()

    # ------------------------------------------------------------------ find
    def find(
        self,
        name: str | None = None,
        domain: str | None = None,
        *,
        first: str | None = None,
        last: str | None = None,
        company: str | None = None,
        linkedin_url: str | None = None,
    ) -> dict:
        """Run the hosted pipeline for one person; return a JSON-serializable dict.

        Stages (never reordered): LinkedIn slug local-parse -> suppression gate
        -> domain resolve -> suppression gate -> DNS/provider fingerprint (store
        cache) -> M365-KB override -> KB lookup -> normalize + variant expansion
        -> ranking -> filters -> per-candidate scoring (``smtp=None``) ->
        rank_scored -> suppression-address filter -> log_lookup -> dict.
        """
        # --- 1. name resolution (LinkedIn URLs are slug-parsed LOCALLY) ------ #
        name_str = self._resolve_name(name, first, last, linkedin_url)
        domain = (domain or "").strip().lower() or None

        # --- 2. suppression gate (before ANY processing) -------------------- #
        if self.store.is_suppressed(None, name_str, domain):
            return self._suppressed_result(
                domain,
                Provider.NONE_UNKNOWN,
                provider_mod.strategy_for(Provider.NONE_UNKNOWN),
                notes=["identity on global suppression list — opted out"],
            )

        # --- 3. domain resolution (arg, else best-effort from company) ------ #
        resolved_domain = domain
        if not resolved_domain and company:
            resolved_domain = dns_mx.resolve_domain_for_company(company, self.cfg)

        if not resolved_domain:
            return self._empty_result(
                None,
                Provider.NONE_UNKNOWN,
                provider_mod.strategy_for(Provider.NONE_UNKNOWN),
                notes=["no domain: pass a domain or a resolvable company"],
            )
        resolved_domain = resolved_domain.strip().lower()

        # --- 4. suppression gate AGAIN now the domain is known -------------- #
        if self.store.is_suppressed(None, name_str, resolved_domain):
            return self._suppressed_result(
                resolved_domain,
                Provider.NONE_UNKNOWN,
                provider_mod.strategy_for(Provider.NONE_UNKNOWN),
                notes=["identity on global suppression list — opted out"],
            )

        # --- 5. per-domain fingerprint (store cache first, resolve once) ---- #
        prov, mx, catch_all = self._fingerprint(resolved_domain)
        kb_entry = self.store.get_kb_entry(resolved_domain)
        notes: list[str] = []

        # --- 6. M365-KB override: keep M365 caps even behind a gateway ------- #
        if (
            kb_entry
            and kb_entry.get("provider") == Provider.MICROSOFT365.value
            and prov != Provider.MICROSOFT365
        ):
            notes.append(
                f"KB records Microsoft 365 for {resolved_domain}; keeping M365 "
                f"caps despite live '{prov.value}' MX (gateway-fronted tenant)"
            )
            prov = Provider.MICROSOFT365

        # --- 7. strategy --------------------------------------------------- #
        strategy = provider_mod.strategy_for(prov)

        # --- 8. name pipeline + ranking ------------------------------------ #
        parsed = names.parse_name(name_str)
        variants = names.expand_variants(parsed, self.nicknames)
        cands = ranking.rank(
            variants,
            kb_entry,
            self.priors,
            threshold=self.cfg.kb_dominance_threshold,
        )

        # --- 9. filters: drop role locals; compute domain-level flags ------- #
        cands = [
            c
            for c in cands
            if not filters.is_role_local(c.local_part, self.static_sets["role"])
        ]
        dom_disposable = filters.is_disposable_domain(
            resolved_domain, self.static_sets["disposable"]
        )
        dom_webmail = filters.is_webmail(resolved_domain, self.static_sets["webmail"])
        base_flags: dict = {
            "syntax_ok": True,
            "is_disposable": dom_disposable,
            "webmail": dom_webmail,
        }
        if mx.error == "dns_failure":
            base_flags["dns_failure"] = True       # permanent -> UNDELIVERABLE
        elif mx.error == "dns_timeout":
            base_flags["dns_unavailable"] = True    # transient -> UNKNOWN, pattern-only
        else:
            base_flags["mx_ok"] = bool(mx.hosts)

        if mx.error == "dns_timeout":
            notes.append(
                "DNS temporarily unresolvable — results are pattern-only (UNKNOWN)"
            )
        if not cands:
            notes.append("no candidate local parts could be generated for this name")

        # --- 10. scoring (smtp=None — verification is OFF) ------------------ #
        scored: list[ScoredCandidate] = []
        for cand in cands:
            local = cand.local_part
            flags = dict(base_flags)
            flags["is_role"] = filters.is_role_local(local, self.static_sets["role"])
            flags["known_bad"] = filters.in_known_bad(local, kb_entry)
            sc = scoring.score_candidate(
                cand,
                prov,
                strategy,
                catch_all,
                None,  # smtp: verification is OFF in the hosted app
                flags,
                self.cfg.score,
                cand.source == "kb",
            )
            sc._domain = resolved_domain  # power ScoredCandidate.email
            scored.append(sc)

        ranked = scoring.rank_scored(scored)

        # --- 11. suppress opted-out ADDRESSES the pipeline just generated --- #
        supp_emails = self.store.suppression_emails()
        if supp_emails:
            kept = [
                sc
                for sc in ranked
                if f"{sc.candidate.local_part}@{resolved_domain}" not in supp_emails
            ]
            if scored and not kept:
                return self._suppressed_result(
                    resolved_domain,
                    prov,
                    strategy,
                    mx=mx,
                    notes=notes
                    + [
                        "all candidate addresses are on the global "
                        "suppression list — opted out"
                    ],
                )
            ranked = kept

        best = ranked[0] if ranked else None
        alternates = ranked[1:] if len(ranked) > 1 else []

        # --- 12. audit log + JSON result ----------------------------------- #
        self.store.log_lookup(
            {
                "ts": time.time(),
                "name": name_str,
                "domain": resolved_domain,
                "local_part": best.candidate.local_part if best is not None else None,
                "linkedin_url": linkedin_url,
                "provider": prov.value,
                "reasons": list(best.reasons) if best is not None else [],
                "ip_hash": None,
            }
        )

        return {
            "suppressed": False,
            "domain": resolved_domain,
            "provider": prov.value,
            "provider_label": _provider_label(prov),
            "strategy": strategy.value,
            "verification_mode": "none",
            "best": _scored_to_dict(best, resolved_domain, prov),
            "alternates": [
                _scored_to_dict(a, resolved_domain, prov) for a in alternates
            ],
            "mx": _mx_to_dict(mx),
            "notes": notes,
        }

    # -------------------------------------------------------------- opt-out
    def optout(
        self,
        email: str | None = None,
        name: str | None = None,
        domain: str | None = None,
    ) -> None:
        """Add an identity / address to the global suppression list (no login)."""
        self.store.add_suppression(email, name, domain, "web-optout")

    # ---------------------------------------------------------------- KB read
    def kb_entry(self, domain: str) -> dict | None:
        """Return the KB entry for ``domain`` (case-insensitive) or None."""
        return self.store.get_kb_entry((domain or "").strip().lower())

    # --------------------------------------------------------- private helpers
    def _resolve_name(
        self,
        name: str | None,
        first: str | None,
        last: str | None,
        linkedin_url: str | None,
    ) -> str:
        """Derive the working name string; LinkedIn URLs are slug-parsed LOCALLY.

        The slug is only turned into a name when no explicit name / first / last
        was given. A LinkedIn URL is NEVER fetched.
        """
        linkedin_slug: str | None = None
        if linkedin_url and normalize.is_linkedin_url(linkedin_url):
            linkedin_slug = normalize.parse_linkedin_slug(linkedin_url)

        if name and name.strip():
            return name.strip()
        joined = " ".join(t for t in (first, last) if t and t.strip()).strip()
        if joined:
            return joined
        if linkedin_slug:
            return normalize.slug_to_name(linkedin_slug)
        return ""

    def _fingerprint(
        self, domain: str
    ) -> tuple[Provider, MXInfo, bool | None]:
        """Return ``(provider, MXInfo, is_catch_all)``, resolving DNS at most once.

        On a store-cache hit the stored fingerprint is reused (MX rebuilt from the
        stored hosts + flags). On a miss, DNS is resolved, the provider is
        classified from the MX host list, and the fingerprint is cached — except a
        TRANSIENT ``dns_timeout`` is NEVER cached (it must not sticky).
        """
        fp = self.store.get_domain_fp(domain, self.cfg.domain_cache_ttl_days)
        if fp is not None:
            mx = MXInfo(
                domain=domain,
                hosts=list(fp.mx),
                is_implicit=bool(fp.flags.get("is_implicit", False)),
                error=fp.flags.get("dns_error"),
            )
            return fp.provider, mx, fp.is_catch_all

        mx = dns_mx.resolve_mx(domain, timeout=self.cfg.dns_timeout)
        prov = provider_mod.classify_provider(mx.hosts)
        fp = DomainFingerprint(
            domain=domain,
            provider=prov,
            mx=list(mx.hosts),
            is_catch_all=None,
            flags={"is_implicit": mx.is_implicit, "dns_error": mx.error},
        )
        if mx.error != "dns_timeout":
            self.store.put_domain_fp(fp)
        return prov, mx, None

    # -- result builders --------------------------------------------------- #
    def _empty_result(
        self,
        domain: str | None,
        provider: Provider,
        strategy: VerifyStrategy,
        *,
        suppressed: bool = False,
        mx: MXInfo | None = None,
        notes: list[str] | None = None,
    ) -> dict:
        """Build a candidate-less JSON result (no-domain / suppressed paths)."""
        return {
            "suppressed": suppressed,
            "domain": domain,
            "provider": provider.value,
            "provider_label": _provider_label(provider),
            "strategy": strategy.value,
            "verification_mode": "none",
            "best": None,
            "alternates": [],
            "mx": _mx_to_dict(mx),
            "notes": list(notes or []),
        }

    def _suppressed_result(
        self,
        domain: str | None,
        provider: Provider,
        strategy: VerifyStrategy,
        *,
        mx: MXInfo | None = None,
        notes: list[str] | None = None,
    ) -> dict:
        """Build a ``suppressed=True`` JSON result (never returns an address)."""
        return self._empty_result(
            domain, provider, strategy, suppressed=True, mx=mx, notes=notes
        )
