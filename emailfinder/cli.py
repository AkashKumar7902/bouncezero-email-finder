"""Scriptable command-line surface over the frozen find/verify core.

A thin argparse wrapper — it holds NO business logic of its own; every decision
(scoring, caps, compliance, KB) lives in the modules :class:`~emailfinder.engine.Engine`
orchestrates. The console-scripts entry point is ``emailfinder = emailfinder.cli:main``
(and ``python -m emailfinder`` routes here too).

Subcommands: ``find`` / ``batch`` / ``rescore`` / ``kb`` / ``optout`` / ``purge`` /
``web``.

Global flags (usable before *or* after the subcommand): ``--json`` (machine
output), ``--user <id>``, ``--config <path>``, ``--data-dir <path>``.

Safety posture surfaced by every command's output (research dossier 4.3 / 5 /
8.1):
  * SMTP probing and paid providers are OFF unless ``--verify`` / ``--providers``
    are explicitly passed;
  * Microsoft 365 and catch-all domains are labelled **unverifiable** with the
    honest cap note and are NEVER printed as DELIVERABLE;
  * a suppressed identity prints an opt-out notice with no address.

Exit codes: ``0`` success, ``1`` usage error, ``2`` no candidate / degraded
(suppressed, no best candidate, or an undeliverable-only result).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from . import kb_store, ranking
from .config import load_config
from .engine import Engine
from .models import FindResult, MXInfo, Provider, ScoredCandidate, Status

# Exit codes (documented in the module docstring / contract).
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NO_CANDIDATE = 2

# Human-friendly provider badges (falls back to the raw enum value).
_PROVIDER_BADGE = {
    Provider.MICROSOFT365: "Microsoft 365",
    Provider.GOOGLE_WORKSPACE: "Google Workspace",
    Provider.CONSUMER_GMAIL: "Gmail (consumer)",
    Provider.PROOFPOINT: "Proofpoint",
    Provider.MIMECAST: "Mimecast",
    Provider.CISCO_IRONPORT: "Cisco IronPort",
    Provider.BARRACUDA: "Barracuda",
    Provider.ZOHO: "Zoho",
    Provider.AMAZON_SES: "Amazon SES",
    Provider.YAHOO_AOL: "Yahoo/AOL",
    Provider.OTHER: "Other",
    Provider.NONE_UNKNOWN: "Unknown",
}


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    """Assemble the top-level parser and every subcommand parser.

    The four global flags live on a shared parent parser (with
    ``default=SUPPRESS``) that is attached to every subparser, and also directly
    on the root parser with real defaults. That combination lets a global flag be
    given either before or after the subcommand without one position clobbering
    the other.
    """
    # Global flags, defined twice: once on the root with real defaults, once on a
    # SUPPRESS-default parent so a post-subcommand occurrence only *adds*.
    globals_parent = argparse.ArgumentParser(add_help=False)
    _add_global_flags(globals_parent, suppress=True)

    parser = argparse.ArgumentParser(
        prog="emailfinder",
        description="BounceZero — offline-first, provider-aware email finder + "
        "verifier. SMTP probing and paid providers are OFF unless explicitly "
        "enabled with --verify / --providers.",
    )
    _add_global_flags(parser, suppress=False)

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # -- find ------------------------------------------------------------- #
    p_find = sub.add_parser(
        "find",
        parents=[globals_parent],
        help="guess + (optionally) verify one person's work email",
    )
    p_find.add_argument("name", nargs="?", help='full name, e.g. "Ajith Kumar"')
    p_find.add_argument("--domain", help="the company mail domain, e.g. acme.com")
    p_find.add_argument("--company", help="company name (best-effort domain guess)")
    p_find.add_argument(
        "--linkedin",
        dest="linkedin_url",
        help="a linkedin.com/in/<slug> URL — slug-parsed LOCALLY, never fetched",
    )
    p_find.add_argument("--first", help="explicit first name")
    p_find.add_argument("--last", help="explicit last name")
    _add_optin_flags(p_find)
    p_find.set_defaults(func=cmd_find)

    # -- batch ------------------------------------------------------------ #
    p_batch = sub.add_parser(
        "batch",
        parents=[globals_parent],
        help="enrich a CSV of people into a mail-merge-ready CSV",
    )
    p_batch.add_argument("in_csv", help="input CSV path")
    p_batch.add_argument("-o", "--out", required=True, help="output (enriched) CSV path")
    p_batch.add_argument(
        "--map",
        dest="map",
        help="rename source headers, e.g. name=Name,domain=Domain",
    )
    _add_optin_flags(p_batch)
    p_batch.set_defaults(func=cmd_batch)

    # -- rescore ---------------------------------------------------------- #
    p_rescore = sub.add_parser(
        "rescore",
        parents=[globals_parent],
        help="re-score a bounced list / DSN mailbox and emit a fix-list",
    )
    p_rescore.add_argument(
        "bounced_csv", nargs="?", help="bounced/audit CSV path (records.csv columns)"
    )
    p_rescore.add_argument("--mbox", help="a DSN mbox/Maildir path instead of a CSV")
    p_rescore.add_argument("-o", "--out", help="write the fix-list CSV here")
    p_rescore.add_argument(
        "--apply-kb",
        dest="apply_kb",
        action="store_true",
        help="persist learned known-bad locals + patterns to the per-user KB",
    )
    p_rescore.set_defaults(func=cmd_rescore)

    # -- kb --------------------------------------------------------------- #
    p_kb = sub.add_parser(
        "kb",
        parents=[globals_parent],
        help="inspect a domain's learned pattern / examples",
    )
    p_kb.add_argument("domain", help="the domain to inspect, e.g. trimble.com")
    p_kb.set_defaults(func=cmd_kb)

    # -- optout ----------------------------------------------------------- #
    p_optout = sub.add_parser(
        "optout",
        parents=[globals_parent],
        help="add an address to the global suppression list",
    )
    p_optout.add_argument("email", help="the address to suppress")
    p_optout.set_defaults(func=cmd_optout)

    # -- purge ------------------------------------------------------------ #
    p_purge = sub.add_parser(
        "purge",
        parents=[globals_parent],
        help="delete provenance rows older than the retention window",
    )
    p_purge.add_argument(
        "--days",
        type=int,
        default=None,
        help="retention window in days (default: config retention_days)",
    )
    p_purge.set_defaults(func=cmd_purge)

    # -- web -------------------------------------------------------------- #
    p_web = sub.add_parser(
        "web",
        parents=[globals_parent],
        help="launch the minimal local web UI (loopback only)",
    )
    p_web.add_argument("--host", default="127.0.0.1", help="bind host (loopback only)")
    p_web.add_argument("--port", type=int, default=8765, help="bind port")
    p_web.set_defaults(func=cmd_web)

    return parser


def _add_global_flags(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Attach the four global flags; ``suppress`` picks the default behaviour."""
    default_flag = argparse.SUPPRESS if suppress else False
    default_val = argparse.SUPPRESS if suppress else None
    parser.add_argument(
        "--json",
        dest="json",
        action="store_true",
        default=default_flag,
        help="emit machine-readable JSON",
    )
    parser.add_argument("--user", dest="user", default=default_val, help="per-user silo id")
    parser.add_argument(
        "--config", dest="config", default=default_val, help="path to a config.json"
    )
    parser.add_argument(
        "--data-dir",
        dest="data_dir",
        default=default_val,
        help="override the data/silo directory",
    )


