# Hosting on Render

One Docker web service runs the API, the worker and the built dashboard on a public `https://<name>.onrender.com` URL. Blueprint: [`render.yaml`](../render.yaml). Image: [`Dockerfile`](../Dockerfile).

## Why not Google Cloud

The GCP service account is for **Gemini calls through Vertex AI only**. Its rules forbid creating resources or using other Google Cloud services, so it can't host the app. The app runs on Render and uses the key only to call `gemini-*` models. Non-Gemini model IDs are rejected in code (`validate_gemini_model`).

## One-time setup

1. Render dashboard → **New → Blueprint** → connect `PalakManocha02/Team-Dakia` → branch `deploy/render`.
2. Enter the values Render asks for (they are never committed):
   - `ACCESS_CODE`: the team passcode the dashboard asks for.
   - `CRUSTDATA_API_KEY`: needed only for live supplier checks.
3. Service → **Environment → Secret Files** → add `gcp-sa.json` and paste the service-account JSON. Render mounts it at `/etc/secrets/gcp-sa.json`, which is where `GOOGLE_APPLICATION_CREDENTIALS` points.
4. Deploy. Health check: `/api/health`.

## Spend guards

| Variable | Default | Effect |
|---|---|---|
| `ACCESS_CODE` | required | Every `/api` route except `/api/health` needs the `X-Access-Code` header. The dashboard asks for it once and keeps it in the browser. |
| `MAX_RUNS_PER_DAY` | 10 | New requests per rolling 24 hours. Each one can spend Gemini and Crustdata credits. |
| `MAX_CREDIT_BUDGET` | 25 | Highest Crustdata credit ceiling a run can set or raise to. |

Gemini also keeps its existing per-run cap of four generations. Leaving these variables unset (local development) turns the guards off.

## Limits of the free plan

- **Data resets.** SQLite lives on the container disk, so runs disappear on every redeploy or restart. To keep history, upgrade to a paid plan, add a disk mounted at `/data` and keep `SOURCING_DB=/data/sourcing.db`.
- **Cold starts.** The service sleeps after 15 idle minutes, and the first visit after that takes about a minute to wake it.
- **The worker shares the container.** It restarts automatically if it exits, including in the `checkout_crash` demo.
