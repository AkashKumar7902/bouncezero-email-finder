# RESEARCH DOSSIER — LinkedIn Email Finder + Verifier

**Audience:** engineers building a MailMeteor-style finder/verifier
**Grounding:** 3,006-address / 654-message / 560-bounce cold-outreach audit (`summary.md`, `domain_kb.json`, 219 domains, 210 companies)
**Status of claims:** only verification-surviving claims retained; corrections applied inline; residual uncertainties flagged `[UNCERTAIN]`.

---

## 0. Ground-truth anchors (from the audit — treat as fact)

| Fact | Value |
|---|---|
| Local-part shapes | first.last **76%** (2313), single_token **14%** (421), first.l **6%** (200), first_last 0.7% (22), name+digits 20, f.last 19, first-last 2 |
| Separators | dot **2535**, none 442, underscore **27**, hyphen **2** |
| #1 bounce cause | `address_not_found` (550 5.1.1) — 352 (wrong guess) |
| #2 bounce cause | `recipient_rejected` (550 5.4.1) — 134, of which 118 on Microsoft 365 |
| Honest-RCPT providers | google_workspace (175 true not-founds), proofpoint (77), cisco_ironport (52), mimecast (15) |
| RCPT-useless provider | microsoft365 — 118 policy 5.4.1 vs only **3** true 5.1.1 |
| Total bounce rate | 18.6% (560/3006) — ~9× the 2% safe line |

**Master rule the whole system pivots on:** *the meaning of an SMTP result depends on the provider behind the MX.* Classify provider first, then decide whether SMTP is even informative.

---

## 1. Candidate generation rules + ordered permutation list

### 1.1 Two-tier ranking (CONFIRMED — the single biggest accuracy lever)
1. **Domain-learned pattern wins.** Look the domain up in the KB. If a `dominant_shape` has ≥60% share, generate **only that shape + 1–2 fallbacks** and assign a high prior (~0.9).
2. **Global priors only when domain unknown.** Emit the full ordered list below.

**Store the literal template string AND the separator, never just the shape family:**
- `single_token` is ambiguous: opengov.com/visa.com = **flast** (`achauhan`, `asingh`), not bare-first. amadeus single_token cases differ.
- Separator is NOT universally a dot: **trimble.com uses underscore** (`ajith_c`, `bharat_dwarkani`). The KB override must carry `dominant_separator` (see `purplle.com` → `.`, `opengov.com` → `(none)`).

### 1.2 Ordered default list (domain unknown) — seed priors, dot forced
Generate all, rank by prior, dedupe after name-variant expansion. Given `{first, last, domain}`:

| # | template | shape | prior |
|---|---|---|---|
| 1 | `first.last` | first.last | 0.60 |
| 2 | `flast` (f+last) | single_token | 0.12 |
| 3 | `first` | single_token | 0.08 |
| 4 | `firstlast` | single_token | 0.05 |
| 5 | `first.l` | first.l | 0.04 |
| 6 | `firstl` | first_l/none | 0.03 |
| 7 | `f.last` | f.last | 0.02 |
| 8 | `last.first` | first.last-rev | 0.015 |
| 9 | `last` | single_token | 0.01 |
| 10 | `lastf` | single_token | 0.01 |
| 11 | `first_last` | underscore | 0.01 |
| 12 | `first-last` | hyphen | 0.005 |
| 13 | `lastfirst` | single_token | 0.005 |
| 14 | `last.f` | f.last-rev | 0.005 |

- These are **seed priors to be learned**, not audit-derived exact frequencies. Force **dot** as the primary separator; keep underscore/hyphen near-zero unless the KB says otherwise.
- **Size conditioning `[UNCERTAIN — heuristic]`:** for SMB/startup domains (few employees, if a LinkedIn headcount signal exists), boost bare-`first`; the broader market shows first-only dominant under ~50 employees. For large-enterprise, keep first.last dominant. Blend the Interseller 10,001+ table (first.last 56%, flast 22%) for domains outside the audit's large-enterprise/India profile.

### 1.3 Token pipeline — runs BEFORE permutation, strict order (CONFIRMED)
1. **Normalize:** Unicode NFKD, strip diacritics / transliterate to ASCII (`José→jose`, `Müller→muller`, `Nguyễn→nguyen`) via a unidecode-style map. For non-Latin scripts (Cyrillic/Arabic/CJK) keep the **top-2 romanizations** as extra candidates.
2. **Clean:** lowercase; strip punctuation/apostrophes (`O'Brien→obrien`); strip titles/suffixes (`Dr`, `Jr`, `III`).
3. **Nickname expansion** (bidirectional table: Bob↔Robert, Bill↔William, Kate/Katie↔Katherine, Alex↔Alexander/Alexandra…). Generate candidates for **as-given first, then formal, then other diminutives**. (Anglocentric — low value for Indian names; budget for false candidates.)
4. **Surname/middle expansion** (see 1.4).
5. **Dedupe** the cross-product of name-variants × templates.

### 1.4 Compound/hyphenated surnames + middle names (CONFIRMED)
- **Multi-word surname** (`Van Der Berg`, `De Souza`, `García Márquez`): emit join-all (`vanderberg`), last-token-only (`berg`), first-surname-token-only (`garcia`), hyphen/dot joined.
- **Hyphenated** (`Smith-Jones`): `smithjones`, `smith-jones`, `smith`, `jones`.
- **Middle name:** weight **drop-middle** highest; also `first.middle.last`, `firstm+last`, `first.m.last`.

