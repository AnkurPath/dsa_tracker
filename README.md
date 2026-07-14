# DSA Revision Helper

Spaced-repetition practice helper for LeetCode problems.

**Stack:** FastAPI · Jinja2 · HTMX · Alpine.js · TailwindCSS · SQLite

## Auth

Create an account at `/register` with **email + password**, then log in. Each user’s problems are private.

## Schedule rules

| How you solved it | Next review |
|-------------------|-------------|
| Looking at code / watching video | **Frequent** — always in **2 days** |
| By your own (before 2nd revision) | **Spaced** — 2 days, then 4 days |
| By your own (on/after 2nd revision) | **Random** — between **4 and 60 days** |

## Practice tools

- **LeetCode auto-import** — connect your public username; recent accepted solves land in your queue automatically (skips problems already tracked)
- **Email reminders** — daily email of problems due today (syncs LeetCode first)
- **Session mode** — clear due revisions one-by-one with a timer (weak topics prioritized)
- **Weakness radar** — own-solve rate, frequent-review load, and coach-style headlines
- **Topic tags** — pulled from LeetCode; filter the dashboard and spot gaps by topic

## Email (optional)

Set these in `.env` (see `.env.example`):

```bash
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=your-app-password
SMTP_FROM="DSA Revision Helper <you@gmail.com>"
APP_BASE_URL=http://127.0.0.1:8000
```

On the dashboard, choose a **send time in IST** (mail goes to your account email). At that time each day the app syncs LeetCode, then:

- sends a **due problems** email if revisions remain (links open DSA Revision Helper)
- sends a **congratulations** email if you already cleared today’s queue

## Run

```bash
uv sync
uv run main.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

Optional: set `DSA_SESSION_SECRET` for signed session cookies in production.

## Docker

### Local build

```bash
cp .env.example .env   # fill SMTP + APP_BASE_URL
docker compose up --build -d
```

### Pull from GHCR (run anywhere)

Images are published to [`ghcr.io/ankurpath/dsa_tracker`](https://github.com/AnkurPath/dsa_tracker/pkgs/container/dsa_tracker) on every push to `main`.

```bash
# If the package is private, log in first:
# echo $GITHUB_TOKEN | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin

cp .env.example .env   # fill SMTP + APP_BASE_URL (+ DSA_SESSION_SECRET)
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

Or without Compose:

```bash
docker pull ghcr.io/ankurpath/dsa_tracker:latest
docker run -d --name dsa-tracker \
  -p 8000:8000 \
  --env-file .env \
  -e DSA_DB_PATH=/data/dsa_tracker.db \
  -v dsa_data:/data \
  ghcr.io/ankurpath/dsa_tracker:latest
```

App: [http://localhost:8000](http://localhost:8000). SQLite persists in the `dsa_data` volume.

To make the image public: GitHub → Packages → `dsa_tracker` → Package settings → Change visibility.

## Deploy on Render

Your app needs a **persistent disk** for SQLite, so use at least the **Starter** plan (free web services wipe the filesystem on restart).

### Option A — Blueprint (easiest)

1. Push this repo to GitHub (including `Dockerfile` + `render.yaml`).
2. Go to [https://dashboard.render.com](https://dashboard.render.com) → **New** → **Blueprint**.
3. Connect the `dsa_tracker` repo and apply the blueprint.
4. Fill in env vars when prompted:
   - `APP_BASE_URL` → `https://YOUR-SERVICE.onrender.com` (no trailing slash)
   - `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` (Gmail app password)
5. Wait for the first deploy. Open the service URL and register with email.

### Option B — Manual Web Service

1. **New** → **Web Service** → connect this GitHub repo.
2. Runtime: **Docker** (Render builds from `Dockerfile`).
3. Instance: **Starter** (required for disk).
4. **Disk**: add a disk, mount path `/data`, size 1 GB.
5. **Environment**:

| Key | Value |
|---|---|
| `DSA_DB_PATH` | `/data/dsa_tracker.db` |
| `APP_BASE_URL` | `https://YOUR-SERVICE.onrender.com` |
| `DSA_SESSION_SECRET` | long random string |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | your Gmail |
| `SMTP_PASSWORD` | Gmail app password |
| `SMTP_FROM` | `DSA Revision Helper <you@gmail.com>` |
| `SMTP_TLS` | `1` |

6. Deploy → open the URL → `/register`.

### After deploy

- Email links use `APP_BASE_URL` — set it to your real `https://….onrender.com`.
- Redeploy happens automatically on push to `main`.
- Check **Logs** if the service won’t start (SMTP/env mistakes show up there).
