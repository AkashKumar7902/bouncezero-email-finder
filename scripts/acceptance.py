#!/usr/bin/env python3
"""End-to-end acceptance checks for the safety invariants (offline, no SMTP/API).

Run:  .venv/bin/python scripts/acceptance.py
Exits non-zero if any invariant is violated. Complements the pytest unit suite
with a few real, cross-module assertions driven through the public Engine.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emailfinder.config import load_config          # noqa: E402
from emailfinder.engine import Engine               # noqa: E402
from emailfinder.models import Status               # noqa: E402
from emailfinder import rescore as rescore_mod      # noqa: E402

# The shipped package carries NO real audit data. These checks run against a
# SYNTHETIC fixture KB / bounce CSV (fake domains + names) so no PII is needed.
FIXTURES = ROOT / "tests" / "fixtures"
SEED = FIXTURES / "sample_kb.json"
AUDIT = FIXTURES / "sample_bounces.csv"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def main() -> int:
    from emailfinder import kb_store, provider as provmod
    from emailfinder.models import DomainFingerprint, Provider

    kb = kb_store._decode(json.loads(SEED.read_text()))
    tmp = tempfile.mkdtemp(prefix="ef-accept-")
    eng = Engine(load_config(overrides={"data_dir": tmp, "user_id": "accept"}))

    # Inject the synthetic KB + pre-seed the domain cache so find() runs fully
    # OFFLINE against fake domains (no live DNS, no real data).
    eng.kb.update(kb)
    for dom, e in kb.items():
        eng.cache.put_domain(DomainFingerprint(
            domain=dom, provider=Provider(e["provider"]), mx=e.get("mx", []),
            is_catch_all=None, flags={"is_implicit": False, "dns_error": None},
        ))

    # --- Invariant 1: Microsoft 365 domains are NEVER DELIVERABLE ----------- #
    m365 = [d for d, e in kb.items() if e.get("provider") == "microsoft365"]
    bad = []
    for dom in m365:
        r = eng.find("Test Candidate", dom)
        for sc in [r.best, *r.alternates]:
            if sc is not None and sc.status == Status.DELIVERABLE:
                bad.append(f"{sc.candidate.local_part}@{dom}")
    check("M365 never DELIVERABLE", not bad,
          f"checked {len(m365)} M365 domain(s); violations={bad}")

    # --- Invariant 2: a known_bad local is forced UNDELIVERABLE ------------- #
    # acme.example seeds 'wrong.guess' as known-bad.
    r = eng.find("Wrong Guess", "acme.example")
    hits = [sc for sc in [r.best, *r.alternates]
            if sc and sc.candidate.local_part == "wrong.guess"]
    ok = bool(hits) and all(sc.status == Status.UNDELIVERABLE for sc in hits)
    check("known_bad local forced UNDELIVERABLE", ok,
          "wrong.guess@acme.example" + ("" if ok else " NOT undeliverable"))

    # --- Invariant 3: South-Indian first+initial produces a candidate ------ #
    r = eng.find("Ashwath S", "acme.example")
    ok = r.best is not None and r.best.candidate.local_part == "ashwath.s"
    check("first+initial generates a candidate", ok, (r.best_email() or "None"))

    # --- Invariant 4: provider classification matches the KB mx ------------ #
    mism = []
    for dom, e in kb.items():
        want = e.get("provider")
        got = provmod.classify_provider(e.get("mx") or []).value
        if got != want and want != "microsoft365":
            mism.append(f"{dom}: kb={want} got={got}")
    check("provider classification matches mx", not mism, f"differ: {mism}")

    # --- Invariant 5: rescore banks true not-founds, NOT valid mailboxes --- #
    from collections import Counter
    fixes = rescore_mod.rescore_csv(AUDIT, eng, eng._kb_path, apply_kb=False)
    verdicts = Counter(f.verdict for f in fixes)
    check("rescore buckets look right",
          verdicts.get("WRONG_GUESS", 0) >= 3
          and verdicts.get("PROBABLE_INVALID_M365", 0) >= 1,
          dict(verdicts))
    banked_transient = [f for f in fixes
                        if f.action == "bank_known_bad" and f.verdict == "TRANSIENT"]
    check("no transient row banked as known-bad", not banked_transient,
          f"{len(banked_transient)} bad banks")

    eng.close()

    # --- Invariant 6: web page ships no external URLs ---------------------- #
    import re as _re
    from emailfinder.web import _PAGE
    ext = [u for u in _re.findall(r"https?://[^\s\"')]+", _PAGE)
           if "127.0.0.1" not in u and "localhost" not in u and "w3.org" not in u]
    check("web page has no external URLs", not ext, f"{ext[:3]}")

    print()
    if failures:
        print(f"ACCEPTANCE FAILED: {len(failures)} invariant(s) violated: {failures}")
        return 1
    print("ACCEPTANCE PASSED: all safety invariants hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
