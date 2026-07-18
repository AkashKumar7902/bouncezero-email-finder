#!/usr/bin/env python3
"""
Analyze cold_mail_outreach_audit.xlsx into a per-domain email-pattern + provider
knowledge base. Output feeds both the research phase and the runtime product.

Outputs (in this directory):
  - domain_kb.json      per-domain pattern/provider/bounce knowledge base
  - records.csv         cleaned per-recipient records
  - summary.md          human-readable insights
"""
import csv
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict

import openpyxl

SRC = "/Users/akashkumar/Downloads/cold_mail_outreach_audit.xlsx"
OUT = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------- load
wb = openpyxl.load_workbook(SRC, data_only=True)
ra = wb["Recipient Audit"]
rows = list(ra.iter_rows(values_only=True))
header = [str(h) for h in rows[0]]
idx = {h: i for i, h in enumerate(header)}


def cell(row, name):
    i = idx.get(name)
    return row[i] if i is not None and i < len(row) else None


records = []
for row in rows[1:]:
    email = (cell(row, "Recipient Email") or "").strip().lower()
    if not email or "@" not in email:
        continue
    local, _, domain = email.partition("@")
    records.append({
        "company": cell(row, "Company"),
        "email": email,
        "name": cell(row, "Recipient Name"),
        "local": local,
        "domain": domain,
        "category": cell(row, "Outreach Categories"),
        "bounce_status": cell(row, "Bounce Status"),
        "reason_class": cell(row, "Bounce Reason Classes"),
        "smtp": cell(row, "SMTP Statuses"),
        "scope": cell(row, "Failure Scopes"),
        "confidence": cell(row, "Match Confidence"),
    })

print(f"loaded {len(records)} recipient records", file=sys.stderr)

# ----------------------------------------------------------------- local-part shape
def shape(local):
    """Structural classification of an email local part."""
    for sep, label in ((".", "dot"), ("_", "underscore"), ("-", "hyphen")):
        if sep in local:
            toks = local.split(sep)
            if len(toks) == 2:
                a, b = toks
                if len(a) == 1 and b.isalpha():
                    return f"f{sep}last", sep
                if len(b) == 1 and a.isalpha():
                    return f"first{sep}l", sep
                return f"first{sep}last", sep
            return f"multi{sep}", sep
    if local.isalpha():
        return "single_token", ""      # firstlast / flast / first (ambiguous)
    if re.fullmatch(r"[a-z]+\d+", local):
        return "name+digits", ""
    return "other", ""


for r in records:
    r["shape"], r["sep"] = shape(r["local"])

# ------------------------------------------------------------------ MX / provider
def classify_provider(mx_hosts):
    joined = " ".join(mx_hosts).lower()
    if "protection.outlook.com" in joined or "mail.protection.outlook" in joined:
        return "microsoft365"
    if "pphosted.com" in joined or "proofpoint" in joined:
        return "proofpoint"
    if "mimecast" in joined:
        return "mimecast"
    if "google.com" in joined or "googlemail" in joined or "aspmx.l.google" in joined:
        return "google_workspace"
    if "amazonaws" in joined or "amazonses" in joined:
        return "amazon_ses"
    if "barracuda" in joined:
        return "barracuda"
    if "cisco" in joined or "iphmx.com" in joined:
        return "cisco_ironport"
    if "zoho" in joined:
        return "zoho"
    if not mx_hosts:
        return "none_or_unknown"
    return "other"


mx_cache = {}
def lookup_mx(domain):
    if domain in mx_cache:
        return mx_cache[domain]
    try:
        out = subprocess.run(
            ["dig", "+short", "+time=2", "+tries=1", "MX", domain],
            capture_output=True, text=True, timeout=6,
        ).stdout.strip()
        hosts = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                hosts.append(parts[1].rstrip(".").lower())
        mx_cache[domain] = hosts
    except Exception:
        mx_cache[domain] = []
    return mx_cache[domain]

# ----------------------------------------------------------------- per-domain KB
by_domain = defaultdict(list)
for r in records:
    by_domain[r["domain"]].append(r)

domains_sorted = sorted(by_domain, key=lambda d: -len(by_domain[d]))
print(f"{len(domains_sorted)} distinct domains; looking up MX...", file=sys.stderr)

kb = {}
for di, domain in enumerate(domains_sorted):
    recs = by_domain[domain]
    reason = Counter(r["reason_class"] for r in recs if r["reason_class"])
    status = Counter(r["bounce_status"] for r in recs)
    seps = Counter(r["sep"] for r in recs)
    shapes = Counter(r["shape"] for r in recs)

    total = len(recs)
    addr_not_found = reason.get("address_not_found", 0)
    rejected = reason.get("recipient_rejected", 0)
    inactive = reason.get("inactive_account", 0)
    no_bounce = status.get("No bounce found", 0)

    mx = lookup_mx(domain)
    provider = classify_provider(mx)

    dominant_sep = seps.most_common(1)[0][0] if seps else ""
    dominant_shape = shapes.most_common(1)[0][0] if shapes else ""

    kb[domain] = {
        "company": recs[0]["company"],
        "total_addresses": total,
        "provider": provider,
        "mx": mx[:3],
        "dominant_separator": dominant_sep or "(none)",
        "dominant_shape": dominant_shape,
        "shape_distribution": dict(shapes.most_common()),
        "reason_classes": dict(reason),
        "no_bounce_found": no_bounce,
        "address_not_found": addr_not_found,
        "recipient_rejected": rejected,
        "inactive_account": inactive,
        # local parts that PROVABLY do not exist (hard negative training signal)
        "known_bad_locals": sorted({r["local"] for r in recs
                                    if r["reason_class"] == "address_not_found"}),
        # local parts with no bounce (weak-positive: not proof of delivery)
        "no_bounce_locals": sorted({r["local"] for r in recs
                                    if r["bounce_status"] == "No bounce found"})[:50],
    }
    if di % 40 == 0:
        print(f"  ...{di}/{len(domains_sorted)}", file=sys.stderr)

