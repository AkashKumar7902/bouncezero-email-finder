# Deploy the BounceZero web app free (Render + Neon)

This deploys `webapp.app:app` (FastAPI) as a **free**, public, multi-user web app
backed by a **free Neon Postgres**. It is additive to the `emailfinder` package —
nothing under `emailfinder/` or `tests/` changes.

> Free-tier limits on both Render and Neon change over time. The numbers below are
> accurate to the free tiers as of **2026**; **double-check the current limits** in
> each provider's dashboard/pricing page before you rely on them.

## What "hosted" means here (read this first)

- **Verification is OFF.** The hosted app never opens SMTP probes (most cloud
  providers block outbound port 25 anyway). Results are **pattern + provider-aware
  guesses**, not verified deliveries.
- **DNS still works.** Render allows outbound DNS, so the app resolves MX records
  and classifies the provider (e.g. Microsoft 365 / catch-all), which drives the
  honest confidence caps.
- **Shared global state.** The knowledge base, suppression list, and lookup log
  live in Postgres and are **shared across all visitors** (no per-user silos).
  A local file would be wiped — Render's free disk is ephemeral.
- **Cold starts.** Render's free web service **spins down after ~15 minutes of
  inactivity**; the next request wakes it and can take **~30–60 seconds**. Neon's
  free compute also auto-suspends when idle and resumes on the next query (a few
  seconds). So the first request after a quiet period is slow, then it's fast.

---

## 1) Create a free Neon Postgres and copy the pooled connection string

1. Sign up / log in at <https://neon.tech> and create a **new project** (pick the
   region closest to your Render region to cut latency). A default database and
   role are created for you.
2. Open **Connect** (or "Connection Details").
3. Toggle to the **Pooled connection** — the host will contain `-pooler`. Use the
   pooled string for a web service (it handles many short-lived connections).
4. Copy the full URI. It looks like:
   ```
   postgresql://<user>:<password>@<host>-pooler.<region>.aws.neon.tech/<db>?sslmode=require
   ```
   Keep `sslmode=require`. This is your `DATABASE_URL`.

The app auto-creates its tables on startup (`init_schema`), so no manual SQL is
needed. Neon free tier gives you a limited amount of storage/compute — plenty for
this app; confirm the current allowance in the Neon console.

## 2) Push the repo to GitHub

```bash
git add -A
git commit -m "Add hosted web app + deploy config"
git remote add origin git@github.com:<you>/linkedin-email-finder.git   # if not set
git push -u origin main
```

Ensure `render.yaml`, `.env.example`, `pyproject.toml`, and the `webapp/` package
are committed. Never commit a real `.env` or the connection string.

## 3) Create a Render web service from the repo

**Option A — Blueprint (recommended, uses `render.yaml`):**
1. Render dashboard -> **New +** -> **Blueprint**.
2. Connect your GitHub account and pick this repo.
3. Render reads `render.yaml` and proposes the `bouncezero-web` service.
4. Because `DATABASE_URL` has `sync: false`, Render will prompt you to enter it —
   paste the Neon pooled string from step 1. Click **Apply**.

**Option B — Manual web service (no blueprint):**
1. Render dashboard -> **New +** -> **Web Service** -> connect the repo.
2. Settings:
   - **Environment / Runtime:** Python
   - **Build Command:** `pip install -e ".[web]"`
   - **Start Command:** `uvicorn webapp.app:app --host 0.0.0.0 --port $PORT`
   - **Health Check Path:** `/healthz`
   - **Instance Type / Plan:** Free
3. Add environment variables (see step 4).

## 4) Set the environment variables

In the service's **Environment** tab set:

| Key | Value |
| --- | --- |
| `DATABASE_URL` | your Neon **pooled** connection string (step 1) |
| `PYTHON_VERSION` | `3.12.8` (or a current 3.12.x Render supports) |
| `RATE_LIMIT_PER_MIN` | `30` (optional) |
| `RATE_LIMIT_PER_DAY` | `300` (optional) |

If `DATABASE_URL` is missing, the app still boots but uses an **ephemeral
in-memory store** (data lost on restart) and logs a warning — fine for a smoke
test, not for real use.

## 5) Deploy

- Blueprint: **Apply** triggers the first deploy. Manual: **Create Web Service**.
- Watch the deploy logs. On success you get a public URL like
  `https://bouncezero-web.onrender.com`.
- Verify:
  - `GET /healthz` -> `{"ok": true}`
  - open `/` for the lookup page
  - `POST /api/find` with `{"name": "...", "domain": "..."}`.
- `autoDeploy` is on, so pushing to `main` redeploys automatically.

---

## Troubleshooting

- **First request hangs ~30–60s:** normal free-tier cold start (Render spin-up +
  Neon resume). Subsequent requests are fast.
- **DB connection errors:** confirm you used the **pooled** host (`-pooler`) and
  kept `?sslmode=require`; re-copy the string from Neon.
- **Everything "unknown provider":** DNS may be slow/blocked for that domain — the
  app degrades to pattern-only guesses; that's expected without verification.
- **429 responses:** per-IP rate limit hit (in-memory, resets on restart). Tune
  `RATE_LIMIT_PER_MIN` / `RATE_LIMIT_PER_DAY`.
