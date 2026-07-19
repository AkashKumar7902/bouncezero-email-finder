# webapp/ — hosted public web app contract (Render + Neon Postgres)

Goal: a **public, multi-user, free-to-host** web app on top of the existing
`emailfinder` pure core. It is **additive** — it must NOT modify anything under
`emailfinder/` or `tests/` (the 380-test local tool stays intact). It reuses the
pure modules for all accuracy logic; only persistence + the web surface are new.

Hard constraints (free hosting = Render free tier):
- **Verification is OFF.** Never call `emailfinder.smtp_probe`. Provider is still
  classified from a live DNS MX lookup (Render allows outbound DNS), which drives
  the M365/catch-all scoring caps. So results are pattern + provider-aware only.
- **Multi-user shared state in Postgres** (free hosting has ephemeral disks; a
  local file would be wiped). The KB + suppression + lookups are GLOBAL (shared
  across all visitors) — there are no per-user silos in the hosted app.
- **Self-contained page**: inline HTML/CSS/JS, no external/CDN URLs.
- Everything JSON-serializable; no secrets in code (DB via `DATABASE_URL` env).

## Reuse these pure modules (do NOT reimplement their logic)
- `emailfinder.normalize`: `is_linkedin_url(s)`, `parse_linkedin_slug(url)`, `slug_to_name(slug)`
- `emailfinder.names`: `parse_name(name)`, `expand_variants(pn, nickname_table)`, `load_nicknames(path)`
- `emailfinder.templates`: `global_priors() -> list[(template, sep, prior)]`
- `emailfinder.ranking`: `rank(variants, kb_entry, priors, threshold=0.60) -> list[Candidate]`, `dominant_share(kb_entry)`
- `emailfinder.provider`: `classify_provider(mx_hosts) -> Provider`, `strategy_for(provider) -> VerifyStrategy`
- `emailfinder.filters`: `load_static_sets(data_dir) -> {"role","disposable","webmail": set}`, `is_role_local`, `is_disposable_domain`, `is_webmail`, `in_known_bad(local, kb_entry)`
- `emailfinder.scoring`: `score_candidate(cand, provider, strategy, is_catch_all, smtp, flags, cfg.score, kb_match) -> ScoredCandidate`, `rank_scored(list)`
- `emailfinder.dns_mx`: `resolve_mx(domain, timeout) -> MXInfo`, `resolve_domain_for_company(company, cfg) -> str|None`
- `emailfinder.config`: `load_config() -> Config` (has `.score`, `.package_data_dir`, `.kb_dominance_threshold`, `.dns_timeout`)
- `emailfinder.models`: `Provider`, `VerifyStrategy`, `Status`, `Candidate`, `ScoredCandidate`, `MXInfo`, `DomainFingerprint`
- `emailfinder.shapes`: `shape(local) -> (shape_label, sep)`
- `emailfinder.compliance` (import the private normalizers for consistent keys): `_norm_email`, `_norm_name`, `_identity_key`, `_norm_identity`

## FROZEN interface: `Store` (in webapp/store.py)

```python
from typing import Protocol
from emailfinder.models import DomainFingerprint

class Store(Protocol):
    # --- knowledge base ---
    def get_kb_entry(self, domain: str) -> dict | None: ...
    def upsert_verified(self, domain: str, template: str, separator: str,
                        provider_value: str, example_local: str) -> None: ...
    def append_known_bad(self, domain: str, local: str, source: str) -> None: ...
    # --- domain fingerprint cache ---
    def get_domain_fp(self, domain: str, ttl_days: int) -> DomainFingerprint | None: ...
    def put_domain_fp(self, fp: DomainFingerprint) -> None: ...
    # --- suppression / opt-out (global) ---
    def is_suppressed(self, email: str | None, name: str | None, domain: str | None) -> bool: ...
    def suppression_emails(self) -> set[str]: ...
    def add_suppression(self, email: str | None, name: str | None,
                        domain: str | None, source: str) -> None: ...
    # --- audit log ---
    def log_lookup(self, record: dict) -> str: ...
    def purge_lookups(self, older_than_days: int) -> int: ...
    def close(self) -> None: ...
```

