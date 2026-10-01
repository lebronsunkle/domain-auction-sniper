# Domain Auction Sniper

A full stack tool for finding, tracking and winning domain name auctions on GoDaddy. It scans the daily auction inventory (900k+ listings), scores every name for brandability, lets you build a watchlist with a max bid for each domain, and fires the bid in the final seconds of the auction. Server side safety limits make sure it never spends more than you allow.

I built this for a domain investor client, and it has been running in production since mid 2026. This repo is a cleaned up portfolio version: credentials, client data and client specific tuning have been removed, and the keyword lists are short examples.

## What it does

**Inventory scoring.** A daily job streams GoDaddy's expiring and closeout feeds and scores each domain on TLD, length, composition, dictionary words, theme buckets, two concept compounds (`bio` + `lytics`) and respellings (`lite`, `tek`). It also flags likely misspellings.

**Watchlist and sniping.** You set a max bid for each domain. A background worker tracks end times and places the bid about 1.5 seconds before close, timed against measured API latency. It checks whether the account already has a bid first, so it never bids against itself.

**Safety governors.** Every bid or purchase passes these checks on the server before any money moves:
* Global kill switch
* Per transaction cap and daily spend cap
* A sanity multiplier that rejects bids far above the domain's estimated value, unless you explicitly confirm the amount
* Dry run mode for testing the decision logic without spending anything

**Alerts.** Reminder emails as auctions near their end (Resend), plus email and SMS alerts the moment you're outbid (Twilio).

**Dashboard.** A single page React app on Cloudflare Pages with tabs for top rated, top value and expiring soon, full inventory search, inline max bid editing and live bid state.

## Stack

| Layer | Tech |
|---|---|
| API | Python 3.11, FastAPI, Pydantic v2 |
| Data | PostgreSQL (SQLAlchemy 2 async, asyncpg), Alembic migrations |
| Workers | asyncio background tasks, APScheduler |
| Integrations | GoDaddy REST + SOAP APIs, Estibot valuations, Resend email, Twilio SMS |
| Frontend | React 18 (single file, no build step), Cloudflare Pages |
| Infra | Docker, Fly.io, GitHub Actions deploys |
| Tests | pytest, pytest-asyncio (330+ tests) |

## Layout

```
app/
  api/         REST endpoints (watchlist, search, auctions, settings, purchases)
  godaddy/     API clients: REST, SOAP, inventory feed parser, availability
  scoring/     tokenizer and brandability scoring engine
  safety/      spend governors and purchase guard
  notify/      email + SMS alert rendering and transport
  models/      SQLAlchemy models
worker/        snipe trigger loop, notifier, inventory sync
dashboard/     single page React dashboard
tests/         unit and integration tests
```

## Running locally

```bash
cp .env.example .env          # fill in your own GoDaddy OTE (sandbox) keys
pip install -e ".[dev]"
sudo apt-get install wamerican  # dictionary used by the tokenizer
alembic upgrade head
uvicorn app.main:app --reload
```

Run the tests with `pytest`. Leave `GODADDY_ENV=ote` (the sandbox) unless you really mean to bid with real money.

## Notes

* The API requires a bearer token on every `/api` request. In production it fails closed if no token is configured.
* Only one worker machine runs at a time. Two trigger workers without leader election could double bid.
