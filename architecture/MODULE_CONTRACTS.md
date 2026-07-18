# BounceZero / emailfinder — module contracts

CLI: Command: `emailfinder` (console_scripts -> cli.main) or `python -m emailfinder`. SMTP and paid providers are OFF unless explicitly flagged. Global flags: --json (machine output), --user <id>, --config <path>, --data-dir <path>.

Subcommands:
- `find "<Full Name>" [--domain acme.com | --company Acme | --linkedin <url>] [--first F --last L] [--verify] [--providers] [--json]` -> runs Engine.find; prints the chosen ScoredCandidate (email, status chip, 0-100 confidence, provider badge) + alternates + the 'why this guess' reasons[] trail. --linkedin only slug-parses the URL locally. Honest M365/catch-all caps shown explicitly; suppressed identities print a suppressed notice with no address.
- `batch <in.csv> -o <out.csv> [--map name=Name,domain=Domain,...] [--verify] [--providers]` -> batch.run_batch with per-domain fingerprint-once; writes the mail-merge-ready enriched CSV; prints a progress line + summary counts.
- `rescore <bounced.csv | --mbox <path>> [-o fixes.csv] [--apply-kb]` -> the headline re-scorer; buckets by RFC 3463 enhanced code, writes the per-address fix list (with corrected candidates), prints per-verdict counts, upserts the per-user KB when --apply-kb.
- `kb <domain>` -> inspect a domain's learned dominant template/separator, provider, shape_distribution, known_bad_locals, verified examples.
- `optout <email>` -> add an address to the global suppression list.
- `purge [--days 90]` -> compliance.purge_expired on the per-user silo.
- `web [--host 127.0.0.1] [--port 8765]` -> launch the minimal local web UI.
Exit codes: 0 success, 1 usage error, 2 no candidate/degraded. All output that references M365/catch-all must label them unverifiable, never DELIVERABLE.

WEB: Minimal localhost SPA served by stdlib http.server ONLY (zero web-framework dependency, fully offline, no CDN/external URLs; CSP-safe). Bound to 127.0.0.1 by default. One shared Engine instance behind a request handler (web.create_handler / web.serve).

Single inline page (HTML+CSS+vanilla JS, no build step):
- Single-lookup card: name + (domain | company | pasted LinkedIn URL) inputs, [Verify] and [Use providers] toggles (default off). Renders the FindResult: primary email, a PROVIDER BADGE (M365/Google/Proofpoint/...), a CONFIDENCE BAR (0-100), and a STATUS CHIP (DELIVERABLE/UNDELIVERABLE/RISKY/UNKNOWN) that shows the honest cap note ('capped: Microsoft 365 not RCPT-verifiable' / 'catch-all: pattern-only') when applicable.
- 'Why this guess' popover: renders ScoredCandidate.reasons[] + provenance (template+separator used, KB dominant vs global prior, provider, verification_mode) — doubles as the required per-record provenance surface.
- Batch panel: CSV upload -> sortable results table (same columns as the enriched CSV) -> download enriched CSV. Bounce/confirm buttons per row feed engine.confirm.
- Rescore panel: upload a bounced CSV or DSN mbox -> FixItem table -> download fix-list.
- Public opt-out: a no-login form (POST /api/optout) feeding the global suppression list.

JSON endpoints: POST /api/find, POST /api/batch (multipart), GET /api/export, POST /api/rescore, POST /api/feedback, POST /api/optout (204), GET /api/kb/<domain>. render_result_json serializes FindResult including per-candidate reasons so the UI never re-derives. The page must render both light and dark and never scroll horizontally on the body.

BATCH: Input CSV: any subset of columns {name | first,last}, {domain | company | linkedin_url}; batch.read_input_csv applies an optional --map override to rename source headers. LinkedIn URLs are slug-parsed locally, never fetched.

Processing: batch.run_batch groups rows by resolved domain so MX resolution + provider classification + catch-all fingerprint happen ONCE per distinct domain across the whole file (cache-backed DomainFingerprint), then Engine.find_batch runs per row in input order. SMTP/providers off unless flagged; when on, verification also caches per domain. Compliance suppression is checked per row; suppressed rows emit a row with status suppressed and no address.

Output (write_enriched_csv, ENRICHED_COLUMNS, mail-merge-ready): email, first, last, domain, company, template, separator, provider, status, confidence, is_catch_all, is_role, is_disposable, webmail, alt_candidates, verification_mode, provenance_id. M365/catch-all rows carry UNKNOWN/RISKY with caps applied — never DELIVERABLE. BatchStats returns per-status counts and distinct-domain count for the CLI summary.

## Data flow
  - SINGLE LOOKUP: a surface (cli.py / web.py / batch.py) calls Engine.find(name|first/last, domain|company|linkedin_url, verify=?, use_providers=?).
  - If linkedin_url given -> normalize.parse_linkedin_slug + slug_to_name extract the NAME locally (NEVER fetched); the raw URL is retained only for optional paid finder pass-through.
  - COMPLIANCE GATE: compliance.is_suppressed(email?, name, domain) against the global suppression list -> if opted out, return FindResult(suppressed=True) immediately, no processing.
  - DOMAIN RESOLUTION: use the given domain, else dns_mx.resolve_domain_for_company (offline slugify + live-MX confirm, None if ambiguous). Then cache.get_domain(domain); on miss dns_mx.resolve_mx (MX sorted asc pref, implicit A/AAAA fallback, error='dns_failure' -> UNDELIVERABLE) + provider.classify_provider -> cache.put_domain(DomainFingerprint).
  - STRATEGY: provider.strategy_for -> PROBE / NO_PROBE / PROBE_WITH_CATCHALL_GUARD / NO_PROBE_ACCEPT_ALL.
  - KB LOOKUP: kb_store.get_entry(domain) -> dominant_shape/separator/known_bad_locals/no_bounce_locals or None.
  - NAME PIPELINE: normalize (NFKD/ASCII, titles, punctuation) -> names.expand_variants (nicknames, compound/hyphenated surnames, drop-middle, South-Indian first+initial, mononym; never fabricate surname/digits).
  - GENERATE + RANK: ranking.rank -> if KB dominant_share >= 0.60, candidates.generate_from_kb with the literal template+separator from templates.template_for_kb (opengov=flast, trimble=underscore, purplle=first.l honored via single_token disambiguation over no_bounce_locals) + 1-2 fallbacks; else candidates.generate_from_priors (dot forced). Deduped, prior-sorted.
  - FILTER: filters removes role locals, flags disposable/webmail; filters.in_known_bad -> any candidate matching known_bad_locals is forced UNDELIVERABLE in scoring.
  - OPTIONAL VERIFY (only if verify AND strategy==PROBE AND smtp_probe.port25_open): smtp_probe.verify runs catch-all guard then RCPT on the ranked host, short-circuit on first honest 250; port-25 blocked/timeout -> verification_mode='verification_unavailable', candidates stay pattern-only (NEVER invalid).
  - OPTIONAL PROVIDERS (only if use_providers AND registry.should_route: M365 / catch-all / no-KB-pattern): registry.find_with_fallback, sha256-cache-checked first, short-circuit on FOUND_VERIFIED; pattern_hint fed back for KB upsert.
  - SCORE: scoring.score_candidate per candidate -> 0-100 + Status with catch-all cap ~58, M365/accept-all cap ~50, honest-250-not-catchall -> 90-98, honest 5.1.1/5.1.10 -> UNDELIVERABLE ~2, known_bad -> UNDELIVERABLE; scoring.rank_scored picks best; reasons[] trail built.
  - PROVENANCE + FEEDBACK: compliance.build_provenance -> log_provenance (per-user silo); on DELIVERABLE -> kb_store.upsert_verified.
  - RETURN: FindResult (best + alternates + mx + provenance_id + verification_mode) -> surface renders (web badge/bar/popover) or batch.write_enriched_csv.
  - BATCH: Engine.find_batch dedupes domains so MX/provider/catch-all fingerprint resolves once per distinct domain across the file.
  - RESCORE (killer path): rescore.parse_bounce_csv (records.csv columns) or dsn.iter_mailbox -> classify_bounce by enhanced code (5.1.1/5.1.10 -> WRONG_GUESS: append_known_bad + engine.find corrected candidate; 5.4.1 on M365 -> PROBABLE_INVALID_M365 bank; 5.7.x -> SENDER_SIDE leave; routing_loop/dns_failure -> DOMAIN_ISSUE circuit-break; 4.x.x -> TRANSIENT) -> kb_store upserts to the silo -> rescore.write_fixlist emits the per-address fix CSV; accuracy compounds on the next run.

