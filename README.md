# Marketplace Extractor

Patagonia screener + notifier for Vinted UK using:
- Python Selenium worker for scraping
- Supabase for storage, screening, dedupe, and scheduling (pg_cron)
- Telegram for notifications (each alert is a photo of the listing with the details and link as caption)

Convex is retired: its crons are removed and its Telegram/GitHub tokens unset,
so it can no longer send alerts. The `convex/` folder is kept for reference only.

Also includes a Facebook Marketplace pipeline for:
- `captain's chair`
- `chesterfield captain chair`
- Belfast radius filter (40 miles)
- Recent-only matches (up to 24h old; notifications prioritize <= 5 minutes)

## What it does
- Scrapes multiple Vinted UK Patagonia queries every cycle (including `r1`, `torrentshell`, `h2no`, `goretex`, and `ski jacket`).
- Screens listings using strict rules:
  - brand contains Patagonia
  - model/title contains one of the configured target terms
  - category + condition allowlists
  - price <= `£50` (5000 pence) for each individual listing
  - published within last 24 hours
- Prioritizes freshness tiers:
  - `HOT` < 10 min
  - `NEW` < 60 min
  - `RECENT` < 24h
- Sends deduplicated Telegram alerts. Each listing is claimed in Supabase's `notifications` table before it is sent, so it is alerted at most once.

## Project layout
- `workers/vinted_scraper.py`: Selenium scraper implementation
- `workers/run_cycle.py`: scrape -> ingest -> notify cycle
- `workers/facebook_scraper.py`: Facebook Marketplace scraper implementation
- `workers/run_cycle_facebook.py`: Facebook scrape -> ingest -> notify cycle
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

7. Run the Facebook cycle manually:
```bash
python workers/run_cycle_facebook.py
```

## GitHub Actions automation
Workflow file: `.github/workflows/vinted-cycle.yml`
Workflow file: `.github/workflows/facebook-cycle.yml`

Set these repository secrets:
- `CONVEX_SITE_URL`
- `INGEST_SHARED_SECRET`
- `VINTED_QUERY` (optional, defaults to `patagonia r1`)
- `VINTED_QUERIES` (optional, comma-separated query list for multi-product search)
- `VINTED_TARGET_TERMS` (optional, comma-separated model/keyword terms)
- `STRICT_TARGET_ONLY` (optional, defaults to `1`)
- `MAX_PAGES` (optional, defaults to `1`)
- `PAGE_LOAD_TIMEOUT_SECONDS` (optional, defaults to `12`)
- `MAX_RUNTIME_SECONDS` (optional, defaults to `120`)
- `DISABLE_NOTIFY` (optional, defaults to `0` so notifications send immediately)

For Facebook workflow (`facebook-cycle.yml`), set:
- `CONVEX_SITE_URL_FACEBOOK`
- `INGEST_SHARED_SECRET_FACEBOOK`
- `FACEBOOK_QUERIES` (optional)
- `FACEBOOK_LISTING_URLS` (optional, comma-separated direct listing URLs; bypasses search discovery)
- `FACEBOOK_TARGET_TERMS` (optional)
- `FACEBOOK_LOCATION_SLUG` (optional, default `belfast`)
- `FACEBOOK_RADIUS_MILES` (optional, default `40`)

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

## Vinted Telegram watch list

Apply `supabase/migrations/20261030120000_vinted_watchlist.sql` to the existing
Supabase project **before deploying these workers**. Set `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID` on the
worker host. The new table has RLS enabled and is accessible only to the service
role. No new Python dependency is needed.

Send `/start` to the Vinted bot to show its keyboard:

- **Add item** asks for search terms, then a maximum price in GBP.
- **Watch list** shows all saved searches and their price limits.
- **Remove item** asks for the displayed search number to delete.
- **Cancel** exits the current operation.

Only messages in `TELEGRAM_CHAT_ID` can change the list. Use a private chat for
single-user control. Searches and pending conversations survive restarts.
The first run seeds the existing environment/default searches with their current
filters. Once initialized, Supabase is authoritative: removing every search
pauses scraping, and environment changes do not recreate deleted searches.
New searches match all search words in the listing title, brand or description,
with the saved GBP price cap and the existing freshness/notification windows.
Existing seeded searches retain the Patagonia screening rules. Listing alerts
continue using the existing deduplication records.

The server polls menu messages every five seconds; the GitHub Actions worker
processes messages at the beginning of each cycle and between search queries. Run **one polling host per
bot token**, with no Telegram webhook configured. When using the server, disable
any separate scheduled Actions worker for this bot. Scraper subprocesses launched
by the server set `VINTED_POLL_TELEGRAM=0` to avoid competing with its menu poller.
Actions-only users may need to wait for the current search query or the next scheduled cycle for each reply. Changes made during a scrape apply to the next cycle.
Send `/start` after deployment to reveal the menu; deployment sends no unsolicited
messages.

Validation: `python -m pytest -q` (install `pytest` plus
`workers/requirements.txt`). Tests use fake Telegram/Supabase services and do
not send real messages.
