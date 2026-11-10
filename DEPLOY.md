# Deploying to Heroku

This app runs as one always-on web process on Heroku's **Basic** dyno.

## Why Heroku Basic, and what it costs

| | Heroku (recommended) | DigitalOcean App Platform |
|---|---|---|
| Student offer | $13/month platform credit for 24 months ([heroku.com/github-students](https://www.heroku.com/github-students)); a credit or debit card must be on file | Not confirmed: digitalocean.com/github-students showed no student offer when checked on 2026-11-09 |
| Cheapest always-on plan | Basic dyno, $7/month, "Always on". Eco ($5) "Sleeps after 30 minutes of inactivity", so don't use Eco. | Web service container, $5/month. The free tier covers static sites only. |
| **Your cost after credits** | **$0/month for 24 months** ($7 dyno minus $13 credit). Then $7/month. | Unknown without a confirmed credit |

Notes on cost:
- Unused credit doesn't roll over, and your card is charged for anything above $13 in a month. One Basic dyno with no add-ons stays under that.
- Claude API usage is billed separately by Anthropic. `RATE_LIMIT_GLOBAL_PER_MINUTE` caps how fast it can be spent.
- Run exactly **one** dyno. Rate limits live in memory, so a second dyno would double them.

## What's in the repo for Heroku

| File | Purpose |
|---|---|
| `Procfile` | `web: uvicorn web.app:app --host 0.0.0.0 --port $PORT --workers 1 ...`. Heroku sets `$PORT`. |
| `.python-version` | `3.12`, the version the tests run on. Heroku reads this file; `runtime.txt` is deprecated. |
| `requirements.txt` | Runtime packages only. Heroku detects a Python app from this file. Dev and test tools are in `requirements-dev.txt`. |
| `.slugignore` | Leaves tests, build scripts and reports out of the deployed bundle |

## Step by step

Steps marked † use button wording that Heroku's docs don't spell out. If a label looks slightly different, look for the closest match on that tab.

### 1. Claim the student credit (once)

1. Go to https://www.heroku.com/github-students and click **Get the student offer**.
2. Sign in to Heroku, or create an account, with the GitHub account that has the Student Developer Pack. You must be 18 or older.
3. Add a card: click your avatar (top right) → **Account settings** → **Billing** tab (https://dashboard.heroku.com/account/billing) → add a credit or debit card.
4. Back on the offer page, **Apply** for the Heroku for GitHub Students offer.
5. Check that the credit appears under **Billing Information** on the Billing tab. Don't continue until it does: without it, the dyno is billed to your card.

### 2. Create the app

1. Go to https://dashboard.heroku.com.
2. Click **New** → **Create new app** †.
3. App name: up to 30 lowercase letters, numbers and dashes, for example `hawaii-district-profiles`. This becomes your URL (`https://<name>-<random>.herokuapp.com` or similar; the dashboard shows the exact URL later).
4. Region: **United States** †.
5. Click **Create app** †. Make it under your personal account, not a Team: the student credit doesn't apply to Team accounts.

### 3. Connect the GitHub repo

The repo is `HAIC-2026/omerta`, which belongs to a GitHub organization. Heroku requires that:
- your GitHub account is a **member** of HAIC-2026, not an outside collaborator, and has **admin** access to the repo;
- if the organization restricts third-party apps, an organization owner approves Heroku. The owner does this on GitHub under the organization's **Settings → Third-party access**, by granting access to the "Heroku Dashboard" app.

Then:
1. Open your app → **Deploy** tab.
2. Under **Deployment method**, click **GitHub** †, then **Connect to GitHub** †. Authorize Heroku in the GitHub pop-up (needed once per Heroku account).
3. In the repo search, choose the **HAIC-2026** owner, type `omerta`, click **Search** †, then **Connect** † next to it.

### 4. Paste the API key and set the config (before the first deploy)

1. Open the **Settings** tab.
2. In **Config Vars**, click **Reveal Config Vars** †.
3. For each row below, type the name in **KEY**, the value in **VALUE**, and click **Add** †:

| KEY | VALUE |
|---|---|
| `ANTHROPIC_API_KEY` | your key from https://console.anthropic.com → **API Keys** (starts with `sk-ant-`). Paste it only here, never in the repo or a chat. |
| `TRUST_PROXY` | `1` (required on Heroku, so rate limits apply per visitor, not to Heroku's router) |
| `RATE_LIMIT_PER_MINUTE` | `30` (everyone on a venue's wifi shares one IP) |
| `RATE_LIMIT_PER_DAY` | `600` |
| `RATE_LIMIT_GLOBAL_PER_DAY` | `500` (whole-site daily cap on questions; the cap on API spend) |
| `RATE_LIMIT_GLOBAL_PER_MINUTE` | `60` |
| `CHAT_MODEL` | `claude-sonnet-5-5` |

Leave everything else unset; the defaults are in the README's Configuration table. Leave `ALLOWED_ORIGINS` unset too: the UI is served from the same site, so it doesn't need CORS. Changing a config var restarts the app automatically.

### 5. Deploy

1. **Deploy** tab → **Manual deploy** † → branch `main` → **Deploy Branch** †.
2. Watch the build log. You should see `Python app detected`, `Using Python 3.12 specified in .python-version`, `Installing dependencies using 'pip install -r requirements.txt'` and `Procfile declares types -> web`.
3. Optional: under **Automatic deploys** †, enable automatic deploys from `main`, so every push redeploys. Tick **Wait for CI to pass before deploy** only if CI is set up; there is none yet.

### 6. Make sure it never sleeps (Basic dyno, one of it)

1. **Resources** tab.
2. Above the dyno list, click **Change Dyno Type**.
3. Click **Basic**, then **Save**.
4. Check that the `web` process shows **1** dyno. If it shows 0, click the edit (pencil) icon next to it, set 1, and click **Confirm**.

New apps default to Eco if your account has an Eco subscription, and Basic otherwise, so do this step either way.

### 7. Check it

Click **Open app** † (top right), then visit:

| URL | Expect |
|---|---|
| `/healthz` | `{"ok":true}` |
| `/readyz` | `"ready": true`, `"chat_available": true`, `acs_districts: 76`, `legislators_serving: 76`, `trust_proxy: true`, and the rate limits you set |
| `/readyz?deep=1` | adds `"ai_service": "ok"`; this confirms the API key works and uses no tokens |
| `/`, `/map`, `/about`, `/district/senate/9/profile` | pages load |

Then ask one question in the chat, for example the "Which district does Stanley Chang represent?" chip.

## If something goes wrong

- **Logs:** app → **More** † (top right) → **View logs**. Each `/chat` request writes one JSON line (`"event":"chat"`) with its `request_id` (also sent as the `X-Request-ID` response header), status, latency, tool calls and token counts. Questions and answers are never logged.
- **"Chat is temporarily unavailable":** the key is missing or wrong, or the Anthropic account is out of credit. Check `ANTHROPIC_API_KEY` under Settings → Config Vars, then check `/readyz?deep=1`.
- **Everyone gets "Too many questions":** `TRUST_PROXY` isn't `1`, so all visitors look like one IP (the router's). Or raise `RATE_LIMIT_PER_MINUTE`.
- **Build fails on Python:** `.python-version` must name a version Heroku supports. 3.12 is supported as of November 2026.
- **Roll back:** **Activity** tab → pick an earlier release → **Roll back to here** †.