### 1.5 India-specific branch (CONFIRMED, with one correction)
The audit is India-heavy — treat as first-class:
- **FirstName + Initial(s)** (South Indian, initial expands to father/village name — LinkedIn shows the initial): generate `first.<init>`, `first<init>`, and `first`-only. KB evidence: harman `saravanan.gm`; amadeus `ashwath.s`, `bhargav.k`, `apeksha.ss`, `abhishek.kd` (first + collapsed remaining-name initials).
- **Mononyms** (single-name individuals): try `first`-only; **do NOT fabricate a surname**, do not append digits.
- **When only an initial is available, do not invent a full last name.**
- **CORRECTION — do NOT bump first.l domain-class-wide for `.in`/Indian companies.** That over-generalizes from purplle.com (the lone first.l outlier). Most Indian companies in the audit are first.last-dominant: chargebee 35/20, wingify 50/1, navi 48/0, tessell 41/0, easebuzz.in 37/3. Keep **first.last as the Indian default**; rely on the per-domain KB for known first.l orgs; hold first.l only as a #2–3 fallback.

### 1.6 Generation hygiene
- Filter role/functional locals out of person-guessing: `info`, `hr`, `careers`, `hiring`, `sales`, `support`, `admin`, `engineering` (KB shows hackerearth `careers`/`hr`, akamai `hiring`).
- Emit the full deduped set (typically 15–40 after expansion), but **verify in ranked order and short-circuit on first accept**. Never bulk-probe.

---

## 2. Verification engine design (MX → SMTP RCPT)

### 2.1 Probe sequence (CONFIRMED, RFC 5321)
```
MX lookup (sort ascending preference, probe lowest-preference first)
  └─ no MX?  implicit MX: fall back to A/AAAA as preference-0 host (RFC 5321 §5.1)
       └─ neither resolves? → dns_failure, do NOT probe
TCP connect :25  →  read 220 banner (read fully before sending)
EHLO your.fqdn   (real resolvable FQDN matching PTR; HELO only on 5xx)
MAIL FROM:<probe@domain-you-control>
RCPT TO:<candidate@target>   ← THIS reply is the signal
QUIT                          ← never send DATA
```
- **Never send DATA** — that delivers real mail. Stopping at RCPT transmits nothing.
- **VRFY/EXPN are dead** — disabled on modern MTAs (expect 252/502). RCPT TO is the de-facto probe.
- **Connection reuse:** one EHLO+MAIL FROM can carry multiple sequential RCPTs to the same MX (do the catch-all probe and the real-address probe in the **same session** — same triplet, same server mood). Watch for per-session RCPT caps; on `421`/"too many recipients" back off and reconnect.

### 2.2 Reply-code interpretation — map code + RFC 3463 subcode, not the 3-digit alone

| Reply | Meaning | Verdict |
|---|---|---|
| 250 / 251 | accepted | **valid** *only if domain not catch-all and provider honest* |
| 252 | cannot VRFY, will attempt | unknown |
| 550 **5.1.1** / **5.1.10** | mailbox does not exist (`RESOLVER.ADR.RecipientNotFound`) | **invalid** (hard) — trustworthy on honest providers *and* on DBEB-M365 |
| 550/553 **5.4.1** "Access denied" | M365 edge decision (see §4) | **do not read as generic policy**; on a DBEB tenant it is *probable-invalid*, but pre-send it is not reliably separable — default **unknown** unless tenant classified |
| 5.7.x | sender reputation / auth / policy block (about YOU) | non-signal for mailbox existence |
| 421 / 450 / 451 (esp. 451 4.7.1) | greylist / transient defer | **retry** (see §3) |
| 452 | mailbox full / over-quota transient | transient |
| **552** | exceeded storage / message too large | **permanent** (NOT retryable) — corrected from earlier draft; not a mailbox-existence signal |

- A read/connect **timeout = unknown, never invalid.**
- `5.1.10` is the *more common* M365 not-found enhanced code — handle it alongside `5.1.1`.

### 2.3 Port-25 reality (CONFIRMED — a hard architecture constraint)
- Outbound TCP **:25 is blocked** by virtually all residential ISPs and by default on **AWS EC2, Azure PAYG/free, GCP Compute Engine**. It is also blocked on the dev machine.
- **Ports 587/465 are NOT substitutes** — they require AUTH and only let you send *as* an authenticated user; they cannot probe a third-party MX.
- **Symptom of a block:** `connect()` hangs/times out (no RST) → classify **"cannot verify from this host"**, never "invalid".
- **Consequence:** SMTP RCPT verification must live in a **separate worker** on a VPS/bare-metal with :25 open + clean static IP + matching PTR/rDNS + SPF on the probe domain — OR be replaced by a paid HTTPS verifier API (§6). The system must degrade gracefully to `verification_unavailable`, never silently mark addresses invalid.
- Spamhaus **PBL** lists dynamic/residential/cloud ranges as "must not talk direct-to-MX" — probing from a PBL-listed IP produces **false negatives** (drops/tarpits/5xx) regardless of mailbox existence. Probe only from static, un-PBL'd IPs.