def _add_optin_flags(parser: argparse.ArgumentParser) -> None:
    """Add the opt-in ``--verify`` (SMTP) and ``--providers`` flags (both OFF)."""
    parser.add_argument(
        "--verify",
        "--smtp",
        dest="verify",
        action="store_true",
        help="opt in to SMTP RCPT verification (off by default)",
    )
    parser.add_argument(
        "--providers",
        dest="use_providers",
        action="store_true",
        help="opt in to paid provider lookups (off by default)",
    )


# --------------------------------------------------------------------------- #
# engine construction
# --------------------------------------------------------------------------- #
def _build_engine(args) -> Engine:
    """Build an Engine from the parsed global flags + any opt-in feature flags."""
    overrides: dict = {}
    if getattr(args, "user", None):
        overrides["user_id"] = args.user
    if getattr(args, "data_dir", None):
        overrides["data_dir"] = str(args.data_dir)
    if getattr(args, "verify", False):
        overrides["enable_smtp"] = True
    if getattr(args, "use_providers", False):
        overrides["enable_providers"] = True

    config_path = getattr(args, "config", None)
    cfg = load_config(
        path=Path(config_path) if config_path else None,
        overrides=overrides or None,
    )
    return Engine(cfg)


# --------------------------------------------------------------------------- #
# JSON serialization helpers
# --------------------------------------------------------------------------- #
def _json_default(obj):
    """``json.dumps`` fallback for enums / dataclasses / Paths."""
    from enum import Enum

    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, set):
        return sorted(obj)
    return str(obj)


