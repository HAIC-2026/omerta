# Deployment (Render)

The app runs as one always-on Python web service on Render:

**https://know-your-district.onrender.com**

It deploys automatically from `main` of `HAIC-2026/omerta`. [render.yaml](render.yaml) records the same settings as the dashboard.

## Service settings

| Setting | Value | Where in the dashboard |
|---|---|---|
| Service type | Web Service, Python runtime | |
| Instance type | **Starter** (plan ID `0.5c-512mb`: 0.5 CPU, 512 MB RAM). Always on, so judges never hit a cold start. | Settings → Instance Type |
| Branch | `main` | Settings → Build & Deploy |
| Auto-Deploy | On Commit: every push to `main` deploys | Settings → Build & Deploy |
| Build command | `pip install -r requirements.txt` | Settings → Build & Deploy |
| Start command | `uvicorn web.app:app --host 0.0.0.0 --port $PORT --workers 1 --no-server-header --timeout-graceful-shutdown 20` | Settings → Build & Deploy |
| Health check path | `/healthz` | Settings → Health Checks |
| Python | 3.12, read from `.python-version` in the repo root. Without this file, Render would use its own default for new services (3.14.x as of November 2026). | |

Run exactly **one** instance with **one** worker. Rate limits and conversations are held in memory in one process, so a second instance would double the limits and lose conversations routed to the other one.

## Environment variables and the API key

The Anthropic API key exists in one place only: the service's **Environment** page in the Render dashboard. It's never in the repo, in `render.yaml` (which lists it with `sync: false`), or in any response the app sends.

| Key | Value | Why |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(secret)* | Required for chat. To rotate it, create a new key at https://console.anthropic.com → **API Keys**, paste it here, save, then delete the old key in the console. |
| `TRUST_PROXY` | `1` | The app is behind Render's proxy. This makes rate limits apply per visitor, not to the proxy. See `client_ip()` in [web/app.py](web/app.py). |
| `RATE_LIMIT_PER_MINUTE` | `30` | Per client IP. Everyone on a venue's wifi shares one IP. |
| `RATE_LIMIT_PER_DAY` | `600` | Per client IP |
| `RATE_LIMIT_GLOBAL_PER_MINUTE` | `60` | Whole site |
| `RATE_LIMIT_GLOBAL_PER_DAY` | `500` | Whole-site daily cap on questions: the cap on API spend |

Everything else uses the defaults in the README's [Configuration](README.md#configuration) table. Saving a change on the Environment page redeploys the service. `PORT` is set by Render.

## Checking that it works

| URL | Expect |
|---|---|
| `/healthz` | `{"ok":true}`. Render's health check polls this path. |
| `/readyz` | `"ready": true`, `"chat_available": true`, `acs_districts: 76`, `legislators_serving: 76`, `trust_proxy: true`, and the rate limits above under `config` |
| `/readyz?deep=1` | Also `"ai_service": "ok"`: the API key works and the Anthropic API is reachable. This check uses no tokens and is cached for 60 seconds. |

```bash
curl -s https://know-your-district.onrender.com/readyz?deep=1
```

What `ai_service` means:

| Value | Meaning |
|---|---|
| `ok` | Healthy |
| `not_configured` | `ANTHROPIC_API_KEY` is missing on the Environment page |
| `auth_error` | The key is wrong or revoked |
| `unreachable` | The Anthropic API couldn't be reached |
| `error` | Any other upstream error |

The map, profiles and `/about` keep working in every case.

## Confirming the deployed commit

1. Locally, run `git fetch origin && git rev-parse origin/main` to get the commit that should be live.
2. In the dashboard, open the service → **Events**. The newest **Deploy live** entry shows the short commit hash and message. It should match the first seven characters from step 1. If a newer deploy is still building or has failed, it appears above it.
3. From a terminal, with a Render API key (Account Settings → API Keys) and the service ID (`srv-…`, in the dashboard URL):

   ```bash
   curl -s -H "Authorization: Bearer $RENDER_API_KEY" \
     "https://api.render.com/v1/services/<SERVICE_ID>/deploys?limit=1" \
     | python -c "import sys,json; d=json.load(sys.stdin)[0]['deploy']; print(d['status'], d['commit']['id'])"
   ```

   This should print `live <full hash>`.

If the live commit is behind, check the deploy log in Events, or use **Manual Deploy → Deploy latest commit**. To roll back, pick an earlier deploy in Events and choose **Rollback**.

## Logs

Open the service → **Logs**. Each `/chat` request writes one JSON line (`"event":"chat"`): its `request_id` (also the `X-Request-ID` response header), status, latency, tool calls and token counts. Questions and answers are never logged.

| Symptom | Likely cause |
|---|---|
| "Chat is temporarily unavailable" | The key is missing or wrong, or the Anthropic account is out of credit. Check `/readyz?deep=1`. |
| Everyone gets "Too many questions" | `TRUST_PROXY` isn't `1`, so every visitor looks like the proxy. Or the per-IP limit is too low for a shared venue network. |
| Chat says the demo hit today's limit | `RATE_LIMIT_GLOBAL_PER_DAY` was reached. The window slides over 24 hours. Raise it on the Environment page if the API budget allows. |

## After the event: stop billing (after November 21, 2026)

Render bills a paid instance by the second while it's active ([FAQ](https://render.com/docs/faq#compute-and-storage-costs)). Claude API usage is billed separately by Anthropic.

1. Open the service → **Settings**. At the bottom of the page, click **Suspend Web Service** and confirm. The URL stops serving the app until you resume.
2. Check **Billing** in the workspace settings over the next day to make sure usage for the service has stopped. Render's docs don't spell out how suspended services are billed, so check this rather than assume it.
3. To bring it back, open the service and click **Resume Web Service**. Settings and environment variables are kept.
4. To end costs permanently instead, delete the service: **Settings** → **Delete Web Service**. Also revoke the API key at https://console.anthropic.com → **API Keys**. The key is useless without the service, but revoking it removes any risk.