### 2.4 Timeouts & retries
- RFC 5321 §4.5.3.2 minimums are generous (5 min); practical values: connect 5–30 s, banner ~30 s, per-command 30–60 s. Use **generous socket timeouts (30–60 s)** to survive tarpitting.
- Retry only 4xx/timeouts (see §3). Reuse the **same source IP + same MAIL FROM** across retries so greylist triplets match.

### 2.5 MAIL FROM choice (corrected)
Use a **real, deliverable probe address** on an SPF/PTR-clean domain you control. The reason is **sender-address-verification (SAV) callbacks and reputation** — *not* because "servers reject `<>`". The null sender `<>` is RFC-legitimate (required for DSNs); the genuine tradeoff:
- Null `<>`: better reputation isolation, but higher outright-reject/greylist rate on some MTAs.
- Real warmed sender: higher acceptance, but couples results to (and can burn) that domain's reputation.
`[UNCERTAIN]` A/B-test both against your actual provider mix.

---

## 3. Catch-all & greylisting handling

### 3.1 Catch-all detection (CONFIRMED)
- **Test:** in the same session as the real probe, RCPT a **high-entropy guaranteed-fake** local part (e.g. `zzq7x8k3n2p9-noexist-<ts>@domain`). If it returns **250 → domain is catch-all**; every specific-address 250 there is meaningless → downgrade to **RISKY / pattern-based only**, never "valid".
- **Confirm with 2–3 distinct randoms** before finalizing "catch-all"; inconsistent results → `unknown/inconsistent`, not catch-all.
- **Limitation:** a random-probe 250 cannot distinguish a true catch-all from a server that 250s at the edge and rejects *after DATA* (deferred rejection — notably some M365/gateway configs). The multi-probe test reduces flukes but cannot detect after-DATA rejection. The only certain catch-all test is sending real mail and watching for a bounce — which you avoid.
- Some Google Workspace tenants and Proofpoint/Mimecast/IronPort gateways *are* configured accept-all → **detection is per-domain, never per-provider.**

### 3.2 Greylisting (CONFIRMED)
- First contact from an unknown (IP, envelope-sender, recipient) triplet → transient **4xx**, canonically **451 4.7.1** (sometimes 421). **Not invalid.**
- Handling: on any 4xx during MAIL/RCPT → classify `greylisted/deferred`, requeue. Retry cadence `[heuristic]`: ~60 s, then 5 m, 15 m, 60 m, cap ~4–6 attempts over a few hours. **Reuse the same IP + MAIL FROM** so the triplet clears. Still 4xx after cap → **unknown** (not invalid).
- A synchronous verifier (seconds) cannot fully clear greylisting — budget an **async retry queue** or those addresses land in `unknown`.

### 3.3 Distinguish greylist vs tarpit vs rate-limit vs blocklist
| Symptom | Signal | Response |
|---|---|---|
| Greylist | fast 4xx (451 4.7.1), clears on same-triplet retry | requeue + backoff |
| Tarpit | deliberate multi-second delay before reply | generous timeouts, low concurrency |
| Rate-limit | 451 **4.7.500** "Server busy" (most common on MS), 4.7.650/4.7.651/4.7.28 reputation | back off hard, lower concurrency (MS: <500 simultaneous conns; for probing go far lower) |
| Blocklist | 5xx/reset **before RCPT**, on connect/HELO (about YOUR IP) | rotate IP, mark address **unknown**; check IPs vs Spamhaus PBL/DBL |

- **Corrected:** Microsoft's most common deferral is **451 4.7.500**, not 4.7.651/4.7.28. Treat the whole **4.7.5xx–4.7.6xx** family as rate-limit/reputation transient. Timing alone won't cleanly separate greylist from rate-limit on M365 (4.7.500 can itself be reputation-driven) — **use the code family** as the discriminator.

### 3.4 Caching (CONFIRMED — mandatory for reputation)
Persist per domain: `provider`, `is_catch_all` (tri-state yes/no/unknown), `last_probed_at`, `honest_reject_confirmed`, tarpit/rate-limit flags, learned `pattern` + `separator`. Probe each **domain's** catch-all/provider fingerprint **once per batch**, not per address. TTL ~7–30 days `[judgment call]`; **shorten** when live results start disagreeing with the cached verdict (catch-all status changes when admins toggle DBEB or routing rules).

---

## 4. Provider → reliability strategy map

### 4.1 Classify provider from MX suffix (case-insensitive, all MX names)
| Provider | MX suffix |
|---|---|
| Microsoft 365 | `*.mail.protection.outlook.com`, `*.olc.protection.outlook.com` |
| Google Workspace | `aspmx.l.google.com`, `alt[1-4].aspmx.l.google.com`, `aspmx[2-3].googlemail.com` |
| Consumer Gmail | `*.gmail-smtp-in.l.google.com` |
| Proofpoint | `*.pphosted.com`, `*.gslb.pphosted.com`, `*.ppe-hosted.com` (Essentials) |
| Mimecast | `*.mimecast.com`, `.co.za`, `-offshore.com` |
| Cisco IronPort | `*.iphmx.com` |
| Barracuda | `*.barracudanetworks.com`, `*.ess.barracudanetworks.com`, `cudamail.com` |
| Zoho | `mx.zoho.com/.eu/.in` |
| Amazon SES inbound | `inbound-smtp.<region>.amazonaws.com` |
| Yahoo/AOL | `*.yahoodns.net` |

