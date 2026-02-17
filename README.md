# Marketplace Extractor

Patagonia R1 screener + notifier for Vinted UK using:
- Python Selenium worker for scraping
- Convex for ingestion, screening, dedupe, and state
- Telegram for notifications

## What it does
- Scrapes Vinted UK query `patagonia r1` every cycle.
- Screens listings using strict rules:
  - brand contains Patagonia
  - model contains `r1` (Regulator-only titles without `r1` are rejected)
  - category + condition allowlists
  - price <= `£80` (8000 pence)
  - published within last 24 hours
- Prioritizes freshness tiers:
  - `HOT` < 10 min
  - `NEW` < 60 min
  - `RECENT` < 24h
- Sends deduplicated Telegram alerts.

## Project layout
- `workers/vinted_scraper.py`: Selenium scraper implementation
- `workers/run_cycle.py`: scrape -> ingest -> notify cycle
- `convex/schema.ts`: Convex tables/indexes
- `convex/ingest.ts`: listing upsert + screening persistence
- `convex/jobs.ts`: pending notification processing
- `convex/notifyTelegram.ts`: Telegram sender
- `convex/http.ts`: authenticated ingest/trigger HTTP endpoints
- `convex/crons.ts`: Convex cron for notification pass

## Setup
1. Install JS dependencies:
```bash
npm install
```

2. Configure local env files from examples:
```bash
cp .env.example .env.local
cp workers/.env.example workers/.env
```

3. Set Convex env vars (required by backend):
```bash
npx convex env set INGEST_SHARED_SECRET "replace_me"
npx convex env set TELEGRAM_BOT_TOKEN "replace_me"
npx convex env set TELEGRAM_CHAT_ID "replace_me"
```

4. Deploy/update Convex functions:
```bash
npm run convex:dev
```

5. Setup Python worker dependencies:
```bash
python -m pip install -r workers/requirements.txt
```

6. Run a cycle manually:
```bash
python workers/run_cycle.py
```

## GitHub Actions automation
Workflow file: `.github/workflows/vinted-cycle.yml`

Set these repository secrets:
- `CONVEX_SITE_URL`
- `INGEST_SHARED_SECRET`
- `VINTED_QUERY` (optional, defaults to `patagonia r1`)
- `MAX_PAGES` (optional, defaults to `3`)
- `DISABLE_NOTIFY` (optional, defaults to `1` for screener-only mode)

The workflow runs every 5 minutes.
With `DISABLE_NOTIFY=1`, it keeps ingesting and screening in Convex without Telegram sends.

## Debugging
- Check recent runs in Convex via `queries:getRunStats`.
- Check matches with `queries:getRecentMatches`.
- Check failed notification rollups with `queries:getNotificationFailureSummary`.
- If scraping fails, inspect selectors in `workers/vinted_scraper.py` and update fallback rules.

## Runbook
1. If no alerts are sent:
- Verify Convex env vars: `INGEST_SHARED_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- Verify worker secret matches `INGEST_SHARED_SECRET`.
- Check `queries:getRunStats` for recent `ingest` and `notify` failures.
2. If scrape volume drops to zero:
- Inspect `scrape_stats` output from `workers/run_cycle.py`.
- Confirm Vinted page format changes and update fallback extraction in `workers/vinted_scraper.py`.
3. If repeated Telegram failures occur:
- Check `queries:getNotificationFailureSummary` for the top error strings.
- Fix credentials and rerun one manual cycle.