def _dumps(obj) -> str:
    return json.dumps(obj, indent=2, default=_json_default, ensure_ascii=False)


def _cap_note(result: FindResult, sc: ScoredCandidate | None) -> str | None:
    """Return the honest unverifiable cap note for M365 / catch-all, else None.

    These are the ONLY two states the contract requires to be labelled
    unverifiable; scoring already guarantees neither can be DELIVERABLE, so this
    is purely the user-facing explanation of the cap.
    """
    if sc is not None and sc.is_catch_all:
        return "catch-all: pattern-only (unverifiable)"
    if result.provider == Provider.MICROSOFT365:
        return "capped: Microsoft 365 not RCPT-verifiable (unverifiable)"
    return None


def _scored_to_dict(sc: ScoredCandidate, domain: str | None) -> dict:
    local = sc.candidate.local_part
    return {
        "email": f"{local}@{domain}" if domain else local,
        "local_part": local,
        "template": sc.candidate.template,
        "separator": sc.candidate.separator,
        "shape": sc.candidate.shape,
        "source": sc.candidate.source,
        "name_origin": sc.candidate.name_origin,
        "score": sc.score,
        "status": sc.status.value,
        "is_catch_all": sc.is_catch_all,
        "is_role": sc.is_role,
        "is_disposable": sc.is_disposable,
        "webmail": sc.webmail,
        "reasons": list(sc.reasons),
    }


def _find_to_dict(result: FindResult) -> dict:
    """Serialize a FindResult (incl. per-candidate reasons + the cap note)."""
    domain = result.domain
    best = None
    if result.best is not None:
        best = _scored_to_dict(result.best, domain)
        note = _cap_note(result, result.best)
        if note:
            best["cap_note"] = note
    return {
        "query": result.query,
        "domain": domain,
        "provider": result.provider.value,
        "provider_badge": _PROVIDER_BADGE.get(result.provider, result.provider.value),
        "strategy": result.strategy.value,
        "suppressed": result.suppressed,
        "verification_mode": result.verification_mode,
        "provenance_id": result.provenance_id,
        "mx": _mx_to_dict(result.mx),
        "best": best,
        "alternates": [_scored_to_dict(a, domain) for a in result.alternates],
        "notes": list(result.notes),
    }


def _mx_to_dict(mx: MXInfo | None) -> dict | None:
    if mx is None:
        return None
    return {
        "domain": mx.domain,
        "hosts": list(mx.hosts),
        "is_implicit": mx.is_implicit,
        "error": mx.error,
    }


def _status_chip(status: Status) -> str:
    return f"[{status.value.upper()}]"


# --------------------------------------------------------------------------- #
# subcommand: find
# --------------------------------------------------------------------------- #
def cmd_find(args) -> int:
    """Single lookup: print the chosen ScoredCandidate + alternates + reasons."""
    if not (args.name or args.first or args.last or args.linkedin_url):
        _err("find: provide a NAME, --first/--last, or --linkedin URL")
        return EXIT_USAGE
    if not (args.domain or args.company or args.linkedin_url):
        _err("find: provide --domain, --company, or --linkedin")
        return EXIT_USAGE

    engine = _build_engine(args)
    try:
        result = engine.find(
            args.name,
            args.domain,
            first=args.first,
            last=args.last,
            company=args.company,
            linkedin_url=args.linkedin_url,
            verify=args.verify,
            use_providers=args.use_providers,
        )
    finally:
        engine.close()

    if args.json:
        print(_dumps(_find_to_dict(result)))
    else:
        _render_find_human(result)

    if result.suppressed or result.best is None:
        return EXIT_NO_CANDIDATE
    if result.best.status == Status.UNDELIVERABLE:
        return EXIT_NO_CANDIDATE
    return EXIT_OK


def _render_find_human(result: FindResult) -> None:
    """Human-readable single-lookup rendering."""
    if result.suppressed:
        print("SUPPRESSED — this identity is on the global opt-out list.")
        print("No address is returned (Art. 21 opt-out honored).")
        return

    if result.best is None:
        print("No candidate could be produced.")
        for note in result.notes:
            print(f"  note: {note}")
        return

    domain = result.domain
    best = result.best
    email = f"{best.candidate.local_part}@{domain}"
    badge = _PROVIDER_BADGE.get(result.provider, result.provider.value)

    print(f"{email}")
    print(
        f"  {_status_chip(best.status)}  confidence {best.score}/100  "
        f"provider: {badge}"
    )
    note = _cap_note(result, best)
    if note:
        print(f"  ! {note}")

    flags = _flag_summary(best)
    if flags:
        print(f"  flags: {flags}")

    if result.alternates:
        print("\n  alternates:")
        for alt in result.alternates:
            alt_email = f"{alt.candidate.local_part}@{domain}"
            alt_note = _cap_note(result, alt)
            suffix = f"  ! {alt_note}" if alt_note else ""
            print(
                f"    {alt_email}  {_status_chip(alt.status)} "
                f"{alt.score}/100{suffix}"
            )

    print("\n  why this guess:")
    for reason in best.reasons:
        print(f"    - {reason}")

    for extra in result.notes:
        print(f"  note: {extra}")