`get_kb_entry` MUST return a dict shaped exactly how ranking/scoring/filters
expect (or None if the domain is unknown):
```python
{
  "provider": "<Provider .value, e.g. 'microsoft365'>",
  "dominant_shape": "first.last",         # or "" if unknown
  "dominant_separator": ".",              # "" means no separator (NOT "(none)")
  "shape_distribution": {"first.last": 12, "first.l": 1},
  "no_bounce_locals": ["a.b", ...],       # list
  "known_bad_locals": {"wrong.guess", ...},  # set (filters.in_known_bad does membership)
}
```
`upsert_verified`: insert the local as no_bounce, bump shape_distribution via
`shapes.shape`, and set `dominant_shape` to the distribution ARGMAX (a single
example must never override the learned majority) with the matching separator.
`append_known_bad`: mark the local known_bad (dedup), drop it from no_bounce.
Suppression normalization MUST match `is_suppressed` query keys (reuse the
compliance normalizers): email via `_norm_email`, identity via `_identity_key`
(add) / `_norm_identity` (read).

## Implementations
- **webapp/store.py**: the `Store` Protocol + `MemoryStore` (dict-backed, for
  local dev + tests) + any shared helpers (e.g. `_sep_from_shape`, argmax promote).
- **webapp/store_pg.py**: `PgStore(dsn)` — psycopg 3, SAME interface, plus module
  `SCHEMA` (CREATE TABLE IF NOT EXISTS ...) and `init_schema(conn)`. Tables:
  `domains`, `locals`, `domain_cache`, `suppression`, `lookups` (see below).
  Import psycopg lazily inside methods/ctor so importing the module never fails
  when psycopg is absent.

Postgres schema (idempotent):
```
domains(domain PK, provider, mx JSONB, dominant_shape, dominant_separator,
        shape_distribution JSONB, updated_at DOUBLE PRECISION)
locals(domain, local_part, status, source, seen_at, PRIMARY KEY(domain, local_part))
domain_cache(domain PK, provider, mx JSONB, is_catch_all BOOLEAN,
             flags JSONB, last_probed_at DOUBLE PRECISION)
suppression(id BIGSERIAL PK, email, identity, source, added_at)  -- index email, identity
lookups(id BIGSERIAL PK, ts DOUBLE PRECISION, name, domain, local_part,
        linkedin_url, provider, reasons JSONB, ip_hash)          -- index ts
```

## FROZEN interface: `HostedFinder` (in webapp/service.py)

```python
class HostedFinder:
    def __init__(self, store: Store, cfg=None): ...   # cfg defaults to load_config()
    def find(self, name=None, domain=None, *, first=None, last=None,
             company=None, linkedin_url=None) -> dict: ...
    def optout(self, email=None, name=None, domain=None) -> None: ...
    def kb_entry(self, domain: str) -> dict | None: ...
```

`find` mirrors `emailfinder.engine.Engine.find` but SIMPLER (no SMTP, no paid
providers, no per-user silo). Stages, in order:
1. If `linkedin_url` and `normalize.is_linkedin_url`: `slug = parse_linkedin_slug`;
   if no explicit name, `name = slug_to_name(slug)`. NEVER fetch the URL.
2. Suppression gate: `store.is_suppressed(None, name, domain)` -> return
   `{"suppressed": True, ...}`.
3. Resolve domain: given `domain`, else `dns_mx.resolve_domain_for_company(company, cfg)`.
   If none -> return a result with `notes=["no domain: pass a domain or a resolvable company"]`.
4. Suppression gate again with the resolved domain.
5. Fingerprint: `store.get_domain_fp(domain, ttl)` else `dns_mx.resolve_mx` +
   `provider.classify_provider(mx.hosts)` -> `store.put_domain_fp(...)`.
   Do NOT cache a transient `dns_timeout`.
6. `kb_entry = store.get_kb_entry(domain)`. M365 override: if kb says
   microsoft365 and live provider isn't, force `Provider.MICROSOFT365`.
