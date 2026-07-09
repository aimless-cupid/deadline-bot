# CLAUDE.md — deadline-bot

> Operating contract for Claude Code on this repo. Read before any change. Keep updated as the build moves.

## What this is
A Telegram bot that ingests a forwarded/pasted announcement, makes **one** structured model call to extract a deadline object, stores it in PostgreSQL, and surfaces it on a FastAPI dashboard (upcoming-deadline timeline + keyword search). Deployed on a free host.

**Files:** `handler.py` holds the shared per-update pipeline (`handle_update`); `bot.py` (long-poll) is the **local-dev** transport, `web.py` (FastAPI) serves the dashboard **and** the production webhook route `POST /telegram/webhook` (guarded by `WEBHOOK_SECRET`). Both transports call the same `handle_update`, so dev and prod can't drift. `extractor.py` = model call + date resolution; `db.py` = Postgres.

## Stack — LOCKED (do not change without explicit instruction)
- **Python 3.12** (not 3.13 / 3.14 — see gotchas)
- **FastAPI** — dashboard (Rung 5)
- **PostgreSQL** + **psycopg v3** (`psycopg[binary]`)
- **Model API via raw REST** — Gemini 2.5 Flash. **No SDK** (see gotchas).
- Dev env: **Windows + VS Code**. Secrets via **`python-dotenv`**.

## Scope — FROZEN. This is a clean *code* rebuild, NOT a feature reset.
**v1 must-ship — the ONLY things in scope:**
1. Telegram bot accepts forwarded/pasted text
2. One LLM call → structured `{type, title, deadline, course, summary, tags}` (+ `has_deadline`)
3. Store in PostgreSQL (with dedupe)
4. FastAPI dashboard: upcoming-deadline timeline, sorted + basic keyword search
5. Deployed on a free host, shareable link

**Done = deployed + 30+ real announcements processed + a handful of real users + README with demo. Nothing more.**

**Parking lot — DO NOT BUILD. Defer all of these:** reminders/notifications, semantic/NL search, OCR on posters, voice transcription, importance scoring, AI Q&A assistant, weekly digest, multi-platform, UI polish. New ideas go *here*, never into the code.

## Hard rules
- **Raw REST for the model API, never an SDK.** The API key has an `AQ.` prefix → the official SDK rejects it; raw REST works. Header `x-goog-api-key`, `responseMimeType=json`, `temperature=0`.
- **Secrets only in `.env`** (loaded via `python-dotenv`). `.env` is gitignored. **Never hardcode keys/tokens; never commit `.env`.**
- **One failure surface at a time.** Build rung by rung; isolation-test each component *before* integrating.
- **Plan before code** on anything non-trivial; explain non-obvious choices. The human must be able to explain every line and defend every decision — annotate, don't just emit.
- **"Better" = architecture, tests, README — not new features or gold-plating.** The done-bar is fixed.
- **Git: put fixes on a short-lived branch + open a PR.** I'll merge.

## Known gotchas (apply immediately — don't re-discover)
- **`AQ.` key → raw REST** (above). No provider switch.
- **psycopg wheels were flaky on Python 3.14** → we use 3.12, which sidesteps it entirely. If a wheel ever fails to install, that's the class of problem — troubleshoot, don't panic.
- **Extraction behavior to preserve:** clear deadlines extract fully; events-with-dates that are *not* deadlines (e.g. "biryani Friday") return `has_deadline=False` by design.
- **Hardening that works:** retry/backoff on `503`/`429`, request timeouts, clean `Ctrl+C` shutdown.

## Build structure — rungs (one new failure surface each; isolation-test before integrating)
1. **Env** — Python 3.12, VS Code, fresh repo, `.venv`, `requests`, Claude Code on the repo, GitHub.  ← *current*
2. **Echo bot** — raw REST long-poll (`getUpdates` / `sendMessage` + offset); token in `.env`.
3. **LLM extraction** — raw REST → structured dict; the model extracts `when_text` (a verbatim date phrase), Python resolves it to a real date via `dateparser` (no calendar math in the model); hardened per above.
4. **Postgres storage** — install Postgres, psycopg v3, create `deadlinebot` DB, `DATABASE_URL` in `.env`; isolation-test connectivity *before* writing storage code; add dedupe.
5. **FastAPI dashboard** — timeline + keyword search.
6. **Deploy** — free host (Render, Python pinned to 3.12 via `.python-version`) with real users; the bot runs as a Telegram **webhook** (`web.py`), not long-poll; confirm the host's supported Python first.

## Commands
*(fill in as the build moves)*
```
# create venv (pins 3.12)
py -3.12 -m venv .venv
# activate (PowerShell)
.venv\Scripts\Activate.ps1
# install deps
pip install -r requirements.txt
# run bot         — TBD (Rung 2)
# run dashboard   — TBD (Rung 5)
```

## Database
- `DATABASE_URL=postgresql://postgres:<pwd>@localhost:5432/deadlinebot` (in .env)
- Table `deadlines`: id, title, type, deadline (DATE, nullable), course, summary, tags (text[]), raw_text, content_hash (UNIQUE), created_at
- Dedupe: sha256 of normalized raw_text → content_hash; INSERT ... ON CONFLICT (content_hash) DO NOTHING
- Date resolution: LLM extracts when_text (phrase); Python (`extractor.resolve_when`) resolves via dateparser, anchored to the message's send time in `BOT_TZ` (.env, default UTC). LLM does NOT compute dates. Two dateparser gaps are patched in code: "next/this <weekday>" (returns None) and bare day-of-month like "the 25th" (mis-parsed).
- Pooler-safe: all connections go through `db._connect()`, which passes `prepare_threshold=None` to disable server-side prepared statements — required for managed Postgres (Neon) whose pooled endpoint is PgBouncer in transaction mode (no stable backend across statements).
