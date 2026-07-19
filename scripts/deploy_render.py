#!/usr/bin/env python3
"""One-shot deploy of the BounceZero web app to Render (app + free Postgres).

You only need a Render account + one API key. Everything else is automated:
  1. find your Render owner id
  2. create a free Render Postgres, wait until it's ready, read its URL
  3. create the free web service from the (public) GitHub repo, wiring
     DATABASE_URL + build/start commands
  4. poll the first deploy, then hit /healthz on the live URL

Usage:
  RENDER_API_KEY=rnd_xxx ./.venv/bin/python scripts/deploy_render.py \
      --repo https://github.com/AkashKumar7902/bouncezero-email-finder \
      --region oregon

The repo must be PUBLIC (so Render's API can pull it with no GitHub OAuth), or
your GitHub already connected to Render. Re-running is safe-ish: it errors if the
names already exist — pass --suffix to get fresh names.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.render.com/v1"


def _req(method: str, path: str, key: str, body: dict | None = None) -> tuple[int, object]:
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def _die(msg: str, detail=None) -> "None":
    print(f"\nERROR: {msg}")
    if detail is not None:
        print(json.dumps(detail, indent=2) if isinstance(detail, (dict, list)) else detail)
    sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="public GitHub repo URL")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--name", default="bouncezero-web")
    ap.add_argument("--db-name", default="bouncezero-db")
    ap.add_argument("--region", default="oregon")
    ap.add_argument("--suffix", default="", help="append to names to avoid clashes")
    args = ap.parse_args()

    key = os.environ.get("RENDER_API_KEY")
    if not key:
        _die("set RENDER_API_KEY (Render dashboard -> Account Settings -> API Keys)")

    name = args.name + args.suffix
    db_name = args.db_name + args.suffix

    # 1) owner id
    st, owners = _req("GET", "/owners?limit=1", key)
    if st != 200 or not owners:
        _die("could not read your Render owner id (bad API key?)", owners)
    owner_id = owners[0]["owner"]["id"]
    print(f"owner: {owners[0]['owner'].get('name','?')} ({owner_id})")

    # 2) free Postgres
    print(f"creating free Postgres '{db_name}' in {args.region} ...")
    st, db = _req("POST", "/postgres", key, {
        "ownerId": owner_id, "name": db_name, "plan": "free",
        "region": args.region, "version": "16",
    })
    if st not in (200, 201):
        _die("failed to create Postgres", db)
    db_id = db["id"]
    print(f"  postgres id: {db_id} — waiting for it to become available ...")
    for _ in range(60):
        st, d = _req("GET", f"/postgres/{db_id}", key)
        status = (d or {}).get("status")
        if status == "available":
            break
        print(f"    status: {status} ...")
        time.sleep(10)
    st, conn = _req("GET", f"/postgres/{db_id}/connection-info", key)
    if st != 200:
        _die("could not read DB connection info", conn)
    db_url = conn.get("internalConnectionString") or conn.get("externalConnectionString")
    if not db_url:
        _die("no connection string returned", conn)
    print("  Postgres ready.")

    # 3) web service
    print(f"creating free web service '{name}' from {args.repo} ...")
    st, svc = _req("POST", "/services", key, {
        "type": "web_service",
        "name": name,
        "ownerId": owner_id,
        "repo": args.repo,
        "branch": args.branch,
        "autoDeploy": "yes",
        "serviceDetails": {
            "runtime": "python",
            "region": args.region,
            "plan": "free",
            "envSpecificDetails": {
                "buildCommand": 'pip install -e ".[web]"',
                "startCommand": "uvicorn webapp.app:app --host 0.0.0.0 --port $PORT",
            },
            "healthCheckPath": "/healthz",
        },
        "envVars": [
            {"key": "DATABASE_URL", "value": db_url},
            {"key": "PYTHON_VERSION", "value": "3.12.8"},
            {"key": "RATE_LIMIT_PER_MIN", "value": "30"},
            {"key": "RATE_LIMIT_PER_DAY", "value": "300"},
        ],
    })
    if st not in (200, 201):
        _die("failed to create web service", svc)
    svc_obj = svc.get("service", svc)
    svc_id = svc_obj.get("id")
    url = svc_obj.get("serviceDetails", {}).get("url") or svc_obj.get("url")
    print(f"  service id: {svc_id}")
    print(f"  URL (once live): {url}")

    # 4) poll first deploy
    print("waiting for the first deploy (free tier build can take a few minutes) ...")
    for _ in range(90):
        st, deploys = _req("GET", f"/services/{svc_id}/deploys?limit=1", key)
        if st == 200 and deploys:
            dstatus = deploys[0]["deploy"]["status"]
            print(f"    deploy: {dstatus}")
            if dstatus in ("live", "deactivated"):
                break
            if dstatus in ("build_failed", "update_failed", "canceled", "pre_deploy_failed"):
                _die(f"deploy failed with status '{dstatus}' — check Render logs", deploys)
        time.sleep(10)

    print("\nDONE.")
    print(f"Live URL: {url}")
    print(f"Try:  curl {url}/healthz")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