7. `strategy = provider.strategy_for(prov)`.
8. `parse_name` -> `expand_variants` (load nicknames from cfg.package_data_dir) ->
   `ranking.rank(variants, kb_entry, global_priors(), threshold=cfg.kb_dominance_threshold)`.
9. filters: drop role locals; compute disposable/webmail flags; per-candidate
   `known_bad` via `filters.in_known_bad`.
10. Score each candidate with `smtp=None` (verification off), `flags` including
    dns_failure/dns_unavailable/webmail/is_disposable/is_role/known_bad/syntax_ok,
    `kb_match = (cand.source == 'kb')`; `rank_scored`.
11. Filter out any generated address in `store.suppression_emails()`; if that
    empties a non-empty set, return suppressed.
12. `store.log_lookup({...})` (name, domain, best local, linkedin_url, provider,
    reasons, ts, ip_hash placeholder). Return a JSON dict:
    `{suppressed, domain, provider, provider_label, strategy, verification_mode:"none",
      best: {email, local_part, template, separator, score, status, is_catch_all,
             is_role, is_disposable, webmail, reasons, cap_note}, alternates:[...],
      mx, notes}`.
    Provider label + cap_note: reuse the same wording as emailfinder/web.py
    (`_provider_label`, `_cap_note`) — copy those small helpers into service.py.

## FROZEN: FastAPI app (webapp/app.py)
- `app = FastAPI()`. Build ONE `HostedFinder` at startup. Store selection:
  `DATABASE_URL` set -> `PgStore(dsn)` + `init_schema`; else `MemoryStore` with a
  logged WARNING ("ephemeral in-memory store — dev only").
- Routes:
  - `GET /` -> the inline HTML page (self-contained, light+dark, no external URLs,
    single-lookup card with provider badge + confidence bar + status chip + honest
    cap note + 'why this guess' + a public opt-out form).
  - `POST /api/find` (JSON {name, domain?, company?, linkedin_url?}) -> HostedFinder.find dict.
  - `POST /api/optout` (JSON {email?, name?, domain?}) -> 204.
  - `GET /api/kb/{domain}` -> {found, ...entry} (sets -> sorted lists for JSON).
  - `GET /healthz` -> {"ok": True}.
- **Per-IP rate limiting** middleware: sliding window, default 30 req/min and
  300 req/day per IP (in-memory; note it resets on restart/scale). Return HTTP 429
  with a JSON body when exceeded. Read client IP from `X-Forwarded-For` (Render
  sets it) falling back to the socket peer. Health check is exempt.
- Never leak a stack trace; wrap handler errors -> 400/500 JSON.

## Deploy (webapp/ + repo root)
- `render.yaml` (Render Blueprint): one web service, env python, build
  `pip install -e ".[web]"`, start
  `uvicorn webapp.app:app --host 0.0.0.0 --port $PORT`, `DATABASE_URL` as a
  (manually-set, from Neon) env var, `PYTHON_VERSION` pinned.
- `.env.example` with `DATABASE_URL=postgresql://...` and rate-limit knobs.
- `pyproject.toml`: add a `web` optional-dependency group
  (`fastapi`, `uvicorn[standard]`, `psycopg[binary]`, `python-multipart`).
- README: a "Deploy free (Render + Neon)" section — create Neon DB, copy the
  connection string, create a Render web service from the repo, set `DATABASE_URL`,
  deploy; note verification is off on free hosting and the cold-start behavior.

## Tests (tests/test_webapp.py) — must not require Postgres
- Drive `HostedFinder(MemoryStore())` + FastAPI `TestClient`:
  - a find on a KB-seeded domain uses the learned pattern; M365 domain is capped
    and never DELIVERABLE; a dns_failure domain -> UNDELIVERABLE;
  - opt-out then find returns suppressed / filters the address;
  - rate limiter returns 429 after the cap;
  - `/healthz` ok; served page has NO external http(s) URLs.
- Mock `dns_mx.resolve_mx` (monkeypatch) so tests are offline.
- A PgStore smoke test may be `@pytest.mark.integration` + skipped unless
  `DATABASE_URL` is set.
