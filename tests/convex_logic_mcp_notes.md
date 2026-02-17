# Convex Logic MCP Proof

Use these MCP calls to validate screening + notifications without local network access to `*.convex.site`:

1. Run `ingest.js:upsertListings` with a Patagonia R1 listing.
2. Run `jobs.js:runNotificationPass`.
3. Run `queries.js:getRecentMatches` and `queries.js:getRunStats`.
4. Read tables `notifications` and `runs`.

Expected outcomes:
- Listing appears in `listingSnapshots`.
- Match appears with freshness bucket and score.
- Notification row created (`sent` or `failed`).
- `runs` row created for kind `notify`.
