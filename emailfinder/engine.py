"""The orchestrator and single stable API every surface calls.

:class:`Engine` wires the PURE core (normalize / names / templates / candidates /
ranking / provider / filters / scoring) and the I/O modules (dns_mx / smtp_probe /
cache / kb_store / compliance / provider registry) into ONE fixed, known-good
pipeline. There are no reorderable stages and the engine holds NO scoring logic
of its own — every 0-100 score / status decision lives in :mod:`emailfinder.scoring`.

Expensive-to-build state (KB overlay, static filter sets, nickname table, global
priors, the SQLite cache, the compliance gate, the optional provider registry) is
built ONCE in :meth:`Engine.__init__` and reused across every :meth:`find` call
and every batch row.

Safety invariants honored here (research dossier 2.2 / 4.3 / 5 / 8.1):
  * a timeout / 4xx / port-25 block is ``verification_unavailable`` -> pattern-only,
    NEVER flipped to invalid;
  * Microsoft 365 and catch-all domains are hard-capped by scoring and can never
    be reported DELIVERABLE;
  * ``known_bad_locals`` force UNDELIVERABLE;
  * a LinkedIn URL is ONLY ever slug-parsed locally — no network I/O ever touches
    linkedin.com.
"""
from __future__ import annotations

from typing import Iterable, Iterator

from . import (
    candidates as _candidates,  # noqa: F401  (kept for pipeline documentation)
    compliance as _compliance,
    dns_mx,
    filters,
    kb_store,
    names,
    normalize,
    provider as provider_mod,
    ranking,
    scoring,
    smtp_probe,
    templates,
)
from .cache import Cache
from .config import Config
from .models import (
    Candidate,
    DomainFingerprint,
    FindResult,
    MXInfo,
    Provider,
    ScoredCandidate,
    SmtpResult,
    VerifyStrategy,
)
from .providers.base import (
    FOUND_CATCH_ALL,
    FOUND_VERIFIED,
    FindRequest,
)
from .providers.registry import build_registry

# How many top-ranked candidates the engine will actually SMTP-probe before it
# stops (short-circuiting on the first honest 250, per the data flow). Kept small
# so a live probe run never hammers a mail server.
_MAX_PROBE = 3


