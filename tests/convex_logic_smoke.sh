#!/usr/bin/env bash
set -euo pipefail

: "${CONVEX_SITE_URL:?missing CONVEX_SITE_URL}"
: "${INGEST_SHARED_SECRET:?missing INGEST_SHARED_SECRET}"

NOW_UTC=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

curl -sS -X POST "${CONVEX_SITE_URL}/ingest/listings" \
  -H 'content-type: application/json' \
  -H "x-ingest-secret: ${INGEST_SHARED_SECRET}" \
  -d "{\"listings\":[{\"source\":\"vinted_uk\",\"listingId\":\"smoke-001\",\"url\":\"https://www.vinted.co.uk/items/999999-smoke\",\"title\":\"Patagonia R1 Smoke Test\",\"brand\":\"Patagonia\",\"priceMinor\":7000,\"currency\":\"GBP\",\"condition\":\"Very good condition\",\"category\":\"fleece\",\"publishedAt\":\"${NOW_UTC}\",\"fetchedAt\":\"${NOW_UTC}\"}]}"

echo

curl -sS -X POST "${CONVEX_SITE_URL}/jobs/run-notifications" \
  -H 'content-type: application/json' \
  -H "x-ingest-secret: ${INGEST_SHARED_SECRET}" \
  -d '{"limit":10}'

echo

echo "Smoke test calls completed. Validate in Convex dashboard/queries."