**Precedence & tie-break rules (CONFIRMED against KB):**
1. If ANY MX matches a **security-gateway** suffix (Proofpoint/Mimecast/IronPort/Barracuda), classify as that gateway regardless of backend. (opengov.com lists both pphosted + aspmx → behaves as Proofpoint.)
2. Else match M365 / Google / Zoho / SES / Yahoo.
3. For mixed backend + SES-inbound (navi.com, rapido.bike), the **primary/lowest-preference backend wins** over a secondary SES-inbound MX (audit labeled these google_workspace).
4. Unmatched → "other/unknown" → default to **probe-with-catch-all-guard at low confidence**; for vanity MX, resolve A + WHOIS the IP before defaulting.

*(Barracuda, Proofpoint Essentials `ppe-hosted`, Fastmail `messagingengine.com` appear nowhere in the audit — those suffixes are training knowledge, not audit-validated.)*

### 4.2 Strategy table
| Class | Providers | Strategy |
|---|---|---|
| **PROBE** (trust RCPT verdicts) | google_workspace, proofpoint, mimecast, cisco_ironport | catch-all guard → RCPT: 250=valid, 550 5.1.1/5.1.10=invalid, 4xx=retry, 5.4.1/5.7.x=non-signal |
| **NO_PROBE / pattern-only** | microsoft365 | **skip live RCPT** (default); score on pattern KB, flag `unverifiable-M365`. *Carve-out:* honor a clean post-send DSN **5.1.1/5.1.10** as invalid, and feed post-send **5.4.1 from a DBEB tenant** to the KB as *probable-invalid* |
| **PROBE_WITH_CATCHALL_GUARD** | zoho, barracuda, "other/unknown" | verdict valid only if not catch-all and recipient-verification is on |
| **NO_PROBE / accept-all** | yahoo_aol (`yahoodns.net`), amazon_ses inbound | 250 is meaningless (anti-harvest / post-acceptance rule eval); pattern-only. Consumer Gmail returns honest 5.1.1 but heavily rate-limits a single probing IP |

