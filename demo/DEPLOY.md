# Deploying the public demo (free: Render + Neon + Gemini free tier)

Everything in the repo is ready (`render.yaml`, `docker/demo-start.sh`, `.github/workflows/demo-reset.yml`).
What's left needs your own accounts. About 20 minutes, most of it waiting for the first build.

## 0. Push `main` to GitHub
Render deploys from the GitHub repo (`invoice-extraction-pipeline`), so the demo code has to be there first.
CI then boots the demo on a fresh database and replays every recorded invoice: wait for it to go green.

## 1. A Gemini key just for the demo
1. Open Google AI Studio → **Get API key** → **Create API key** → choose **Create a new project** (call it
   something like `invoice-demo`). Don't reuse the support demo's project.
2. Why a new project: free-tier quota is per project. The demo then has its own quota, so visitors can never use
   up the quota you need for eval runs or the other demos, and vice versa.
3. Check that **billing is OFF** on that project (AI Studio shows "Free tier"). With billing off, the worst a
   visitor can do is use up the day's free quota; it can never cost money. Free-tier inputs may be used by
   Google to improve its models: the page tells visitors not to upload real invoices.

## 2. Neon (the database)
1. neon.tech → **New project** (a second project next to the support demo's; the free plan allows several).
   Only Postgres (no Auth / Data API), **Postgres 16** (what the tests and CI run), region
   **AWS US East (N. Virginia)**: `render.yaml` puts the app in Render's `virginia` region, next to it.
2. Project dashboard → **Connect** → turn **Connection pooling OFF** → copy the connection string
   (`postgresql://...neon.tech/neondb?sslmode=require...`). Use the direct one, not the pooled one: psycopg 3
   uses prepared statements, which the pooler can break. Paste it as it is: the app switches
   `postgresql://`/`postgres://` to its own driver.

## 3. Render (the app)
1. render.com (signed in with GitHub) → **New → Blueprint** → the `invoice-extraction-pipeline` repo, branch
   `main`, blueprint path `render.yaml`. It shows one free web service, `invoice-extraction-demo`, in Virginia.
2. It asks for the two secret values: `DATABASE_URL` = the Neon string, `GEMINI_API_KEY` = the demo key.
   `DEMO_MODE`, `MAX_UPLOAD_MB` and `DEMO_RESET_TOKEN` fill themselves in.
3. **Apply**. The first build takes ~5-10 minutes. The first start creates the tables in Neon by itself. When
   the service shows **Live**, open its URL (`https://invoice-extraction-demo.onrender.com` if that name was
   free; if Render added a suffix, tell me and I'll fix the README link).

## 4. The nightly reset (GitHub Actions)
1. Render → the service → **Environment** → copy the value of `DEMO_RESET_TOKEN`.
2. GitHub repo → **Settings → Secrets and variables → Actions**:
   - **Secrets** tab → New secret `DEMO_RESET_TOKEN` = that value.
   - **Variables** tab → New variable `DEMO_URL` = the Render URL, without a trailing slash.
3. **Actions** tab → **demo-reset** → **Run workflow** once, to check it goes green.

## 5. Check it
```powershell
python demo/check_deploy.py https://invoice-extraction-demo.onrender.com
```
Stdlib only, spends no live quota: the page, the guards, and every recorded invoice must end where the eval
scored it. Or send me the URL and I'll check it, plus one live extraction.

## Good to know
- **Free instances sleep** after about 15 minutes with no visitors. The next visit wakes it in about a minute.
  Open the link yourself a minute before you share it on a call.
- **The disk is wiped on every restart/sleep.** Extractions stay (they're in Neon) but an uploaded or generated
  document disappears from the page ("no longer stored"). Recorded invoices ship with the app and always work.
  An extraction a restart interrupted ends `extraction_failed` (the visitor can retry) instead of being re-run.
- **Every push to `main` redeploys** (`autoDeploy: true`).
- **Live caps**: 20 live extractions a day for everyone, 3 per visitor per hour (`DEMO_LIVE_DAILY_LIMIT`,
  `DEMO_LIVE_PER_IP_PER_HOUR` on Render). The per-visitor cap trusts `X-Forwarded-For`, which a determined
  visitor can fake; the daily cap is what protects the quota.
- **Scheduled workflows pause** after 60 days without commits. GitHub emails you; re-enable it in the Actions
  tab.