class Engine:
    """Reusable orchestrator over the frozen find/verify pipeline."""

    def __init__(self, cfg: Config) -> None:
        """Build the compliance silo and load every reusable resource once.

        Resolves the per-user silo (KB overlay, cache, provenance, shared
        suppression list) via :class:`~emailfinder.compliance.Compliance`, copies
        the packaged seed KB into the overlay on first run, loads the static
        filter sets and nickname table, opens the SQLite cache, and (only when
        providers are enabled in ``cfg``) builds the paid-provider registry.
        """
        self.cfg = cfg
        self.compliance = _compliance.Compliance(
            cfg.user_id, cfg.data_dir, cfg.retention_days
        )
        paths = self.compliance.silo_paths()
        self._kb_path = paths["kb"]
        seed_path = cfg.package_data_dir / "domain_kb.seed.json"
        self.kb = kb_store.load_kb(self._kb_path, seed_path)
        self.static_sets = filters.load_static_sets(cfg.package_data_dir)
        self.nicknames = names.load_nicknames(cfg.package_data_dir / "nicknames.json")
        self.priors = templates.global_priors()
        self.cache = Cache(paths["cache"])
        # build_registry is inert when providers are disabled (the default).
        self.registry = build_registry(cfg, self.cache)

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
        verify: bool = False,
        use_providers: bool = False,
    ) -> FindResult:
        """Run the full fixed pipeline for one person and return a FindResult.

        Stages (never reordered): local LinkedIn slug parse -> compliance
        suppression gate (early ``suppressed=True``) -> domain resolution ->
        cached fingerprint / MX resolve + provider classify -> strategy ->
        KB lookup -> normalize + variant expansion -> ranking -> filters ->
        optional SMTP verify (only ``verify`` AND ``PROBE`` AND port-25 open) ->
        optional paid providers (only ``use_providers`` AND ``should_route``) ->
        per-candidate scoring + ranking -> provenance -> on DELIVERABLE, KB upsert.
        """
        # --- 1. name resolution (LinkedIn URLs are slug-parsed LOCALLY) ------ #
        name_str, linkedin_slug = self._resolve_name(name, first, last, linkedin_url)

        query: dict = {
            "name": name_str,
            "first": first,
            "last": last,
            "domain": domain,
            "company": company,
            "linkedin_url": linkedin_url,
            "linkedin_slug": linkedin_slug,
            "verify": verify,
            "use_providers": use_providers,
        }

        # --- 2. compliance suppression gate (before ANY processing) --------- #
        if self.compliance.is_suppressed(None, name_str, domain):
            return FindResult(
                query=query,
                domain=domain,
                provider=Provider.NONE_UNKNOWN,
                strategy=provider_mod.strategy_for(Provider.NONE_UNKNOWN),
                suppressed=True,
                notes=["identity on global suppression list — opted out"],
            )

        # --- 3. domain resolution (arg, else best-effort from company) ------ #
        resolved_domain = domain
        if not resolved_domain and company:
            resolved_domain = dns_mx.resolve_domain_for_company(company, self.cfg)

        if not resolved_domain:
            return FindResult(
                query=query,
                domain=None,
                provider=Provider.NONE_UNKNOWN,
                strategy=provider_mod.strategy_for(Provider.NONE_UNKNOWN),
                notes=["no domain: pass --domain or an unambiguous --company"],
            )
        resolved_domain = resolved_domain.strip().lower()
        query["domain"] = resolved_domain

        # --- 3b. suppression gate AGAIN now the domain is known ------------- #
        # (the first gate at step 2 could not build a name@domain key when only
        #  --company was given; this closes that path).
        if self.compliance.is_suppressed(None, name_str, resolved_domain):
            return FindResult(
                query=query,
                domain=resolved_domain,
                provider=Provider.NONE_UNKNOWN,
                strategy=provider_mod.strategy_for(Provider.NONE_UNKNOWN),
                suppressed=True,
                notes=["identity on global suppression list — opted out"],
            )

        # --- 4. per-domain fingerprint (cache-first, resolve once) ---------- #
        prov, mx = self._fingerprint(resolved_domain)
        kb_entry = kb_store.get_entry(self.kb, resolved_domain)
        override_notes: list[str] = []

        # If the KB records a Microsoft 365 backend, keep M365 caps / NO_PROBE
        # even when the live MX is fronted by a gateway (Proofpoint/Mimecast/...).
        # A gateway may accept-all and defer to M365, so trusting its RCPT could
        # certify an M365 mailbox DELIVERABLE — the invariant we must never break.
        if (
            kb_entry
            and kb_entry.get("provider") == Provider.MICROSOFT365.value
            and prov != Provider.MICROSOFT365
        ):
            override_notes.append(
                f"KB records Microsoft 365 for {resolved_domain}; keeping M365 "
                f"caps despite live '{prov.value}' MX (gateway-fronted tenant)"
            )
            prov = Provider.MICROSOFT365

        strategy = provider_mod.strategy_for(prov)
        catch_all = self._cached_catchall(resolved_domain)

        # --- 5. name pipeline + ranking ------------------------------------ #
        parsed = names.parse_name(name_str)
        variants = names.expand_variants(parsed, self.nicknames)
        cands = ranking.rank(
            variants,
            kb_entry,
            self.priors,
            threshold=self.cfg.kb_dominance_threshold,
        )

        # --- 6. filters: drop role locals; compute domain-level flags ------- #
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
            base_flags["dns_failure"] = True          # permanent -> UNDELIVERABLE
        elif mx.error == "dns_timeout":
            base_flags["dns_unavailable"] = True       # transient -> UNKNOWN, pattern-only
        else:
            base_flags["mx_ok"] = bool(mx.hosts)

        notes: list[str] = list(override_notes)
        if mx.error == "dns_timeout":
            notes.append("DNS temporarily unresolvable — results are pattern-only (UNKNOWN)")
        if not cands:
            notes.append("no candidate local parts could be generated for this name")

        # --- 7. optional SMTP verification (never marks a timeout invalid) -- #
        smtp_by_local: dict[str, SmtpResult] = {}
        verification_mode = "none"
        if (
            verify
            and cands
            and strategy == VerifyStrategy.PROBE
            and smtp_probe.port25_open(timeout=self.cfg.smtp_connect_timeout)
        ):
            verification_mode = "verification_unavailable"
            # Detect the domain's catch-all status ONCE (fingerprint-once), cache
            # it, and pass it into every per-candidate verify so the 3-RCPT
            # catch-all guard does not re-run for each candidate/row.
            if catch_all is None:
                catch_all = smtp_probe.detect_catchall(mx, strategy, self.cfg)
                self._store_catchall(resolved_domain, prov, mx, catch_all)
            for cand in cands[:_MAX_PROBE]:
                email = f"{cand.local_part}@{resolved_domain}"
                res = smtp_probe.verify(
                    email, mx, strategy, self.cfg, known_catch_all=catch_all
                )
                smtp_by_local[cand.local_part] = res
                if res.unavailable:
                    # Port-25 blocked/timeout: the whole run is unavailable.
                    smtp_by_local = {}
                    verification_mode = "verification_unavailable"
                    break
                verification_mode = "smtp"
                if res.verdict == "valid":
                    break  # honest 250 short-circuit
                if res.verdict == "invalid":
                    continue  # honest hard bounce -> try the next candidate
                break  # catch_all / retry / non_signal: stop probing

        # --- 8. optional paid providers (only where guessing can't help) ---- #
        provider_smtp: SmtpResult | None = None
        provider_cand: Candidate | None = None
        if use_providers and not self.registry.is_empty():
            has_kb_pattern = self._has_kb_pattern(kb_entry)
            if self.registry.should_route(prov, catch_all, has_kb_pattern):
                req = FindRequest(
                    first_name=first or parsed.first,
                    last_name=last or parsed.last,
                    full_name=name_str,
                    domain=resolved_domain,
                    company_name=company,
                    linkedin_url=linkedin_url,
                )
                pres = self.registry.find_with_fallback(req, prov, catch_all)
                if pres is not None and pres.email:
                    provider_cand, provider_smtp = self._provider_candidate(pres)
                    if pres.status == FOUND_VERIFIED:
                        verification_mode = "provider"
                    if pres.pattern_hint:
                        notes.append(f"provider pattern hint: {pres.pattern_hint}")

        if provider_cand is not None:
            # A provider-found address leads the candidate list.
            cands = [provider_cand] + [
                c for c in cands if c.local_part != provider_cand.local_part
            ]

        # --- 9. scoring (all scoring logic lives in scoring.py) ------------- #
        scored: list[ScoredCandidate] = []
        for cand in cands:
            local = cand.local_part
            flags = dict(base_flags)
            flags["is_role"] = filters.is_role_local(local, self.static_sets["role"])
            flags["known_bad"] = filters.in_known_bad(local, kb_entry)
            if cand.source == "provider":
                smtp = provider_smtp
            else:
                smtp = smtp_by_local.get(local)
            kb_match = cand.source == "kb"
            sc = scoring.score_candidate(
                cand,
                prov,
                strategy,
                catch_all,
                smtp,
                flags,
                self.cfg.score,
                kb_match,
            )
            sc._domain = resolved_domain  # power ScoredCandidate.email
            scored.append(sc)

        ranked = scoring.rank_scored(scored)

        # --- 9b. suppress opted-out ADDRESSES the pipeline just generated ---- #
        # An email-only opt-out (no name) is only checkable once we know the
        # actual guessed address. Drop any generated candidate whose address is
        # on the global suppression list; if that removes every candidate, the
        # whole result is suppressed (never return an opted-out address).
        supp_emails, _supp_ids = self.compliance.load_suppression_sets()
        if supp_emails:
            kept = [
                sc for sc in ranked
                if f"{sc.candidate.local_part}@{resolved_domain}" not in supp_emails
            ]
            if scored and not kept:
                return FindResult(
                    query=query,
                    domain=resolved_domain,
                    provider=prov,
                    strategy=strategy,
                    mx=mx,
                    suppressed=True,
                    notes=notes + ["all candidate addresses are on the global "
                                   "suppression list — opted out"],
                )
            ranked = kept

        best = ranked[0] if ranked else None
        alternates = ranked[1:] if len(ranked) > 1 else []

        # --- 10. provenance + feedback ------------------------------------- #
        query["provider"] = prov.value
        chosen = best.candidate if best is not None else None
        record = self.compliance.build_provenance(
            query, mx, chosen, verification_mode, best.reasons if best else []
        )
        provenance_id = self.compliance.log_provenance(record)

        from .models import Status  # local import: avoid widening the top block

        if best is not None and best.status == Status.DELIVERABLE and chosen is not None:
            kb_store.upsert_verified(
                self.kb,
                self._kb_path,
                resolved_domain,
                chosen.template,
                chosen.separator,
                prov,
                chosen.local_part,
            )

        return FindResult(
            query=query,
            domain=resolved_domain,
            provider=prov,
            strategy=strategy,
            best=best,
            alternates=alternates,
            mx=mx,
            verification_mode=verification_mode,
            provenance_id=provenance_id,
            suppressed=False,
            notes=notes,
        )

    # --------------------------------------------------------------- batch
    def find_batch(self, rows: Iterable[dict]) -> Iterator[FindResult]:
        """Yield a FindResult per input row, preserving input order.

        MX resolution, provider classification and the catch-all fingerprint
        happen ONCE per distinct domain across the whole batch: the SQLite cache
        de-duplicates domain fingerprints, and a small per-batch memo resolves
        each distinct ``company`` to a domain only once. Each row is a dict with
        any of ``name`` / ``first`` / ``last`` / ``domain`` / ``company`` /
        ``linkedin_url`` (+ optional per-row ``verify`` / ``use_providers``).
        """
        company_memo: dict[str, str | None] = {}
        for row in rows:
            row = dict(row or {})
            domain = _clean(row.get("domain"))
            company = _clean(row.get("company"))
            # Resolve each distinct company to a domain at most once per batch.
            if not domain and company:
                key = company.lower()
                if key not in company_memo:
                    company_memo[key] = dns_mx.resolve_domain_for_company(
                        company, self.cfg
                    )
                domain = company_memo[key]

            yield self.find(
                row.get("name"),
                domain,
                first=row.get("first"),
                last=row.get("last"),
                company=company,
                linkedin_url=row.get("linkedin_url"),
                verify=bool(row.get("verify", False)),
                use_providers=bool(row.get("use_providers", False)),
            )

    # ------------------------------------------------------------- feedback
    def confirm(self, email: str, domain: str, *, deliverable: bool) -> None:
        """Fold a human/DSN confirmation back into the per-user KB overlay.

        ``deliverable=True`` upserts the address as a verified example (bumping
        the domain's shape distribution + ``no_bounce_locals``); otherwise banks
        the local part into ``known_bad_locals`` so future guesses of it are
        forced UNDELIVERABLE. Used by the web confirm buttons and the re-scorer.
        """
        local = (email or "").split("@", 1)[0].strip().lower()
        dom = (domain or "").strip().lower()
        if not local or not dom:
            return
        if deliverable:
            fp = self.cache.get_domain(dom, ttl_days=self.cfg.domain_cache_ttl_days)
            prov = fp.provider if fp is not None else Provider.NONE_UNKNOWN
            kb_store.upsert_verified(
                self.kb, self._kb_path, dom, "", "", prov, local
            )
        else:
            kb_store.append_known_bad(
                self.kb, self._kb_path, dom, local, "confirmed_bad"
            )

    def close(self) -> None:
        """Flush the KB overlay to the silo and close the SQLite cache."""
        try:
            kb_store.save_kb(self.kb, self._kb_path)
        finally:
            self.cache.close()

    # --------------------------------------------------------- private helpers
    def _resolve_name(
        self,
        name: str | None,
        first: str | None,
        last: str | None,
        linkedin_url: str | None,
    ) -> tuple[str, str | None]:
        """Derive the working name string + the local LinkedIn slug (if any).

        A LinkedIn URL is ALWAYS slug-parsed locally (never fetched); the slug is
        only turned into a name when no explicit name / first / last was given.
        """
        linkedin_slug: str | None = None
        if linkedin_url and normalize.is_linkedin_url(linkedin_url):
            linkedin_slug = normalize.parse_linkedin_slug(linkedin_url)

        if name and name.strip():
            return name.strip(), linkedin_slug
        joined = " ".join(t for t in (first, last) if t and t.strip()).strip()
        if joined:
            return joined, linkedin_slug
        if linkedin_slug:
            return normalize.slug_to_name(linkedin_slug), linkedin_slug
        return "", linkedin_slug

    def _fingerprint(self, domain: str) -> tuple[Provider, MXInfo]:
        """Return ``(provider, MXInfo)`` for a domain, resolving DNS at most once.

        On a cache hit the stored fingerprint is reused (MX rebuilt from the
        stored hosts + flags). On a miss, DNS is resolved, the provider is
        classified from the MX host list, and the fingerprint is cached.
        """
        fp = self.cache.get_domain(domain, ttl_days=self.cfg.domain_cache_ttl_days)
        if fp is not None:
            mx = MXInfo(
                domain=domain,
                hosts=list(fp.mx),
                is_implicit=bool(fp.flags.get("is_implicit", False)),
                error=fp.flags.get("dns_error"),
            )
            return fp.provider, mx

        mx = dns_mx.resolve_mx(domain, timeout=self.cfg.dns_timeout)
        prov = provider_mod.classify_provider(mx.hosts)
        fp = DomainFingerprint(
            domain=domain,
            provider=prov,
            mx=list(mx.hosts),
            is_catch_all=None,
            flags={"is_implicit": mx.is_implicit, "dns_error": mx.error},
        )
        # Never cache a TRANSIENT DNS failure — that would sticky a momentary
        # timeout into a 14-day dead fingerprint. Only successful resolutions and
        # definitive NXDOMAIN/no-record negatives are cached.
        if mx.error != "dns_timeout":
            self.cache.put_domain(fp)
        return prov, mx

    def _cached_catchall(self, domain: str) -> bool | None:
        """Return the cached catch-all tri-state for a domain (None if unknown)."""
        fp = self.cache.get_domain(domain, ttl_days=self.cfg.domain_cache_ttl_days)
        return fp.is_catch_all if fp is not None else None

    def _store_catchall(
        self, domain: str, prov: Provider, mx: MXInfo, is_catch_all: bool | None
    ) -> None:
        """Persist a detected catch-all tri-state into the domain fingerprint so
        later candidates/rows reuse it instead of re-probing (fingerprint-once)."""
        if is_catch_all is None:
            return
        self.cache.put_domain(
            DomainFingerprint(
                domain=domain,
                provider=prov,
                mx=list(mx.hosts),
                is_catch_all=is_catch_all,
                flags={"is_implicit": mx.is_implicit, "dns_error": mx.error},
            )
        )

    def _has_kb_pattern(self, kb_entry: dict | None) -> bool:
        """True when the KB has a confident (>= threshold) dominant pattern."""
        if not kb_entry:
            return False
        _shape, share = ranking.dominant_share(kb_entry)
        return share >= self.cfg.kb_dominance_threshold

    def _provider_candidate(self, pres) -> tuple[Candidate, SmtpResult | None]:
        """Turn a paid-provider find result into a candidate + synthetic signal.

        The synthetic SmtpResult lets the shared scorer apply the SAME caps
        (M365 / catch-all can never be DELIVERABLE) to a provider-verified hit.
        """
        local = pres.email.split("@", 1)[0].strip().lower()
        cand = Candidate(
            local_part=local,
            template="provider",
            separator="",
            shape="",
            prior=0.95,
            source="provider",
            name_origin="provider",
        )
        if pres.status == FOUND_VERIFIED:
            smtp: SmtpResult | None = SmtpResult(
                code=250, verdict="valid", reason="provider_verified"
            )
        elif pres.status == FOUND_CATCH_ALL:
            smtp = SmtpResult(verdict="catch_all", reason="provider_catch_all")
        else:
            smtp = None
        return cand, smtp


def _clean(value) -> str | None:
    """Trim a possibly-None cell to a non-empty string, else None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