### 4.3 The Microsoft 365 truth (important — earlier drafts had the mechanism backwards)
- `550 5.4.1 "Access denied AS(201806281)"` is **Directory-Based Edge Blocking (DBEB)** — the recipient object was **not found** in the tenant's Entra ID/O365 directory. **DBEB is ON by default** for authoritative accepted domains; accept-all arises from internal-relay config or a third-party gateway in front.
- So on a DBEB tenant, **5.4.1 is a strong probable-invalid signal** (Microsoft's equivalent of user-unknown), NOT a policy block on valid mailboxes. Valid mailboxes hit 5.4.1 only via directory-sync lag / misconfiguration — implausible whole-domain edge cases (capco 24/51, entainindia 20/39 are wrong-guess clusters).
- **BUT pre-send live RCPT probing of M365 is still operationally unreliable:** EOP tarpits/throttles probe IPs, and at probe time you cannot distinguish DBEB-invalid from an accept-all tenant from a reputation block. Therefore:
  - **Default (pre-send):** skip RCPT, score M365 on pattern KB, label `unverifiable-M365`. Never mark 5.4.1-at-probe as a hard invalid downrank.
  - **If you do probe a specific tenant:** run the random-local-part classifier — random gets 5.4.1 ⇒ DBEB on ⇒ a real-address 5.4.1 is meaningful (probable-invalid) and a real-address 250 may still be accept-all; random accepted (250) ⇒ accept-all tenant ⇒ RCPT meaningless.
  - **Post-send (DSN feedback):** a 5.4.1 DSN from a DBEB tenant **is** reliable — bank the local part as known-bad. Do **not** discard it (that would throw away ~118 true negatives).
- Auth/reputation rejections use **5.7.x** (5.7.1/5.7.515/5.7.606), never 5.4.1 — don't conflate.

### 4.4 Self-correcting map
Log observed `5.1.1 vs 5.4.1 vs 250-then-bounce` rates per provider (as the audit did) and auto-flip a provider to NO_PROBE if its policy-reject rate crosses a threshold.

---

## 5. Confidence scoring model

Combine four inputs into a **0–100 score** + a **status label**. All numeric constants are **tunable defaults**, not audit-derived — calibrate against held-out bounce outcomes.

```
final = w_src·source_evidence + w_dom·domain_pattern_match
        + w_pat·global_pattern_prior + w_smtp·smtp_signal
```
Ordering of influence: **source_evidence (address seen in a real public source) > domain_pattern_match (KB) > global_pattern_prior > smtp_signal**, and `smtp_signal` is **gated by provider** (§4).

**Status labels (adopt Hunter's split — status separate from score):**
`DELIVERABLE / UNDELIVERABLE / RISKY(catch-all|role|disposable) / UNKNOWN` — plus `is_catch_all`, `is_role`, `is_disposable`, `webmail` flags kept as **separate fields** from the raw SMTP code so you can reclassify without re-probing.

**Resolution rules:**
| Situation | Status | Score |
|---|---|---|
| syntax/MX fail | UNDELIVERABLE | 0 |
| honest provider, real-address 550 5.1.1/5.1.10 | UNDELIVERABLE | ~2 |
| honest provider, 250, **not** catch-all | DELIVERABLE | 90–98 |
| catch-all domain | RISKY (pattern-based only) | = pattern score, **hard-capped ~55–60**, never "valid" |
| M365 / accept-all / greylist-uncleared / rate-limited / blocklisted | UNKNOWN | = pattern score, cap ~50 |
| role/disposable | RISKY overlay | regardless of SMTP |

**Pattern score base:** guess matches domain's learned dominant shape → 75–85; matches only the global 76% first.last prior → ~55–65; unusual shape → lower; local part in `known_bad_locals` → force UNDELIVERABLE.
**Why cap catch-all/M365:** the audit's #1 bounce cause is `address_not_found` and catch-all/M365 removes the exact signal that catches wrong guesses — a good pattern match still carries real bounce risk.
**Boost:** raise a catch-all/pattern-only address a few points only if **multiple independent public sources** corroborate the exact local part.

**Feedback loop:** every DELIVERABLE result and every KB pattern hint upserts `domain_kb.json` (`pattern`, `separator`, `provider`, `verified_examples+1`); every true not-found appends to `known_bad_locals`. The KB self-improves and shrinks paid spend over time.

---

## 6. Pluggable finder/verifier API providers + interface contract

### 6.1 Recommended set (CONFIRMED, prices re-quote at purchase — 2026 figures drift)
- **Anymail Finder** — primary paid finder. `POST /v5.1/find-email/person` (Authorization: bare API key), accepts `linkedin_url` alone or name+domain/company; live SMTP + catch-all resolution; **charges only for a verified find** (risky/not_found/blacklisted free; 30-day free repeats); `verify-email` at 0.2 credit; no rate limits, 180 s timeout; 97%+ delivery guarantee. From ~$29/mo; 100 trial credits (expire 14 days, card required).
- **Hunter.io** — secondary finder + **pattern evidence** source. `GET /v2/email-finder` (score 0–100 + `sources[]`), `GET /v2/email-verifier` (valid|invalid|accept_all|webmail|disposable|unknown, HTTP 202 on >20 s). Domain-Search returns the domain's `pattern` → write straight into `domain_kb.json`. Rate limits **15 rps + 500 rpm** finder / **10 rps + 300 rpm** verifier (enforce BOTH windows). `test-api-key` for CI. **Free = 50 unified credits/mo** (corrected). Credit only charged when found.
- **MillionVerifier** — cheap bulk verifier. `GET api/v3/` (result: ok|invalid|catch_all|unknown|unverified|disposable), **160 rps**, credits never expire, `API_KEY_FOR_TEST`. ~**$0.0018–0.0037/email** (50k for $89). **No server-side dedupe → repeat verifications are charged every time → caching before this call is mandatory.**

### 6.2 Runner-ups / avoid
- **Findymail** — drop-in AMF alternative (verified-only, Bearer auth, `linkedin_url` via `/api/search/business-profile`). **Corrected:** entry tier is **$99/mo = 5k finder + 5k verifier**; `/api/verify` returns **`{email, verified: bool, provider}`** (boolean, not an enum). Clay-#1 / ~75% find-rate are unverified vendor claims.
- **Prospeo** — `POST /enrich-person` only (X-KEY); legacy `/email-finder` & `/social-url-enrichment` removed; uses BOUNCEBAN for catch-all/M365 resolution.
- **ZeroBounce** — premium verifier, richest taxonomy (**24** sub-statuses), ~$0.0195/email, 100 free credits/mo recurring; use only if you want forensic sub-status. **Reoon** — budget (quick|power modes). **NeverBounce** — credits expire 12 mo (worst value).
- **Do NOT build against:** Clearbit (sunset, HubSpot-only), Apollo (seat-tied credits, up to 9/record, DB-stale), RocketReach (export+lookup capped, API gated to ~$2,099/yr Ultimate), Snov.io (OAuth + async two-step, charges on valid OR unknown), Dropcontact (API in €79/mo Starter but only 500 credits, batch-async).

### 6.3 Interface contract
```
interface EmailFinder:
  name() -> str
  find(FindRequest) -> FindResult
  estimated_cost_credits(req) -> float
  healthy() -> bool

FindRequest { first_name?, last_name?, full_name?, domain?, company_name?, linkedin_url?, timeout_ms? }
   # validate: linkedin_url alone OR name+company; normalize casing/diacritics before dispatch
FindResult { email|null, status: FOUND_VERIFIED|FOUND_UNVERIFIED|FOUND_CATCH_ALL|NOT_FOUND,
             confidence:0-100, pattern_hint?, provider, credits_charged, latency_ms, raw }

interface EmailVerifier:
  verify(email, {timeout_ms, deep}) -> VerifyResult

VerifyResult { email, status: DELIVERABLE|UNDELIVERABLE|RISKY|UNKNOWN,
               reason: mailbox_not_found|catch_all|policy_blocked|disabled|mailbox_full|
                       greylisted|disposable|role_based|syntax|dns_failure|provider_timeout|other,
               is_catch_all, is_disposable, is_role, webmail, score|null,
               mx_provider: microsoft365|google_workspace|proofpoint|mimecast|cisco_ironport|zoho|other|null,
               provider, credits_charged, raw }
```
**Status-mapping table** (freeze against live sandbox calls):
| Provider | → DELIVERABLE | → UNDELIVERABLE | → RISKY | → UNKNOWN |
|---|---|---|---|---|
| MillionVerifier | ok | invalid (mailbox_not_found) | catch_all, disposable | unknown, unverified |
| ZeroBounce | valid | invalid (map sub_status) | catch-all, spamtrap/abuse/do_not_mail | unknown |
| Anymailfinder | valid | invalid | risky | — |
| Hunter | valid | invalid | accept_all (catch_all) | unknown; **webmail→UNKNOWN + set webmail flag** (may be deliverable) |
| Findymail verify | verified=true | — | — | verified=false (no reason granularity) |

**Typed errors:** `ErrAuth` (fail-fast, disable provider), `ErrQuotaExhausted` (skip to next billing tick), `ErrRateLimited{retry_after}` (honor per-provider limits above), `ErrTimeout` (retry once), `ErrProviderDown` (circuit-break 5 min), `ErrBadInput` (non-retryable).
**Orchestration (config, not code):** `local_pattern_guess → local_smtp_probe (only if provider ∈ {google_workspace,proofpoint,mimecast,cisco_ironport}) → verifier_fallback (MillionVerifier) → finder_fallback (Anymailfinder → Hunter)`; short-circuit on DELIVERABLE/FOUND_VERIFIED; **skip local_smtp_probe entirely on M365/catch-all**.
**Caching/idempotency:** key = `sha256(provider + normalized_input)`; TTL **30 d verify / 90 d find** (mirrors AMF 30-day & Prospeo 90-day free-repeat windows so a cache miss never double-charges); cache is **mandatory** before MillionVerifier.
**Config:** `providers: [{name, api_key_env, enabled, priority, max_credits_per_day}]` — providers are optional; the system must run fully in **zero-provider local mode**.
**Routing payoff:** gating paid calls to only M365 + catch-all + unknown-pattern domains cuts paid volume ~**68–70%** (honest-probeable providers = 153/219 domains, 2,044/3,006 addrs). **No commercial verifier resolves M365 5.4.1** — expect RISKY/UNKNOWN there, budget ~2–5% residual bounce on M365/catch-all.

---

## 7. Deliverability & sending guidance

### 7.1 Bounce-rate stakes (CONFIRMED)
Audit ran at **18.6%** (floor — 32 ambiguous rows excluded push worst-case ~19.7%) vs the **<2% acceptable / >5% spam-foldering** industry heuristics (these are practitioner thresholds, **not** provider-published; Gmail Postmaster shows spam rate, IP/domain reputation, auth — **not** bounce rate). Bounce reputation is tracked per sending domain/IP, so every wrong guess degrades inbox placement for later correct sends.

### 7.2 What verification actually prevents (CORRECTED — earlier "→3% at zero cost" was wrong)
- RCPT verification on honest providers prevents ~**348/560 (62%)** (`address_not_found`); MX/DNS health checks + per-domain circuit-breaker prevent ~**47 (8%)** (dns_failure 17 + connection_failure 10 + routing_loop 20; cdk.com alone = 14 routing loops).
- **Residual after both = ~165 bounces → ~5.5–6.5%, NOT ~3%.** Reaching ≤2% additionally requires **withholding/down-weighting the 118 M365 policy-blocked addresses** (with DBEB feedback, most were preventable wrong guesses → total preventable ~83%).
- **Not "zero cost":** requires **port-25-capable infrastructure (VPS)** or a **paid HTTPS verifier API**, and probe IPs risk greylisting from Proofpoint/IronPort. Degrade to `verification_unavailable` when unavailable.

### 7.3 Sending limits (CONFIRMED)
- **Free Gmail:** 500 **recipients** / rolling-24h (To+CC+BCC+replies count; slots free 24 h after each send). **Workspace:** 2,000 messages/24h (3,000 unique / 10,000 total recipients). The audit's 3,006 recipients guaranteed the "sender limit reached" block.
- Google's behavioral system also blocks **below** the cap on burst sending, volume spikes, or **"a large number of un-delivering messages"** — i.e. the 560 bounces and the sending-block were **causally linked**. Repeated episodes escalate to suspension.

### 7.4 Google/Yahoo/Microsoft bulk-sender rules (CONFIRMED)
- **All senders to Gmail:** SPF **or** DKIM; valid forward + reverse DNS (PTR); TLS; spam-complaint rate <**0.3%** (target <0.1%).
- **Bulk (5,000+/day per primary domain):** **both** SPF and DKIM; DMARC ≥`p=none`; From-domain **alignment** with SPF or DKIM; RFC 8058 **one-click unsubscribe** (honored within 2 days). Yahoo mirrors. **Microsoft Outlook.com** added the same for 5,000+/day senders effective **May 5, 2025** (rejects with 5.7.515).
- Even below 5,000/day, aligned SPF+DKIM+DMARC is the de-facto floor because M365 tenants hard-reject unauthenticated cold mail.

### 7.5 Auth spec + dedicated domain (CONFIRMED)
- **SPF:** one TXT at root, ≤10 DNS lookups (`v=spf1 include:_spf.google.com ~all`).
- **DKIM:** 2048-bit (1024 min), published at `selector._domainkey`, enabled in Workspace admin.
- **DMARC:** `_dmarc` TXT, ramp `p=none` (+`rua`) → quarantine → reject once reports show alignment. Relaxed alignment permits subdomains.
- Use a **dedicated cousin domain** (e.g. `tryfrnd.app` vs `frnd.app`) so bounce disasters burn a sacrificial reputation, and enroll it in **Google Postmaster Tools**.

### 7.6 Warmup & cadence `[practitioner heuristics — safety margins, not provider-documented]`
- Warmup 2–4 weeks: 10–15/day → **≤20%/day increase** → steady state **30–50 cold sends/inbox/day** (75–100 ceiling). Scale **horizontally** (more inboxes/domains), not vertically. Volume spikes are the #1 flagging trigger.
- Cadence: randomized 1–3 min gaps, business-hours in recipient TZ; **cap per recipient-domain/day ~20–30** (gateways like Proofpoint/Mimecast/IronPort rate-pattern inbound sources — never blast 209 addresses at amadeus.com at once); 2–3 follow-ups max, 3–4 day spacing, stop on any reply/opt-out.

### 7.7 Runtime circuit breakers & DSN feedback (CONFIRMED)
- Pause campaign when rolling bounce >3% (hard stop 5%); pause a recipient domain after 2 consecutive not-founds or any routing-loop (would have stopped harman.com's 39 not-founds after ~3–4).
- Parse every DSN by enhanced code: **5.1.1/5.1.10 → permanent suppression + KB `known_bad_locals`**; **5.4.1 on a DBEB M365 tenant → probable-invalid, bank as known-bad** (do NOT discard); **5.7.x → sender-side reputation/auth fix, not address-invalid**; **4.x.x → soft, retry ≤2× over 48 h**.
- Ship one-click unsubscribe (RFC 8058) and track spam-complaint rate against 0.3% hard / 0.1% target even below 5k/day.

---

## 8. Legal / ethical constraints the tool must enforce

*(Not legal advice; federal-level summary; EU member-state ePrivacy varies.)*

### 8.1 The clean-room design line (CONFIRMED — the core architectural decision)
- **SHOULD:** accept user-typed/pasted **name + company** (or CSV); resolve domain via public DNS/website; rank by audit priors + KB; verify via provider-aware RCPT (skip M365); return confidence-scored results; **per-user data silo**; retention cap (~90 days) + one-click delete; **per-record provenance log** ("derived from user-entered name + public MX"); a **global cross-user suppression list** any recipient can join via a public opt-out page, checked before returning results.
- **SHOULD NOT:** automate any LinkedIn access (no headless browsers, no `li_at`/session-cookie import, no DOM-scraping extension — all LinkedIn UA **§8.2** breaches); **pool/resell contacts across users** (data-broker trigger); bulk-verify whole domains (dictionary-attack pattern); label M365/catch-all as "verified"; or auto-send (keep finding and sending as separate deliberate acts).

### 8.2 Case law (CONFIRMED)
- **hiQ v. LinkedIn:** hiQ **LOST**. CFAA "gates-up" for public pages (likely no federal crime), **but** Nov 2022 summary judgment held hiQ **breached the User Agreement** (scraping + fake accounts); Dec 2022 consent judgment: **$500k, permanent injunction, delete all scraped data + derived algorithms.** "hiQ made scraping legal" is **false**.
- **Meta v. Bright Data (N.D. Cal., Jan 2024):** protects only **logged-off** scraping of public pages by a non-user; the moment scraping runs through an account (all LinkedIn email finders), the UA binds and hiQ applies. Narrow district-court ruling, not appellate.
- **LinkedIn v. ProAPIs (Oct 2025):** ~1M fake accounts scraping data sold up to $15k/mo; settled in principle 2026 (no merits ruling). A tool **vendor** that automates LinkedIn violates the UA even if only its users hold accounts; users risk permanent bans.

### 8.3 GDPR — a derived work email IS personal data (CONFIRMED)
- **Kaspr (CNIL, Dec 2024, €240,000)** is the controlling precedent: fined for scraping restricted-visibility profiles (no valid legitimate interest), endless 5-year rolling retention, no Art. 14 notice, vague access-request answers. A derivation-only tool that stores nothing (or only the user's own list) with per-record provenance avoids nearly all of this.
- B2B cold email can rest on **legitimate interest** (Recital 47) **only** with a documented **LIA** (ship the ICO three-part-test template pre-filled for referral outreach). **Art. 14 notice within 1 month / at first communication** (first email must disclose who/why/source/how-to-object). **Art. 21 opt-out is absolute.** **ePrivacy overrides:** Germany (UWG §7), Austria, Italy require **opt-in even for B2B** — support per-country sending policies.

### 8.4 Other regimes
- **CAN-SPAM (US):** opt-out regime, no consent needed, **no B2B exception**; truthful headers/subject, ad identification, valid physical address, opt-out honored ≤10 business days. Up to **$53,088/email** (Jan 2025 adjustment). **Correction:** §7704(b) harvesting/dictionary-attack "aggravated violations" attach only **in conjunction with sending** violating mail — RCPT probing with no mail sent is **not itself** a CAN-SPAM violation; the real risk is abuse-desk/blocklist + future-sending aggravation. Keep rate limits regardless.
- **CASL (Canada):** opt-in by default; implied-consent "conspicuously published" path needs publication + no anti-solicitation notice + role-relevance, **sender bears proof**. A **permutation-guessed address is never "published"** → no implied-consent basis for Canadian recipients — surface this gap. Penalties CAD $10M/org, $1M/individual.
- **India DPDP:** Rules notified 13 Nov 2025; phased — consent-manager registration **~13 Nov 2026**, substantive consent/notice/rights **~13 May 2027** (18 months). **Corrected dates** (was "14th"). **No legitimate-interest ground** — consent or narrow §7 uses only; cold marketing not covered. `[UNCERTAIN]` MeitY's Jan 2026 consultation proposed compressing to 12 months — treat 2027 as a floor. §3(c) personal/domestic exclusion may cover an individual job-seeker but not a commercial tool vendor. Up to ₹250 crore.
- **CCPA/CPRA:** B2B exemption expired Jan 1, 2023 — CA work emails/titles are protected PI. Applies over thresholds: **~$26.6M revenue** (inflation-adjusted from $25M, corrected) OR ≥100k consumers/households OR ≥50% revenue from selling PI. **Delete Act (SB 362):** a service selling PI of consumers it has no relationship with must register as a **data broker** + honor DROP → **never pool/resell contacts.**

### 8.5 SMTP-probe ethics
Probe only where informative (skip M365), keep volumes human-scale, cap probes per domain/day (~10–20), jitter timing, dedicated probe IP separate from sending IP, **never "verify by sending" a test email** (that IS the unsolicited mail regulators target). At scale, verification traffic looks like directory-harvest to abuse desks (Spamhaus lists it).

---

## 9. Recommended Python library stack

| Purpose | Library | License | Notes |
|---|---|---|---|
| MX/A/AAAA lookup | **dnspython** | ISC | mature |
| Syntax + normalization | **email-validator** (JoshData) | Unlicense (CC0 pre-2024) | **disable `check_deliverability`** — run your own MX/SMTP |
| Async SMTP probing | **aiosmtplib** | MIT | hundreds of concurrent RCPTs, per-conn timeouts; bounded semaphore |
| Permutation ranker | **hand-written (~50 lines)** | — | drive ranking from audit priors + KB; no dependency |
| Disposable/role list | **vendored static JSON** (disposable-email-domains) | CC0/MIT | not a live API |

- **Study, do NOT vendor:** **Reacher / check-if-email-exists** — **AGPL-3.0** (dual-licensed commercial); network-use copyleft. Copy its design: module split `syntax/mx/smtp/misc`; `is_reachable ∈ {safe, risky, invalid, unknown}`; `VerifyMethod ∈ {Smtp, Api, Headless}` (Gmail→api, Yahoo→headless, Hotmail B2C→headless, else smtp); SMTP timeout is **configurable** (drop the earlier "45 s no-retry" specific — unconfirmed) and transient→`unknown`; SOCKS5 proxy support (connecting IP matters). A cleaner-licensed JS reference: **deep-email-validator** (MIT).
- **Avoid:** `validate_email` (syrusakbary) — LGPL, unmaintained. **Correction:** it is *not* syntax-only; it also does MX/SMTP checks. Skip it for maintenance/license reasons, not capability. Naive combinatorial permutators (Satys, jacobgoh101, MailPermute) are generation-only references, low maintenance.

---

## 10. Pitfalls to avoid

1. **Trusting an SMTP code without knowing the provider.** A 550 5.4.1 from M365 is not a generic policy block (it's DBEB directory-not-found), and a 250 from a catch-all or Yahoo/SES proves nothing. Classify provider from MX **first**.
2. **Marking timeouts/4xx/connection failures as "invalid."** They are **UNKNOWN**. Port-25 blocks, greylisting, tarpitting, and PBL-listed probe IPs all produce non-answers, not negatives.
3. **Probing from a laptop / default cloud VM / residential IP.** :25 is blocked (AWS/Azure/GCP default) and PBL-listed IPs corrupt results. Use a dedicated VPS with clean static IP + PTR + SPF, or a paid API.
4. **Retrying a 552.** It's permanent (mailbox-full/size), not transient. Only 421/450/451/452 retry.
5. **Spraying the full permutation set via SMTP.** Generate all, probe in ranked order, short-circuit on first accept, reuse one session per domain, throttle per MX. Bulk probing = dictionary-attack pattern + reputation burn.
6. **Not detecting catch-all before trusting a 250.** Always random-local-part probe first; downgrade to pattern-only. Remember it can't catch after-DATA/deferred rejection.
7. **Over-generalizing patterns.** Don't bump first.l for all Indian/.in domains (purplle is an outlier); don't assume dot everywhere (trimble uses underscore). Let the per-domain KB override, storing literal template + separator.
8. **Discarding M365 5.4.1 DSNs.** Post-send on a DBEB tenant they're probable-invalid — bank them. But don't rely on pre-send M365 RCPT (tarpitted, ambiguous).
9. **Claiming "→3% bounce at zero cost."** Realistic residual after verify+health-checks is ~5.5–6.5%; ≤2% needs M365 suppression; verification needs real port-25 infra or paid API.
10. **Burning your primary domain / unwarmed bursts.** Use a sacrificial cousin domain, warm it, enforce SPF+DKIM+DMARC before any send, respect the Gmail 500-recipient/24h cap, and wire circuit breakers.
11. **Building a scraped people-database or pooling contacts across users.** That's the Kaspr/Delete-Act trigger. Stay derivation-only, per-user siloed, with provenance logging and a global suppression list.
12. **Automating LinkedIn in any form.** UA §8.2 breach + hiQ/ProAPIs injunction exposure + user bans. Input is manual name+company only.
13. **Not caching domain-level verdicts.** Re-probing catch-all/provider status per address wastes reputation and invites blocklisting. Cache per domain, probe fingerprint once per batch.
14. **MillionVerifier without a cache in front.** It charges every repeat verification (no server-side dedupe).