def _flag_summary(sc: ScoredCandidate) -> str:
    parts = []
    if sc.is_role:
        parts.append("role")
    if sc.is_disposable:
        parts.append("disposable")
    if sc.webmail:
        parts.append("webmail")
    if sc.is_catch_all:
        parts.append("catch-all")
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
# subcommand: batch
# --------------------------------------------------------------------------- #
def cmd_batch(args) -> int:
    """Enrich an input CSV into the mail-merge-ready output CSV."""
    from . import batch  # lazy: keeps cli importable if batch is absent

    in_csv = Path(args.in_csv)
    if not in_csv.exists():
        _err(f"batch: input CSV not found: {in_csv}")
        return EXIT_USAGE
    mapping = _parse_map(args.map)
    if args.map is not None and mapping is None:
        _err("batch: --map must be key=value[,key=value...]")
        return EXIT_USAGE

    engine = _build_engine(args)
    out_csv = Path(args.out)
    try:
        stats = batch.run_batch(
            engine,
            in_csv,
            out_csv,
            mapping=mapping,
            verify=args.verify,
            use_providers=args.use_providers,
        )
    finally:
        engine.close()

    stats_dict = _stats_to_dict(stats)
    if args.json:
        print(_dumps({"out": str(out_csv), "stats": stats_dict}))
    else:
        print(f"wrote {out_csv}")
        _render_stats_human(stats_dict)
    return EXIT_OK


def _parse_map(raw: str | None) -> dict[str, str] | None:
    """Parse ``a=b,c=d`` into a dict; None on a malformed spec (empty -> {})."""
    if raw is None:
        return None
    mapping: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            return None
        key, _, value = pair.partition("=")
        key, value = key.strip(), value.strip()
        if not key or not value:
            return None
        mapping[key] = value
    return mapping


def _stats_to_dict(stats) -> dict:
    """Best-effort dict view of a BatchStats (dataclass or plain object)."""
    if stats is None:
        return {}
    if isinstance(stats, dict):
        return stats
    if dataclasses.is_dataclass(stats) and not isinstance(stats, type):
        return dataclasses.asdict(stats)
    try:
        return dict(vars(stats))
    except TypeError:
        return {"stats": str(stats)}


def _render_stats_human(stats_dict: dict) -> None:
    print("summary:")
    for key, value in stats_dict.items():
        print(f"  {key}: {value}")


# --------------------------------------------------------------------------- #
# subcommand: rescore
# --------------------------------------------------------------------------- #
def cmd_rescore(args) -> int:
    """Re-score a bounced CSV / DSN mailbox and emit the per-address fix-list."""
    from . import rescore  # lazy import

    if not args.bounced_csv and not args.mbox:
        _err("rescore: provide a bounced CSV path or --mbox <path>")
        return EXIT_USAGE

    engine = _build_engine(args)
    kb_path = engine._kb_path
    try:
        if args.mbox:
            source = Path(args.mbox)
            if not source.exists():
                _err(f"rescore: mailbox not found: {source}")
                return EXIT_USAGE
            items = rescore.rescore_mailbox(
                source, engine, kb_path, apply_kb=args.apply_kb
            )
        else:
            source = Path(args.bounced_csv)
            if not source.exists():
                _err(f"rescore: CSV not found: {source}")
                return EXIT_USAGE
            items = rescore.rescore_csv(
                source, engine, kb_path, apply_kb=args.apply_kb
            )
        if args.out:
            rescore.write_fixlist(items, Path(args.out))
    finally:
        engine.close()

    counts = _verdict_counts(items)
    if args.json:
        print(
            _dumps(
                {
                    "counts": counts,
                    "out": str(args.out) if args.out else None,
                    "items": [dataclasses.asdict(i) for i in items],
                }
            )
        )
    else:
        print(f"re-scored {len(items)} address(es)")
        for verdict, n in counts.items():
            print(f"  {verdict}: {n}")
        if args.out:
            print(f"wrote fix-list to {args.out}")
        if args.apply_kb:
            print("KB updated (known-bad locals + patterns banked to the silo)")
    return EXIT_OK


