# DSA Tracker

Spaced-repetition practice tracker for LeetCode problems.

**Stack:** FastAPI · Jinja2 · HTMX · Alpine.js · TailwindCSS · SQLite

## Auth

Create an account at `/register`, then log in. Each user’s problems are private.

## Schedule rules

| How you solved it | Next review |
|-------------------|-------------|
| Looking at code / watching video | **Frequent** — always in **2 days** |
| By your own (before 2nd revision) | **Spaced** — 2 days, then 4 days |
| By your own (on/after 2nd revision) | **Random** — between **4 and 60 days** |

Mark a problem **Mastered** when you want to drop it from active revision.

Each attempt records **how** you solved it and **time taken**.

## Run

```bash
uv sync
uv run main.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

Optional: set `DSA_SESSION_SECRET` for signed session cookies in production.