## Test plan
  - shapes.py GOLDEN: assert shape() matches audit_analysis/analyze_audit.py over the full records.csv corpus (parametrized) — guarantees re-scored KB rows stay schema-identical to the seed.
  - normalize.py: to_ascii(Jose/Muller/Nguyen)->jose/muller/nguyen; clean_token(O'Brien)->obrien; strip_titles_suffixes drops Dr/Jr/III; parse_linkedin_slug does ZERO network I/O (assert under a socket-blocking guard); romanizations returns <=2 for non-Latin, 1 for Latin.
  - names.py: mononym -> only first-only variant, NO fabricated surname, NO digits; South-Indian 'Ashwath S' -> {ashwath.s, ashwaths, ashwath}; 'Saravanan GM' -> saravanan.gm; compound 'Van Der Berg' -> {vanderberg, berg, van}; hyphenated 'Smith-Jones' -> {smithjones, smith, jones}; nickname 'Bob' -> robert included.
  - templates.py + candidates.py: template_for_kb on opengov (single_token, no_bounce sample 'achauhan') -> ('flast',''); trimble -> ('first_last','_') rendering ajith_c; purplle -> ('first.l','.'); global_priors first.last dot forced; cross-product deduped by local_part keeping highest prior.
  - ranking.py: dominant_share on amadeus (203/209 first.last) -> >=0.60 -> KB path emits first.last dot; unknown domain -> global priors; assert first.l NOT bumped domain-class-wide for .in (chargebee/wingify/navi/tessell/easebuzz stay first.last).
  - provider.py: classify_provider gateway precedence — opengov (pphosted+aspmx) -> PROOFPOINT; navi (google+SES-inbound) -> GOOGLE_WORKSPACE (lowest-pref backend wins); harman iphmx -> CISCO_IRONPORT; amadeus outlook -> MICROSOFT365; ukg mimecast -> MIMECAST; empty -> NONE_UNKNOWN; strategy_for M365 -> NO_PROBE.
  - filters.py: in_known_bad on trimble 'ashok_kumar' -> True -> UNDELIVERABLE; role local 'careers'/'hr' filtered; disposable/webmail flags set correctly.
  - scoring.py: honest 250 not-catch-all -> DELIVERABLE 90-98; catch-all -> RISKY capped <=58; M365 pattern-only -> UNKNOWN capped <=50; honest 5.1.1 -> UNDELIVERABLE ~2; known_bad -> UNDELIVERABLE; dns_failure -> 0; reasons[] populated. ASSERT M365/catch-all NEVER produce DELIVERABLE.
  - dns_mx.py: mock resolver — MX sorted asc pref; no-MX domain -> implicit A/AAAA is_implicit=True; NXDOMAIN -> error='dns_failure' (no raise). Optional live integration: resolve a real domain (DNS works in this env).
  - smtp_probe.py: port25_open returns False fast (<6s) in THIS env (port 25 blocked) -> verify returns unavailable=True, engine sets verification_mode='verification_unavailable' and NEVER flips a candidate to UNDELIVERABLE. Mock server: 550 5.1.1 -> invalid, 451 4.7.1 -> retry, 250-random -> catch_all, connect-timeout -> unavailable. Assert DATA is never sent.
  - cache.py: get_domain reused across a batch (assert resolve_mx called once per distinct domain via mock call count); api cache prevents a second billed provider call for identical normalized input; TTL expiry re-resolves.
  - kb_store.py: append_known_bad then get_entry shows the local; upsert_verified bumps shape_distribution + no_bounce_locals; save_kb round-trips losslessly (sets<->sorted lists, ''<->'(none)'); atomic write leaves valid JSON on simulated crash; writes hit the per-user silo, NOT the packaged seed.
  - providers: adapters mocked (respx) — status mapping frozen (MillionVerifier ok->DELIVERABLE, invalid->UNDELIVERABLE, catch_all->RISKY; Hunter webmail->UNKNOWN+flag; Anymail valid/risky/not_found); typed errors on 401/429; registry consults sha256 cache BEFORE calling (assert zero double-charge on repeat) and should_route is True only for M365/catch-all/unknown, False for a PROBE domain with a KB pattern; empty config -> inert.
  - compliance.py: is_suppressed blocks a listed identity -> find returns suppressed=True with no candidates; provenance.jsonl gets one line per find; purge_expired removes an aged record; silos are per-user isolated.
  - engine.py end-to-end (offline, mocked DNS, SMTP+providers off): find('Ajith Kumar','trimble.com') -> best local uses underscore separator from KB; find('Aman Sharma','harman.com') -> first.last high pattern score; find on a suppressed identity -> suppressed=True; provenance written.
  - rescore.py GOLDEN (real audit records.csv): address_not_found rows -> WRONG_GUESS FixItems + corrected candidates + locals banked to known_bad_locals; M365 5.4.1/recipient_rejected -> PROBABLE_INVALID_M365 banked (not discarded); 5.7.x -> SENDER_SIDE untouched; routing_loop (cdk.com) -> DOMAIN_ISSUE; re-running find on a banked local now returns UNDELIVERABLE (accuracy compounds).
  - dsn.py: parse a sample multipart/report DSN -> recipient + enhanced 5.1.1 + reason_class address_not_found; feeds rescore identically to the CSV path.
  - web.py smoke: stdlib test client POST /api/find returns the same FindResult JSON (incl. reasons) as the CLI; POST /api/optout adds to suppression and a subsequent /api/find returns suppressed; batch upload round-trips to enriched CSV; served HTML references NO external http/https URLs; binds 127.0.0.1 only.
  - COMPLIANCE GUARDRAILS (assert-by-construction): import-guard test that no module imports a browser/scraper or issues a network request to linkedin.com; parse_linkedin_slug is provably network-free; grep-style assertion that no result path labels M365/catch-all as DELIVERABLE anywhere in cli/batch/web output.

## Modules

### emailfinder/__init__.py
_Responsibility:_ Public package surface. Re-exports the stable API so internal layout can change freely: from emailfinder import find, Engine, Config, load_config, rescore_csv, Result, Status.
_Depends on:_ emailfinder/engine.py, emailfinder/config.py, emailfinder/models.py, emailfinder/rescore.py
Functions:
  - `def find(name: str | None, domain: str | None = None, *, linkedin_url: str | None = None, company: str | None = None, user_id: str = 'default', verify: bool = False, use_providers: bool = False, config: 'Config | None' = None) -> 'FindResult'`
      Module-level convenience that lazily builds/reuses a default Engine (baked seed KB, default silo) and delegates to Engine.find. The one call every surface and doctest can use.
  - `__version__: str`
      Package version string.

### emailfinder/models.py
_Responsibility:_ The single shared vocabulary: all enums + frozen-ish dataclasses. Pure Python, stdlib only (enum/dataclasses/typing). Every other module imports from here; models.py imports nothing internal, so there are no cycles. Flags (is_catch_all/is_role/is_disposable/webmail) are kept as fields SEPARATE from raw SMTP codes (dossier 5) so results can be reclassified without re-probing.
_Depends on:_ (none)
Functions:
  - `class Status(str, Enum): DELIVERABLE='deliverable'; UNDELIVERABLE='undeliverable'; RISKY='risky'; UNKNOWN='unknown'`
      Hunter-style deliverability label, separate from the 0-100 score.
  - `class Provider(str, Enum): MICROSOFT365; GOOGLE_WORKSPACE; CONSUMER_GMAIL; PROOFPOINT; MIMECAST; CISCO_IRONPORT; BARRACUDA; ZOHO; AMAZON_SES; YAHOO_AOL; OTHER; NONE_UNKNOWN`
      MX-derived provider identity. String values MUST equal the audit's provider strings (microsoft365, google_workspace, proofpoint, mimecast, cisco_ironport, zoho, amazon_ses, other, none_or_unknown) for KB round-trip; enum .value carries the exact audit string.
  - `class VerifyStrategy(str, Enum): PROBE; NO_PROBE; PROBE_WITH_CATCHALL_GUARD; NO_PROBE_ACCEPT_ALL`
      Dossier 4.2 reliability class derived from Provider; decides whether RCPT is even informative.
  - `@dataclass class ParsedName: raw: str; first: str | None; last: str | None; middle: list[str]; initials: list[str]; is_mononym: bool; extra_tokens: list[str]`
      Structured name after cleaning; last is None => mononym branch.
  - `@dataclass class NameVariant: first: str | None; last: str | None; middle: list[str]; initials: list[str]; origin: str`
      One normalized/expanded name form; origin in {as_given, formal, nickname, surname_expansion, first_initial, mononym, romanization} for provenance.
  - `@dataclass class Candidate: local_part: str; template: str; separator: str; shape: str; prior: float; source: str; name_origin: str`
      A concrete local part with the LITERAL template string + separator that produced it (never just the shape family); source in {kb, global}.
  - `@dataclass class MXInfo: domain: str; hosts: list[str]; is_implicit: bool; error: str | None`
      MX hosts sorted ascending by preference; is_implicit=True on A/AAAA fallback (RFC 5321 5.1); error='dns_failure' when neither resolves.
  - `@dataclass class DomainFingerprint: domain: str; provider: Provider; mx: list[str]; is_catch_all: bool | None; learned_template: str | None; learned_separator: str | None; last_probed_at: float; flags: dict`
      Cached per-domain verdict; is_catch_all tri-state (None=unknown).
  - `@dataclass class SmtpResult: code: int | None; enhanced: str | None; verdict: str; reason: str; unavailable: bool`
      One RCPT outcome; verdict in {valid, invalid, catch_all, retry, non_signal, unknown}; unavailable=True on port-25 block/timeout (NEVER invalid).
  - `@dataclass class ScoredCandidate: candidate: Candidate; score: int; status: Status; is_catch_all: bool; is_role: bool; is_disposable: bool; webmail: bool; reasons: list[str]`
      Candidate after scoring; reasons is the human-readable 'why this guess' trail surfaced in the web popover, CLI, and provenance.
  - `@dataclass class FindResult: query: dict; domain: str | None; provider: Provider; strategy: VerifyStrategy; best: ScoredCandidate | None; alternates: list[ScoredCandidate]; mx: MXInfo | None; verification_mode: str; provenance_id: str | None; suppressed: bool; notes: list[str]`
      The single object every surface renders; verification_mode in {none, smtp, provider, verification_unavailable}.
  - `@dataclass class BounceRow: raw: dict; email: str; local: str; domain: str; smtp_code: int | None; enhanced: str | None; reason_class: str | None; provider_hint: str | None`
      One parsed row from a bounced/audit CSV or DSN, normalized for the re-scorer.
  - `@dataclass class FixItem: email: str; domain: str; verdict: str; enhanced: str | None; action: str; corrected_candidate: str | None; kb_change: str | None; detail: str`
      One re-scorer output row; verdict in {WRONG_GUESS, PROBABLE_INVALID_M365, SENDER_SIDE, DOMAIN_ISSUE, TRANSIENT, UNKNOWN}; action in {bank_known_bad, probable_invalid, sender_side_skip, circuit_break, retry_soft}.

### emailfinder/errors.py
_Responsibility:_ Typed exception hierarchy for provider adapters + engine (dossier 6.3), so the registry/engine branch on error kind (fail-fast vs skip vs retry vs circuit-break) instead of parsing strings.
_Depends on:_ (none)
Functions:
  - `class ProviderError(Exception); class ErrAuth(ProviderError); class ErrQuotaExhausted(ProviderError); class ErrRateLimited(ProviderError): retry_after: float; class ErrTimeout(ProviderError); class ErrProviderDown(ProviderError); class ErrBadInput(ProviderError)`
      ErrAuth disables the provider, ErrQuotaExhausted skips to next billing tick, ErrRateLimited carries retry_after, ErrTimeout retries once, ErrProviderDown circuit-breaks 5 min, ErrBadInput is non-retryable.
  - `class ComplianceBlocked(Exception)`
      Raised/handled internally when a query target is on the global suppression list; engine converts to FindResult(suppressed=True).

### emailfinder/config.py
_Responsibility:_ Load/validate runtime config from defaults + optional JSON/env + explicit overrides. Holds ALL tunable numeric constants (global priors location, scoring weights, confidence caps, cache TTLs, timeouts), feature flags (SMTP off, providers off), per-user silo paths, and provider credentials via api_key_env. Fully functional with no config file (zero-provider defaults).
_Depends on:_ emailfinder/models.py
Functions:
  - `@dataclass class ScoreConfig: w_src: float; w_dom: float; w_pat: float; w_smtp: float; catchall_cap: int = 58; m365_cap: int = 50; accept_all_cap: int = 50; kb_match_base: tuple[int, int] = (75, 85); global_prior_base: tuple[int, int] = (55, 65)`
      Tunable scoring weights + hard caps for catch-all and M365 (never 'verified'). All defaults, not audit-calibrated (documented).
  - `@dataclass class ProviderConfig: name: str; api_key_env: str; enabled: bool = False; priority: int = 100; role: str = 'finder'; max_credits_per_day: int | None = None`
      One paid-provider registration; enabled defaults False (zero-provider mode).
  - `@dataclass class Config: data_dir: Path; user_id: str = 'default'; enable_smtp: bool = False; enable_providers: bool = False; smtp_connect_timeout: float = 6.0; smtp_cmd_timeout: float = 30.0; mail_from: str | None = None; kb_dominance_threshold: float = 0.60; domain_cache_ttl_days: int = 14; verify_cache_ttl_days: int = 30; find_cache_ttl_days: int = 90; retention_days: int = 90; score: ScoreConfig = ...; providers: list[ProviderConfig] = ...; package_data_dir: Path = ...`
      Whole-app config; SMTP + providers OFF by default; short smtp_connect_timeout so a port-25 block is detected fast. package_data_dir points at emailfinder/data (read-only seeds).
  - `def load_config(path: Path | None = None, overrides: dict | None = None) -> Config`
      Layer hardcoded defaults <- optional config.json <- env vars <- overrides. Returns an all-defaults Config when path is None. Resolves data_dir to ~/.emailfinder by default.

### emailfinder/shapes.py
_Responsibility:_ PURE structural classifier for a local part: a byte-for-byte port of audit_analysis/analyze_audit.py shape() so re-scored KB rows and seed KB rows share ONE taxonomy. Used by kb_store, rescore, and feedback; NOT by generation. Zero I/O, zero deps.
_Depends on:_ (none)
Functions:
  - `def shape(local: str) -> tuple[str, str]`
      Return (shape_label, separator) exactly mirroring the audit: for each sep in ('.','_','-'): if present and 2 tokens -> 'f{sep}last' (len(a)==1), 'first{sep}l' (len(b)==1), else 'first{sep}last'; >2 tokens -> 'multi{sep}'. Else local.isalpha() -> ('single_token',''); [a-z]+\d+ -> ('name+digits',''); else ('other',''). Must be validated golden against records.csv.

### emailfinder/normalize.py
_Responsibility:_ PURE token normalization (dossier 1.1-1.2 clean steps) + LOCAL-ONLY LinkedIn slug parsing (clean-room red line, dossier 8.1). Zero network by construction. Uses stdlib unicodedata NFKD; optional text-unidecode/anyascii for richer transliteration with graceful fallback.
_Depends on:_ (none)
Functions:
  - `def to_ascii(text: str) -> str`
      NFKD-decompose, drop combining marks, transliterate (Jose/Muller/Nguyen); uses text-unidecode/anyascii if importable else NFKD-only ASCII fold.
  - `def strip_titles_suffixes(tokens: list[str]) -> list[str]`
      Remove Dr/Mr/Ms/Prof and Jr/Sr/II/III/IV/PhD from a token list.
  - `def clean_token(tok: str) -> str`
      Lowercase, drop punctuation/apostrophes (O'Brien->obrien), collapse whitespace; '' if nothing alphanumeric remains.
  - `def is_linkedin_url(s: str) -> bool`
      True if the string looks like a linkedin.com profile URL, so the engine routes it to slug-parse, never to any fetch.
  - `def parse_linkedin_slug(url: str) -> str | None`
      Extract the /in/<slug> segment via urllib.parse only (NO network I/O; enforced by the import-guard test). None if not a /in/ profile URL.
  - `def slug_to_name(slug: str) -> str`
      'ajith-kumar-c-12ab34' -> 'ajith kumar c': strip trailing hash/numeric tokens, replace hyphens with spaces.
  - `def romanizations(text: str, top_k: int = 2) -> list[str]`
      Up to top_k ASCII romanizations for non-Latin scripts; [to_ascii(text)] for Latin input.

### emailfinder/names.py
_Responsibility:_ PURE India-aware name-variant expansion (dossier 1.3-1.5). Splits a cleaned name into ParsedName, then generates NameVariants: nicknames (bidirectional), compound/hyphenated surnames, drop-middle, South-Indian first+initial, mononyms. NEVER fabricates a surname or appends digits.
_Depends on:_ emailfinder/models.py, emailfinder/normalize.py
Functions:
  - `def parse_name(name: str) -> ParsedName`
      Tokenize a cleaned name into first/middle/last/initials/mononym; single-letter tokens -> initials (South-Indian); single token -> is_mononym=True.
  - `def expand_variants(pn: ParsedName, nickname_table: dict[str, list[str]]) -> list[NameVariant]`
      Deduped ordered variant set (as-given first): nickname/formal/diminutive; compound-surname join-all/last-token/first-token/hyphen forms; drop-middle (highest) + first.middle.last; first+initial and first-only for initial-bearing and mononym names. Each variant carries an origin string. Never invents a surname for a mononym.
  - `def load_nicknames(path: Path) -> dict[str, list[str]]`
      Load the vendored bidirectional nickname JSON into a lookup usable in both directions (cached).

### emailfinder/templates.py
_Responsibility:_ PURE literal template registry + the ordered global-prior list (dossier 1.2) + KB shape->literal-template translation with single_token disambiguation. Storing/rendering literal template+separator (not shape family) is the fix for opengov=flast, trimble=underscore, purplle=first.l.
_Depends on:_ emailfinder/models.py
Functions:
  - `def render(template: str, v: NameVariant, sep: str) -> str | None`
      Render one local part, e.g. 'first.last'+'.'->'ajith.kumar', 'flast'->'akumar', 'first_l'+'_'->'ajith_k'. Returns None when the variant lacks required tokens (flast on a mononym) so mononyms never fabricate a surname.
  - `def global_priors() -> list[tuple[str, str, float]]`
      Load data/global_priors.json into the dossier-1.2 ordered (template, forced_separator, prior) list: ('first.last','.',0.60),('flast','',0.12),('first','',0.08),... dot forced, underscore/hyphen near-zero.
  - `def template_for_kb(kb_entry: dict) -> tuple[str, str]`
      Translate a KB entry's dominant_shape+dominant_separator into a concrete (template, separator) the renderer understands. Normalizes '(none)'->''. For ambiguous dominant_shape=='single_token', inspects a sample of the entry's no_bounce_locals to disambiguate flast (opengov 'achauhan') vs bare-first vs firstlast; falls back to flast per dossier.

### emailfinder/candidates.py
_Responsibility:_ PURE candidate generation: cross NameVariants with templates, render, drop empties, and DEDUPE the variant x template cross-product (dossier 1.3 step 5). Two entry points: KB-driven and global-priors.
_Depends on:_ emailfinder/models.py, emailfinder/templates.py
Functions:
  - `def generate_from_kb(variants: list[NameVariant], dominant_template: str, dominant_separator: str, fallbacks: list[tuple[str, str]]) -> list[Candidate]`
      Emit the KB dominant template (prior ~0.9) plus 1-2 fallbacks across all variants; dedupe by local_part keeping highest prior + its provenance (source='kb').
  - `def generate_from_priors(variants: list[NameVariant], priors: list[tuple[str, str, float]]) -> list[Candidate]`
      Emit the full dossier-1.2 ordered set across all variants, dot forced, deduped (source='global').

### emailfinder/ranking.py
_Responsibility:_ PURE ranking that picks the generation path and orders candidates. Implements the dossier-1.1 two-tier rule: KB dominant_shape >= threshold (default 0.60) wins (emit literal template+separator + 1-2 fallbacks); else global priors. Optional SMB size-conditioning boost only behind a config flag; never bumps first.l domain-class-wide for .in (dossier 1.5 correction).
_Depends on:_ emailfinder/models.py, emailfinder/candidates.py, emailfinder/templates.py
Functions:
  - `def dominant_share(kb_entry: dict) -> tuple[str, float]`
      Compute (dominant_shape, share) from shape_distribution so the 60% gate is explicit and testable.
  - `def rank(variants: list[NameVariant], kb_entry: dict | None, priors: list[tuple[str, str, float]], threshold: float = 0.60, size_hint: int | None = None) -> list[Candidate]`
      If kb_entry present and dominant_share >= threshold: generate_from_kb(template_for_kb(kb_entry) + 1-2 next-most-common shapes). Else generate_from_priors. Returns candidates sorted by prior desc.

### emailfinder/provider.py
_Responsibility:_ PURE provider classification + strategy mapping from an MX host LIST (no DNS here — dns_mx supplies hosts, keeping this unit-testable). Ports audit classify_provider and extends it with dossier-4.1 gateway precedence + lowest-preference-backend tie-break, then maps Provider -> VerifyStrategy (dossier 4.2).
_Depends on:_ emailfinder/models.py
Functions:
  - `def classify_provider(mx_hosts: list[str]) -> Provider`
      Case-insensitive suffix match with precedence: any security-gateway suffix (pphosted/proofpoint/mimecast/iphmx/cisco/barracuda) wins over backend (opengov pphosted+aspmx -> PROOFPOINT); else M365/Google/Zoho/SES/Yahoo; SES-inbound loses to a lower-preference backend (navi/rapido -> GOOGLE_WORKSPACE); empty -> NONE_UNKNOWN; unmatched -> OTHER. Provider ordering matches the audit port. Optionally data-driven via provider_map.json.
  - `def strategy_for(provider: Provider) -> VerifyStrategy`
      {google_workspace,proofpoint,mimecast,cisco_ironport} -> PROBE; microsoft365 -> NO_PROBE; {zoho,barracuda,other} -> PROBE_WITH_CATCHALL_GUARD; {yahoo_aol,amazon_ses} -> NO_PROBE_ACCEPT_ALL.
  - `def load_provider_map(path: Path | None = None) -> list[tuple[str, Provider, bool]]`
      Load data/provider_map.json as ordered (suffix, provider, is_gateway) rows so precedence is data-driven and editable.
  - `def pick_probe_host(mx: MXInfo) -> str | None`
      Return the lowest-preference (already sorted) host to probe, honoring implicit-MX A/AAAA fallback.

### emailfinder/filters.py
_Responsibility:_ PURE suppression/flagging filters from vendored static JSON: drop role/functional locals from person-guessing (dossier 1.6), flag disposable domains, flag webmail, and enforce known_bad_locals -> caller forces UNDELIVERABLE (dossier 5, the audit's #1 bounce-cause killer).
_Depends on:_ emailfinder/models.py
Functions:
  - `def is_role_local(local: str, role_set: set[str]) -> bool`
      True for info/hr/careers/hiring/sales/support/admin/engineering/recruiting etc.; removed from person guessing.
  - `def is_disposable_domain(domain: str, disposable_set: set[str]) -> bool`
      Membership check against the vendored disposable-domains JSON.
  - `def is_webmail(domain: str, webmail_set: set[str]) -> bool`
      Flag gmail.com/yahoo.com/outlook.com etc. -> webmail=True (status UNKNOWN, may still be deliverable).
  - `def in_known_bad(local: str, kb_entry: dict | None) -> bool`
      True if local is in the domain's known_bad_locals -> caller forces UNDELIVERABLE.
  - `def load_static_sets(data_dir: Path) -> dict[str, set[str]]`
      Load role_locals.json, disposable_domains.json, webmail_domains.json into sets (cached).

### emailfinder/scoring.py
_Responsibility:_ PURE confidence + status resolution (dossier 5). Combines pattern evidence, provider strategy, catch-all state, SMTP signal (gated by provider), and flags into a 0-100 score + Status, applying catch-all and M365 hard caps and the known_bad force-undeliverable rule. Emits the human-readable reasons trail. Runs AFTER optional verification.
_Depends on:_ emailfinder/models.py, emailfinder/config.py
Functions:
  - `def score_candidate(cand: Candidate, provider: Provider, strategy: VerifyStrategy, is_catch_all: bool | None, smtp: SmtpResult | None, flags: dict, cfg: ScoreConfig, kb_match: bool) -> ScoredCandidate`
      Base = kb_match ? (75-85 scaled by prior) : (55-65 global first.last, lower for unusual shape). Apply: syntax/MX fail -> UNDELIVERABLE/0; honest 550 5.1.1/5.1.10 -> UNDELIVERABLE/~2; honest 250 not-catch-all -> DELIVERABLE/90-98; catch-all -> RISKY capped ~58; M365/accept-all/greylist-uncleared/rate-limited/timeout -> UNKNOWN capped ~50; role/disposable -> RISKY overlay; known_bad -> UNDELIVERABLE. Records every applied rule in reasons[]. NEVER lets M365/catch-all reach DELIVERABLE.
  - `def resolve_status(score: int, provider: Provider, is_catch_all: bool | None, smtp: SmtpResult | None, flags: dict) -> Status`
      Deterministic status decision table (dossier 5) separated from scoring so both are independently testable.
  - `def rank_scored(scored: list[ScoredCandidate]) -> list[ScoredCandidate]`
      Final ordering: DELIVERABLE > RISKY > UNKNOWN > UNDELIVERABLE, then score desc; picks FindResult.best.

### emailfinder/dns_mx.py
_Responsibility:_ I/O: MX/A/AAAA resolution via dnspython with the dossier-2.1 fallback chain and a short timeout. Sorts MX ascending by preference. The ONLY DNS entry point (mockable in tests). The only always-on network call in the default path.
_Depends on:_ emailfinder/models.py
Functions:
  - `def resolve_mx(domain: str, timeout: float = 5.0) -> MXInfo`
      Query MX sorted ascending by preference; on no MX fall back to A/AAAA as implicit preference-0 host (is_implicit=True); if neither resolves set error='dns_failure' (caller -> UNDELIVERABLE). Never raises for NXDOMAIN.
  - `def resolve_domain_for_company(company: str, cfg: Config) -> str | None`
      Best-effort public-domain guess from a company name (offline slugify -> <slug>.com/.in/.io ordered, accepted only when a live MX resolves). Returns None when ambiguous so the surface prompts for an explicit domain. Does NOT scrape.

### emailfinder/smtp_probe.py
_Responsibility:_ I/O: OPTIONAL, off-by-default SMTP RCPT prober (dossier 2-3). Detects the port-25 block within seconds and returns unavailable=True (NEVER invalid). Does catch-all guard + real-address RCPT in one session, never sends DATA, maps code+enhanced-code per dossier 2.2. Isolated so it is trivially mockable (this env has :25 blocked).
_Depends on:_ emailfinder/models.py, emailfinder/config.py, emailfinder/provider.py
Functions:
  - `def port25_open(host: str = 'gmail-smtp-in.l.google.com', timeout: float = 6.0) -> bool`
      Fast pre-flight TCP connect; a hang/timeout (no RST) within the short timeout -> False (blocked). Cached per run so the whole batch degrades instantly instead of hanging. Load-bearing; validated blocked in this env.
  - `def probe_domain_catchall(host: str, mail_from: str, domain: str, cfg: Config) -> bool | None`
      RCPT a high-entropy fake local (zzq...-noexist-<ts>@domain), confirm with 2-3 randoms in the same session; all 250 -> True (catch-all), consistent reject -> False, inconsistent/unavailable -> None.
  - `def probe_rcpt(host: str, mail_from: str, email: str, cfg: Config) -> SmtpResult`
      Open :25 with short connect timeout, read banner, EHLO real FQDN, MAIL FROM real deliverable probe, RCPT TO, QUIT (never DATA/VRFY). Map 250->valid, 550 5.1.1/5.1.10->invalid, 4xx/451 4.7.1->retry, 5.4.1/5.7.x->non_signal, 552->non_signal, timeout/refused->unavailable(unknown).
  - `def verify(email: str, mx: MXInfo, strategy: VerifyStrategy, cfg: Config) -> SmtpResult`
      Top-level guard: if strategy in {NO_PROBE, NO_PROBE_ACCEPT_ALL} or not port25_open -> SmtpResult(unavailable=True, verdict='unknown', reason='verification_unavailable') without probing. Else catch-all guard then RCPT on the chosen host.

### emailfinder/cache.py
_Responsibility:_ I/O: single sqlite store with two tables: domain fingerprints (provider/mx/is_catch_all tri-state/learned pattern+separator/last_probed_at, TTL, fingerprint-once per batch) and the sha256-keyed provider-result cache (mandatory in front of paid verifiers).
_Depends on:_ emailfinder/models.py
Functions:
  - `class Cache:
    def __init__(self, path: Path)`
      Open/create the sqlite file in the per-user silo; create tables if absent.
  - `def get_domain(self, domain: str, ttl_days: int = 14) -> DomainFingerprint | None`
      Return a non-expired cached DomainFingerprint or None on miss/expiry.
  - `def put_domain(self, fp: DomainFingerprint) -> None`
      Upsert a fingerprint with last_probed_at=now.
  - `@staticmethod
def api_key(provider: str, normalized_input: str) -> str`
      sha256(provider + normalized_input) idempotency key.
  - `def get_api(self, key: str, ttl_days: int) -> dict | None; def put_api(self, key: str, value: dict, ttl_days: int) -> None`
      Read/write cached provider responses (30d verify / 90d find) so a cache miss never double-charges (mandatory before MillionVerifier).

### emailfinder/kb_store.py
_Responsibility:_ I/O: load/lookup/upsert the domain knowledge base. Seeds from emailfinder/data/domain_kb.seed.json (219-domain, schema-identical to audit_analysis/domain_kb.json) into a per-user silo overlay on first run; runtime upserts write to the overlay, NEVER mutating the packaged seed. Implements the dossier-5 feedback loop and the audit-shape-identical upsert (via shapes.shape).
_Depends on:_ emailfinder/models.py, emailfinder/shapes.py
Functions:
  - `def load_kb(path: Path, seed_path: Path) -> dict[str, dict]`
      Load the per-user KB (copying the seed on first run); normalize dominant_separator '(none)'->'' at the edges only when rendering (round-trip preserved on save). Returns {} only if both absent.
  - `def get_entry(kb: dict, domain: str) -> dict | None`
      Case-insensitive domain lookup.
  - `def upsert_verified(kb: dict, path: Path, domain: str, template: str, separator: str, provider: Provider, example_local: str) -> None`
      On DELIVERABLE/confirm: set/refresh dominant template+separator+provider, append example to no_bounce_locals, bump shape_distribution via shapes.shape. Atomic write (tmp+rename).
  - `def append_known_bad(kb: dict, path: Path, domain: str, local: str, source: str) -> None`
      On true not-found or DBEB-M365 5.4.1: append local to known_bad_locals (deduped), record source. Atomic write.
  - `def save_kb(kb: dict, path: Path) -> None`
      Atomically serialize the KB back to the silo (sets->sorted lists, ''->'(none)' round-trip).

### emailfinder/compliance.py
_Responsibility:_ I/O + pure helpers merged: the clean-room legal gate (dossier 8.1). Per-user data silo, global cross-user suppression/opt-out check run BEFORE any result is returned, per-record provenance JSONL log, and ~90-day retention purge. A hard gate, not advisory.
_Depends on:_ emailfinder/models.py
Functions:
  - `class Compliance:
    def __init__(self, user_id: str, base_dir: Path, retention_days: int = 90)`
      Resolve/create the per-user silo (kb overlay, cache.sqlite, suppression, provenance.jsonl).
  - `def silo_paths(self) -> dict[str, Path]`
      Return the per-user file paths the Engine wires into kb_store/cache.
  - `def is_suppressed(self, email: str | None, name: str | None, domain: str | None) -> bool`
      True if the address or a normalized name@domain key is on the global suppression/opt-out set; engine short-circuits to suppressed=True before returning any result.
  - `def add_suppression(self, email: str | None, name: str | None, domain: str | None, source: str) -> None`
      Append an opt-out (fed by the public opt-out endpoint or a 5.x DSN).
  - `def build_provenance(self, query: dict, mx: MXInfo | None, chosen: Candidate | None, verification_mode: str, reasons: list[str]) -> dict`
      Assemble the provenance record: source='user-entered name + public MX', linkedin_slug_local_only flag, template+separator used, provider, reasons trail, timestamp, user_id.
  - `def log_provenance(self, record: dict) -> str`
      Append one provenance record to the per-user JSONL log; return its id.
  - `def purge_expired(self) -> int`
      Delete per-user provenance/cache rows older than retention_days; return count purged.

### emailfinder/providers/base.py
_Responsibility:_ I/O: abstract EmailFinder/EmailVerifier interfaces + request/result dataclasses (dossier 6.3). Providers optional, off by default, routed only to M365/catch-all/unknown domains.
_Depends on:_ emailfinder/models.py, emailfinder/errors.py
Functions:
  - `class EmailFinder(ABC): def name(self)->str; def find(self, req:'FindRequest')->'ProviderFindResult'; def estimated_cost_credits(self, req)->float; def healthy(self)->bool`
      Finder interface; find validates linkedin_url-alone OR name+company and normalizes casing/diacritics before dispatch.
  - `class EmailVerifier(ABC): def name(self)->str; def verify(self, email:str, *, timeout_ms:int=15000, deep:bool=False)->'ProviderVerifyResult'; def healthy(self)->bool`
      Verifier interface.
  - `@dataclass class FindRequest: first_name: str|None; last_name: str|None; full_name: str|None; domain: str|None; company_name: str|None; linkedin_url: str|None; timeout_ms: int = 30000`
      Validated payload; linkedin_url alone OR name+company.
  - `@dataclass class ProviderFindResult: email: str|None; status: str; confidence: int; pattern_hint: str|None; provider: str; credits_charged: float; latency_ms: int; raw: dict`
      status in {FOUND_VERIFIED, FOUND_UNVERIFIED, FOUND_CATCH_ALL, NOT_FOUND}.
  - `@dataclass class ProviderVerifyResult: email: str; status: Status; reason: str; is_catch_all: bool; is_disposable: bool; is_role: bool; webmail: bool; score: int|None; provider: str; credits_charged: float; raw: dict`
      Unified verifier output mapped from each vendor per the dossier-6.3 status table.

### emailfinder/providers/anymailfinder.py
_Responsibility:_ I/O: the ONE concrete reference paid finder adapter (Anymail Finder, dossier 6.1) over httpx/urllib. Proves the interface; charges only on verified find; maps to the common shape; raises the typed errors.
_Depends on:_ emailfinder/providers/base.py, emailfinder/errors.py
Functions:
  - `class AnymailFinderAdapter(EmailFinder):
    def __init__(self, api_key: str, session=None)
    def find(self, req: FindRequest) -> ProviderFindResult`
      POST /v5.1/find-email/person with bare-key auth; accepts linkedin_url alone or name+domain; maps valid->FOUND_VERIFIED, risky->FOUND_CATCH_ALL, not_found->NOT_FOUND (credits 0 for risky/not_found); 180s timeout; ErrAuth/ErrRateLimited/ErrTimeout on failures.

### emailfinder/providers/hunter.py
_Responsibility:_ I/O: nice-to-have Hunter.io adapter stub (finder + verifier + Domain-Search pattern-into-KB, dossier 6.1). Interface-complete; enforces BOTH rate windows.
_Depends on:_ emailfinder/providers/base.py, emailfinder/errors.py
Functions:
  - `class HunterAdapter(EmailFinder, EmailVerifier): def find(...); def verify(...); def domain_pattern(self, domain:str)->tuple[str,str]|None`
      GET /v2/email-finder, /v2/email-verifier, /v2/domain-search; maps valid/invalid/accept_all/webmail/unknown; webmail->UNKNOWN+webmail flag; domain_pattern returns (template,separator) for KB upsert; 15rps+500rpm finder / 10rps+300rpm verifier; HTTP 202 -> UNKNOWN-pending.

### emailfinder/providers/millionverifier.py
_Responsibility:_ I/O: nice-to-have MillionVerifier bulk-verifier adapter stub (dossier 6.1). Cheap; NO server-side dedupe so the sha256 cache in cache.py MUST be consulted first (enforced by the registry, not this adapter).
_Depends on:_ emailfinder/providers/base.py, emailfinder/errors.py
Functions:
  - `class MillionVerifierAdapter(EmailVerifier):
    def verify(self, email: str, *, timeout_ms: int = 15000, deep: bool = False) -> ProviderVerifyResult`
      GET api/v3/; map ok->DELIVERABLE, invalid->UNDELIVERABLE(mailbox_not_found), catch_all/disposable->RISKY, unknown/unverified->UNKNOWN.

### emailfinder/providers/registry.py
_Responsibility:_ I/O: build enabled providers from Config, enforce the MANDATORY cache-before-call, apply routing (only M365/catch-all/unknown-pattern domains -> ~68-70% paid-volume cut), per-day credit budgets, typed-error handling, and the dossier-6.3 orchestration order with short-circuit. Empty config => inert (zero-provider mode).
_Depends on:_ emailfinder/providers/base.py, emailfinder/cache.py, emailfinder/config.py
Functions:
  - `def build_registry(cfg: Config, cache: Cache) -> 'ProviderRegistry'`
      Instantiate only enabled adapters, ordered by priority; wrap the shared Cache.
  - `def should_route(self, provider: Provider, is_catch_all: bool | None, has_kb_pattern: bool) -> bool`
      True only when provider==MICROSOFT365, is_catch_all is True, or there is no dominant KB pattern; else False (skip paid calls).
  - `def find_with_fallback(self, req: FindRequest, provider: Provider, is_catch_all: bool | None) -> ProviderFindResult | None`
      Only fire when should_route; check provider_cache (sha256) first; try verifier fallback then finder chain; short-circuit on FOUND_VERIFIED/DELIVERABLE; honor typed errors, daily budgets, and circuit-breaks. Feeds pattern_hint back to the caller for KB upsert.

### emailfinder/engine.py
_Responsibility:_ The orchestrator and single stable API every surface calls. Wires the PURE core + I/O into one fixed, known-good pipeline (no reorderable stages). Holds no scoring logic itself. Loads KB, static sets, nicknames, cache, compliance, provider registry once and reuses them across queries and batch rows.
_Depends on:_ emailfinder/config.py, emailfinder/models.py, emailfinder/normalize.py, emailfinder/names.py, emailfinder/candidates.py, emailfinder/ranking.py, emailfinder/provider.py, emailfinder/filters.py, emailfinder/scoring.py, emailfinder/dns_mx.py, emailfinder/smtp_probe.py, emailfinder/cache.py, emailfinder/kb_store.py, emailfinder/compliance.py, emailfinder/providers/registry.py
Functions:
  - `class Engine:
    def __init__(self, cfg: Config)`
      Build Compliance (silo), load KB overlay (kb_store), static filter sets, nickname table, open Cache, optionally build the provider Registry (off unless configured).
  - `def find(self, name: str | None = None, domain: str | None = None, *, first: str | None = None, last: str | None = None, company: str | None = None, linkedin_url: str | None = None, verify: bool = False, use_providers: bool = False) -> FindResult`
      Full pipeline: parse linkedin_url slug LOCALLY if given; compliance suppression gate (early return suppressed=True); resolve domain (arg or company); cache.get_domain else dns_mx.resolve_mx + provider.classify_provider -> put_domain; provider.strategy_for; kb_store.get_entry; normalize+names.expand_variants; ranking.rank; filters (drop role, flag disposable/webmail, known_bad->UNDELIVERABLE); optional smtp_probe.verify (only if verify AND strategy PROBE AND port25_open) with catch-all guard; optional registry.find_with_fallback (only if use_providers AND should_route); scoring.score_candidate per candidate + rank_scored; compliance.build_provenance + log_provenance; on DELIVERABLE kb_store.upsert_verified. Never marks timeouts invalid; M365/catch-all hard-capped.
  - `def find_batch(self, rows: Iterable[dict]) -> Iterator[FindResult]`
      Batch path with per-domain fingerprint-once dedupe (MX/provider/catch-all resolved once per distinct domain across the batch); preserves input order.
  - `def confirm(self, email: str, domain: str, *, deliverable: bool) -> None`
      Feedback hook: deliverable=True -> kb_store.upsert_verified; not-found -> kb_store.append_known_bad. Used by web confirm buttons and rescore.
  - `def close(self) -> None`
      Flush KB overlay + cache to the silo.

### emailfinder/dsn.py
_Responsibility:_ Parser for DSN/bounce messages (nice-to-have superset of the CSV path): extract recipient + RFC 3463 enhanced status code + reason class from a raw message or an mbox/Maildir. Mostly-pure (stdlib email/mailbox).
_Depends on:_ emailfinder/models.py
Functions:
  - `def parse_dsn_message(raw: bytes) -> list[BounceRow]`
      Parse a multipart/report message/delivery-status part -> BounceRows (recipient, smtp_code, enhanced, reason_text).
  - `def iter_mailbox(path: Path) -> Iterator[BounceRow]`
      Iterate an mbox/Maildir yielding parsed BounceRows.
  - `def classify_enhanced(enhanced: str | None, code: int | None, reason_text: str) -> str`
      Map to a reason_class matching the audit taxonomy (address_not_found, recipient_rejected, routing_loop, dns_failure, policy_or_spam_rejection, inactive_account).

### emailfinder/rescore.py
_Responsibility:_ THE HEADLINE FEATURE: re-score a bounced/audit list. Ingest the audit CSV (records.csv columns) or a DSN mailbox, bucket each row by RFC 3463 enhanced code, emit a per-address FixItem list + corrected candidate via engine.find, and upsert the KB so accuracy compounds (dossier 7.7).
_Depends on:_ emailfinder/models.py, emailfinder/engine.py, emailfinder/dsn.py, emailfinder/kb_store.py, emailfinder/shapes.py
Functions:
  - `ENHANCED_CODE_MAP: dict[str, str]`
      Map enhanced/reason_class -> verdict: 5.1.1/5.1.10 & address_not_found -> WRONG_GUESS; 5.4.1 on M365(DBEB) & recipient_rejected -> PROBABLE_INVALID_M365; 5.7.x/policy_or_spam -> SENDER_SIDE; routing_loop/dns_failure/connection_failure -> DOMAIN_ISSUE; 4.x.x -> TRANSIENT.
  - `def parse_bounce_csv(path: Path, column_map: dict[str, str] | None = None) -> list[BounceRow]`
      Read a bounced/audit CSV (auto-detect records.csv columns company/email/domain/local/shape/sep/bounce_status/reason_class, or a generic email+code CSV) into BounceRows.
  - `def classify_bounce(row: BounceRow, provider: Provider | None) -> str`
      Return the verdict using provider class so M365 5.4.1 stays probable-invalid-bank (not discard) and 5.7.x/DBEB aren't misread as mailbox signals.
  - `def rescore_csv(path: Path, engine: Engine, kb_path: Path, apply_kb: bool = True) -> list[FixItem]`
      For each row: WRONG_GUESS/PROBABLE_INVALID_M365 -> append_known_bad + re-run engine.find excluding the bad local -> corrected_candidate; DOMAIN_ISSUE -> circuit_break flag; SENDER_SIDE -> leave untouched; TRANSIENT -> retry_soft. Persists KB when apply_kb. Returns the FixItem list.
  - `def rescore_mailbox(path: Path, engine: Engine, kb_path: Path, apply_kb: bool = True) -> list[FixItem]`
      Same buckets driven by dsn.iter_mailbox instead of a CSV.
  - `def write_fixlist(items: list[FixItem], out: Path) -> None`
      Write the mail-merge-ready per-address fix CSV (email, verdict, action, corrected_candidate, kb_change, detail).

### emailfinder/batch.py
_Responsibility:_ Batch-CSV read/write helpers: column mapping, per-domain fingerprint-once dedupe orchestration over Engine.find_batch, and the mail-merge-ready enriched output (the exact column set a job-seeker needs).
_Depends on:_ emailfinder/models.py, emailfinder/engine.py
Functions:
  - `ENRICHED_COLUMNS: list[str]`
      email, first, last, domain, company, template, separator, provider, status, confidence, is_catch_all, is_role, is_disposable, webmail, alt_candidates, verification_mode, provenance_id.
  - `def read_input_csv(path: Path, mapping: dict[str, str] | None = None) -> list[dict]`
      Read name/first/last/domain/company/linkedin_url rows with an optional column-mapping override.
  - `def run_batch(engine: Engine, in_csv: Path, out_csv: Path, *, mapping: dict[str, str] | None = None, verify: bool = False, use_providers: bool = False) -> 'BatchStats'`
      Read rows, group by domain so MX/provider/catch-all resolve once per domain, run engine.find_batch, write the enriched CSV; preserves input order; returns counts.
  - `def write_enriched_csv(results: Iterable[FindResult], out: Path) -> None`
      Emit ENRICHED_COLUMNS for mail-merge.

### emailfinder/cli.py
_Responsibility:_ Scriptable CLI (argparse) over the core: single lookup, batch CSV, bounce re-score, KB inspect, opt-out, purge, and launch-web. Human-readable + --json output. Thin wrapper; console_scripts entry emailfinder=emailfinder.cli:main.
_Depends on:_ emailfinder/engine.py, emailfinder/batch.py, emailfinder/rescore.py, emailfinder/config.py, emailfinder/web.py
Functions:
  - `def main(argv: list[str] | None = None) -> int`
      argparse dispatcher for subcommands find/batch/rescore/kb/optout/purge/web. --smtp / --providers opt-in flags; --json machine output. SMTP/providers off unless flagged.
  - `def cmd_find(args) -> int; def cmd_batch(args) -> int; def cmd_rescore(args) -> int`
      find: single FindResult with candidate table + why-this-guess reasons; batch: run_batch + enriched CSV with a progress line; rescore: write fix-list CSV + per-verdict counts, upsert KB.
  - `def cmd_kb(args) -> int; def cmd_optout(args) -> int; def cmd_web(args) -> int`
      Inspect a domain's learned pattern/examples; add an email to the suppression list; launch the local web UI.

### emailfinder/web.py
_Responsibility:_ Minimal localhost web UI over the SAME core using ONLY stdlib http.server (zero web-framework dependency, fully offline, no CDN). Single inline SPA + JSON endpoints. Single-lookup card with provider badge + confidence bar + status chip with VISIBLE caps, a 'why this guess' popover fed by ScoredCandidate.reasons + provenance, CSV upload -> results table -> enriched export, bounce/confirm feedback, and the public opt-out endpoint. Binds 127.0.0.1 only.
_Depends on:_ emailfinder/engine.py, emailfinder/batch.py, emailfinder/rescore.py, emailfinder/compliance.py
Functions:
  - `def create_handler(engine: Engine) -> type[BaseHTTPRequestHandler]`
      Build a request handler bound to one Engine; serves the inline single-page HTML/CSS/JS (no external URLs) and the JSON endpoints.
  - `def serve(engine: Engine, host: str = '127.0.0.1', port: int = 8765) -> None`
      Run the local server on loopback only.
  - `# routes: POST /api/find, POST /api/batch (multipart CSV), GET /api/export, POST /api/rescore, POST /api/feedback, POST /api/optout (public, 204), GET /api/kb/<domain>`
      Thin JSON wrappers calling engine.find / batch.run_batch / rescore / engine.confirm / compliance.add_suppression; every response carries the reasons[] trail and the honest M365/catch-all cap note.

### emailfinder/__main__.py
_Responsibility:_ Enables `python -m emailfinder ...` by delegating to cli.main.
_Depends on:_ emailfinder/cli.py
Functions:
  - `if __name__ == '__main__': raise SystemExit(cli.main())`
      Module entry point.
