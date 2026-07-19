# BounceZero — LinkedIn email finder + verifier

An **offline-first, provider-aware** tool that turns a person's name + company
into their most-likely work email, scores how much to trust it, and — its
headline feature — **re-scores your existing bounced list** to tell you exactly
which addresses were wrong and what to send instead.

Its design was **informed by** analysing a real 3,006-address cold-outreach
bounce audit — but **no audit data is bundled**. The package ships only generic
knowledge (pattern priors, MX→provider map, nickname/role/disposable/webmail
lists) and an **empty knowledge base**. The per-domain KB fills in **at runtime,
from your own data** — run `rescore` on your bounce export, or let `find` /
`confirm` learn as you go. Everything you learn lives in your private
`~/.emailfinder` silo, never in this repo.

> Works with **zero API keys**. Live SMTP verification and paid finder/verifier
> APIs are optional and off by default.

---

## Why it exists (the analysis behind the design)

The reference audit — 654 cold emails to 3,006 people across 210 companies —
bounced **18.6%** of the time. A handful of findings shaped the whole design
(these are baked in as *rules and priors*, not as anyone's data):

| Finding | Consequence in the tool |
|---|---|
| **77%** of corporate emails are `first.last@domain` | the default global pattern priors |
| `address_not_found` is the #1 bounce cause (wrong guess) | `known_bad_locals` (learned from *your* bounces) are force-marked UNDELIVERABLE; the re-scorer banks them |
| **Microsoft 365** returns "Access denied" (550 5.4.1) for *valid* mailboxes — 118 times vs only 3 real not-founds | M365 is **never** RCPT-verifiable → capped at UNKNOWN, never "deliverable" |
| Google / Proofpoint / Mimecast / IronPort return **honest** 550 5.1.1 | those are the only providers the SMTP prober trusts |
| Outbound **port 25 is blocked** on laptops & most cloud VMs | SMTP verify is optional, detects the block fast, and degrades to `verification_unavailable` — never a false "invalid" |

> The full anonymised analysis (aggregate stats + methodology, no individual
> addresses) lives in `audit_analysis/` and `research/` for reference.

## What it does

- **Finds** the most-likely email from a name + company/domain (or a pasted
  LinkedIn URL, whose name slug is parsed **locally** — never scraped).
- **Ranks** candidates: a domain's learned pattern wins (≥60% share); otherwise
  ordered global priors (`first.last`, `flast`, `first`, …). Handles nicknames
  (Bob↔Robert), compound/hyphenated surnames, and **South-Indian first + initial**
  names (`Ashwath S` → `ashwath.s`). Never fabricates a surname or appends digits.
- **Scores** 0–100 with a status: `DELIVERABLE / UNDELIVERABLE / RISKY / UNKNOWN`,
  with honest hard caps on Microsoft 365 and catch-all domains.
- **Re-scores bounces**: ingest a bounced/audit CSV or a DSN mailbox, bucket each
  row by RFC 3463 code, bank true not-founds, emit a corrected candidate per row,
  and improve the KB so accuracy compounds.
- Runs as a **CLI**, a **batch CSV** enricher, and a minimal **local web UI**.

## Install

```bash
cd linkedin-email-finder
python3 -m venv .venv
./.venv/bin/pip install -e .            # core (dnspython only)
# optional extras:
./.venv/bin/pip install -e ".[smtp,providers,transliterate]"
```

## CLI

```bash
emailfinder find "Jane Cooper" --domain acme.com           # name + domain
emailfinder find "Jane Cooper" --company "Acme Corp"        # company -> domain (DNS-resolved)
emailfinder find "Jane Cooper" --domain acme.com --json     # machine-readable

emailfinder batch contacts.csv -o enriched.csv             # mail-merge-ready output
emailfinder rescore bounced.csv -o fixes.csv --apply-kb    # the headline feature (also seeds your KB)
emailfinder rescore --mbox ~/Mail/bounces.mbox -o fixes.csv

emailfinder kb acme.com                                     # inspect a domain's learned pattern
emailfinder optout someone@company.com                     # global suppression
emailfinder purge --days 90                                 # retention cleanup
emailfinder web                                             # http://127.0.0.1:8765
```

Flags: `--verify` (opt-in SMTP), `--providers` (opt-in paid APIs), `--json`,
`--user <id>`, `--data-dir <path>`. SMTP and providers are **off** unless flagged.

### Seed the knowledge base from *your own* data

The tool ships with an empty KB and no contacts. It gets smart from your data:

```bash
# turn your own bounce/outreach export into a fix-list AND learn per-domain
# patterns + known-bad addresses (written to your private ~/.emailfinder silo):
emailfinder rescore my_bounces.csv -o fixes.csv --apply-kb
```

After that, `find` uses the domains it just learned; `confirm` (or the web
"Deliverable / Bounced" buttons) keep teaching it. Nothing you learn is written
back into this repo.

### Reading a result

```
$ emailfinder find "Jane Cooper" --domain acme.com          # acme.com on Microsoft 365
jane.cooper@acme.com
  [UNKNOWN]  confidence 50/100  provider: Microsoft 365
  ! capped: Microsoft 365 not RCPT-verifiable (unverifiable)
  why this guess:
    - global prior for 'first.last' -> base 62 (prior 0.60)
    - Microsoft 365 not RCPT-verifiable -> UNKNOWN, capped at 50
```

(Once you've `rescore`d data for a domain, the reason line reads "KB dominant
pattern …" and the score rises accordingly.)

## Optional: live verification & paid providers

- **SMTP verify** (`--verify`) probes RCPT **only** on honest providers
  (Google/Proofpoint/Mimecast/IronPort), does a catch-all guard first, never
  sends `DATA`, and needs a host with outbound port 25 (a VPS, not a laptop).
- **Paid providers** (`--providers`) plug in behind a common interface
  (Anymail Finder reference adapter + Hunter/MillionVerifier stubs), are
  **cache-guarded** to avoid double-charging, and are routed only where guessing
  can't help (M365 / catch-all / unknown-pattern domains). Configure via a JSON
  config with per-provider `api_key_env`.

## Compliance (clean-room by construction)

Derivation-only, per-user data silo, per-record provenance log, a global
opt-out/suppression list checked **before** any address is returned, and ~90-day
retention. It **never** automates LinkedIn (no scraping/headless/cookies) — a
LinkedIn URL is only ever slug-parsed locally.

## Architecture

A pure core (no I/O) + an I/O layer + thin surfaces, wired by one fixed pipeline
in `engine.py`. See `architecture/MODULE_CONTRACTS.md` for the full module map
and `research/RESEARCH_DOSSIER.md` for the verified domain research.

```
normalize → names → templates/candidates → ranking      (pure: generation)
provider  → filters → scoring                            (pure: classify + score)
dns_mx · smtp_probe · cache · kb_store · compliance      (I/O)
providers/*                                              (optional paid adapters)
engine → cli · web · batch · rescore · dsn               (orchestrator + surfaces)
```

## Testing

```bash
./.venv/bin/python -m pytest            # 380 unit/contract tests
./.venv/bin/python scripts/acceptance.py  # end-to-end safety-invariant checks
```

Tests use small **synthetic fixtures** (`tests/fixtures/`, fake domains + names)
— no real audit data is required or bundled. The `shapes.py` local-part
classifier keeps the exact taxonomy the KB uses, so learned/re-scored rows stay
schema-consistent.

## Deploy free (Render + Neon)

An **additive** public web app lives in `webapp/` (it does not touch the
`emailfinder/` package or the 380-test suite). It reuses the pure core and serves
a self-contained single-lookup page plus a small JSON API.

Hosted-mode caveats:
- **Verification is OFF** — the hosted app never opens SMTP probes; results are
  pattern + provider-aware guesses.
- **DNS still works** — MX lookup classifies the provider (M365 / catch-all) and
  drives the honest confidence caps.
- **Shared global state** in a free **Neon Postgres** (Render's free disk is
  ephemeral); the KB, suppression list, and lookup log are shared across visitors.
- **Cold starts** — Render's free service spins down after ~15 min idle and takes
  ~30–60s to wake; the first request after a quiet period is slow, then fast.

Quick path: create a free Neon Postgres and copy its **pooled** connection string,
push this repo to GitHub, create a Render web service from it (Blueprint via
`render.yaml`, or manual with build `pip install -e ".[web]"` and start
`uvicorn webapp.app:app --host 0.0.0.0 --port $PORT`), set `DATABASE_URL` to the
Neon string, and deploy. Full step-by-step: **[`webapp/DEPLOY.md`](webapp/DEPLOY.md)**.
Free-tier limits change — double-check current Render/Neon limits before relying
on them.

## How it was built

Research → adversarial verification → ideation → architecture → implementation →
adversarial verification, each stage run as a fan-out of specialized agents.
Artifacts live in `research/`, `architecture/`, and `audit_analysis/`.

---

*Not legal advice. Cold outreach is regulated (GDPR/CAN-SPAM/CASL/India DPDP);
see the legal section of the research dossier and use responsibly.*