with open(os.path.join(OUT, "domain_kb.json"), "w") as f:
    json.dump(kb, f, indent=1, default=str)

# ------------------------------------------------------------------- records.csv
with open(os.path.join(OUT, "records.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["company", "email", "domain", "local", "shape", "sep",
                "bounce_status", "reason_class"])
    for r in records:
        w.writerow([r["company"], r["email"], r["domain"], r["local"],
                    r["shape"], r["sep"], r["bounce_status"], r["reason_class"]])

# ----------------------------------------------------------------- provider stats
prov_stats = defaultdict(lambda: Counter())
for domain, d in kb.items():
    p = d["provider"]
    prov_stats[p]["domains"] += 1
    prov_stats[p]["addresses"] += d["total_addresses"]
    prov_stats[p]["address_not_found"] += d["address_not_found"]
    prov_stats[p]["recipient_rejected"] += d["recipient_rejected"]
    prov_stats[p]["inactive_account"] += d["inactive_account"]
    prov_stats[p]["no_bounce_found"] += d["no_bounce_found"]

global_shapes = Counter(r["shape"] for r in records)
global_sep = Counter(r["sep"] for r in records)
global_reason = Counter(r["reason_class"] for r in records if r["reason_class"])

# ------------------------------------------------------------------- summary.md
lines = []
lines.append("# Cold-outreach audit — email-pattern & provider knowledge base\n")
lines.append(f"- Recipient records analyzed: **{len(records)}**")
lines.append(f"- Distinct domains: **{len(kb)}**")
lines.append(f"- Distinct companies: **{len({r['company'] for r in records})}**\n")

lines.append("## Global local-part shape distribution")
lines.append("| shape | count | pct |")
lines.append("|---|---|---|")
for sh, c in global_shapes.most_common():
    lines.append(f"| {sh} | {c} | {c*100//len(records)}% |")

lines.append("\n## Global separator usage")
lines.append("| separator | count |")
lines.append("|---|---|")
for s, c in global_sep.most_common():
    lines.append(f"| {s or '(none)'} | {c} |")

lines.append("\n## Bounce reason classes (all bounces)")
lines.append("| reason_class | count |")
lines.append("|---|---|")
for s, c in global_reason.most_common():
    lines.append(f"| {s} | {c} |")

lines.append("\n## Provider behavior (KEY INSIGHT for verification reliability)")
lines.append("Reject/verify behavior differs sharply by mail provider. `recipient_rejected` "
             "(550 5.4.1 Access denied) on Microsoft 365 is a **policy block that fires even for "
             "valid mailboxes**, so an SMTP RCPT probe cannot confirm/deny those addresses. "
             "`address_not_found` (550 5.1.1) is a **true negative** — that mailbox does not exist.\n")
lines.append("| provider | domains | addrs | addr_not_found | recip_rejected | inactive | no_bounce |")
lines.append("|---|---|---|---|---|---|---|")
for p, c in sorted(prov_stats.items(), key=lambda kv: -kv[1]["addresses"]):
    lines.append(f"| {p} | {c['domains']} | {c['addresses']} | {c['address_not_found']} "
                 f"| {c['recipient_rejected']} | {c['inactive_account']} | {c['no_bounce_found']} |")

lines.append("\n## Top 30 domains by volume")
lines.append("| domain | company | addrs | provider | dominant shape | addr_not_found | rejected | no_bounce |")
lines.append("|---|---|---|---|---|---|---|---|")
for domain in domains_sorted[:30]:
    d = kb[domain]
    lines.append(f"| {domain} | {d['company']} | {d['total_addresses']} | {d['provider']} "
                 f"| {d['dominant_shape']} | {d['address_not_found']} | {d['recipient_rejected']} "
                 f"| {d['no_bounce_found']} |")

with open(os.path.join(OUT, "summary.md"), "w") as f:
    f.write("\n".join(lines) + "\n")

print("DONE. wrote domain_kb.json, records.csv, summary.md", file=sys.stderr)
print(json.dumps({
    "records": len(records),
    "domains": len(kb),
    "provider_breakdown": {p: dict(c) for p, c in prov_stats.items()},
    "global_shapes": dict(global_shapes.most_common()),
    "global_reasons": dict(global_reason.most_common()),
}, indent=2))