def _verdict_counts(items) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.verdict] = counts.get(item.verdict, 0) + 1
    return dict(sorted(counts.items()))


# --------------------------------------------------------------------------- #
# subcommand: kb
# --------------------------------------------------------------------------- #
def cmd_kb(args) -> int:
    """Inspect a domain's learned dominant template/separator + examples."""
    engine = _build_engine(args)
    try:
        entry = kb_store.get_entry(engine.kb, args.domain)
    finally:
        engine.close()

    if entry is None:
        if args.json:
            print(_dumps({"domain": args.domain, "found": False}))
        else:
            print(f"no KB entry for {args.domain}")
        return EXIT_NO_CANDIDATE

    dominant_shape, share = ranking.dominant_share(entry)
    sep = entry.get("dominant_separator", "")
    sep_display = "(none)" if sep in ("", "(none)") else sep
    examples = list(entry.get("no_bounce_locals", []))[:10]
    known_bad = list(entry.get("known_bad_locals", []))

    if args.json:
        print(
            _dumps(
                {
                    "domain": args.domain,
                    "found": True,
                    "company": entry.get("company"),
                    "provider": entry.get("provider"),
                    "dominant_shape": dominant_shape,
                    "dominant_separator": sep_display,
                    "dominant_share": round(share, 4),
                    "total_addresses": entry.get("total_addresses"),
                    "shape_distribution": entry.get("shape_distribution", {}),
                    "known_bad_locals": known_bad,
                    "verified_examples": examples,
                }
            )
        )
        return EXIT_OK

    print(f"KB entry for {args.domain}")
    print(f"  company:            {entry.get('company')}")
    print(f"  provider:           {entry.get('provider')}")
    print(
        f"  dominant pattern:   {dominant_shape} "
        f"(separator {sep_display}, share {share:.0%})"
    )
    print(f"  total addresses:    {entry.get('total_addresses')}")
    dist = entry.get("shape_distribution", {})
    if dist:
        print("  shape distribution:")
        for shape_label, count in sorted(
            dist.items(), key=lambda kv: kv[1], reverse=True
        ):
            print(f"    {shape_label}: {count}")
    if known_bad:
        print(f"  known-bad locals ({len(known_bad)}): {', '.join(known_bad[:20])}")
    if examples:
        print(f"  verified examples:  {', '.join(examples)}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# subcommand: optout
# --------------------------------------------------------------------------- #
def cmd_optout(args) -> int:
    """Add an address to the global (cross-user) suppression list."""
    email = (args.email or "").strip()
    if "@" not in email:
        _err("optout: provide a valid email address")
        return EXIT_USAGE

    engine = _build_engine(args)
    try:
        engine.compliance.add_suppression(email, None, None, "cli_optout")
    finally:
        engine.close()

    if args.json:
        print(_dumps({"suppressed": email}))
    else:
        print(f"opted out: {email} (added to the global suppression list)")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# subcommand: purge
# --------------------------------------------------------------------------- #
def cmd_purge(args) -> int:
    """Purge per-user provenance rows older than the retention window."""
    engine = _build_engine(args)
    try:
        if args.days is not None:
            engine.compliance.retention_days = int(args.days)
        purged = engine.compliance.purge_expired()
    finally:
        engine.close()

    if args.json:
        print(_dumps({"purged": purged}))
    else:
        print(f"purged {purged} expired provenance record(s)")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# subcommand: web
# --------------------------------------------------------------------------- #
def cmd_web(args) -> int:
    """Launch the minimal stdlib web UI on loopback."""
    from . import web  # lazy import: web is optional at import time

    engine = _build_engine(args)
    print(f"serving BounceZero web UI at http://{args.host}:{args.port}")
    print("press Ctrl-C to stop")
    try:
        web.serve(engine, host=args.host, port=args.port)
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        engine.close()
    return EXIT_OK


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def _err(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and dispatch to the matching subcommand handler.

    Returns the process exit code (0 success, 1 usage error, 2 no candidate /
    degraded). Never raises for an expected failure — usage problems become exit
    code 1 with a message on stderr.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE

    # Normalize possibly-suppressed global flags to concrete defaults.
    if not hasattr(args, "json"):
        args.json = False

    try:
        return args.func(args)
    except BrokenPipeError:  # e.g. piping into `head`
        return EXIT_OK
    except KeyboardInterrupt:
        return EXIT_NO_CANDIDATE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
