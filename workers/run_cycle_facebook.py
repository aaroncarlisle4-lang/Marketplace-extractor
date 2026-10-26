import json
import os
import sys
import time
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv

from facebook_scraper import (
    scrape_facebook_listing_urls_with_stats,
    scrape_facebook_listings_with_stats,
)
from run_cycle import convex_failure, finish, run_convex
from supabase_backup import run_supabase_backup, supabase_configured

DEFAULT_FACEBOOK_QUERIES = [
    "captain's chair",
    "chesterfield captain chair",
]

DEFAULT_FACEBOOK_TARGET_TERMS = [
    "captain's chair",
    "captains chair",
    "captain chair",
    "chesterfield captain chair",
]


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def require_any_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    joined = ", ".join(names)
    raise RuntimeError(f"Missing required env var (any of): {joined}")


def normalize_url_env(value: str) -> str:
    cleaned = "".join(value.split()).rstrip("/")
    if not cleaned.startswith("http://") and not cleaned.startswith("https://"):
        raise RuntimeError(f"Invalid URL env value: {cleaned}")
    return cleaned


def drop_none_values(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: drop_none_values(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [drop_none_values(v) for v in obj]
    return obj


def parse_list_env(value: str | None) -> List[str]:
    if not value:
        return []
    parts = [part.strip() for part in value.replace("\n", ",").split(",")]
    return [part for part in parts if part]


MAX_POST_ATTEMPTS = 4
POST_BACKOFF_SECONDS = [3, 8, 20]  # one entry per retry (len == MAX_POST_ATTEMPTS - 1)
# Kept short on purpose: the calling workflow has a 10-minute job timeout and
# scraping alone can take up to MAX_RUNTIME_SECONDS, so retries must not eat that budget.


def post_json(url: str, secret: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, MAX_POST_ATTEMPTS + 1):
        try:
            response = requests.post(
                url,
                headers={
                    "content-type": "application/json",
                    "x-ingest-secret": secret,
                },
                data=json.dumps(payload),
                timeout=60,
            )
            if not response.ok:
                try:
                    print(
                        f"[attempt {attempt}] HTTP {response.status_code} from {url}: {response.text[:2000]}",
                        file=sys.stderr,
                    )
                except Exception:
                    pass
                # Client errors (4xx) won't be fixed by retrying — fail fast.
                if 400 <= response.status_code < 500:
                    response.raise_for_status()
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as err:
            last_error = err
            status = err.response.status_code if err.response is not None else None
            if status is not None and 400 <= status < 500:
                break
        except Exception as err:  # noqa: BLE001
            last_error = err
        if attempt < MAX_POST_ATTEMPTS:
            time.sleep(POST_BACKOFF_SECONDS[attempt - 1])
    raise RuntimeError(f"POST failed for {url}: {last_error}")


def main() -> int:
    load_dotenv()

    convex_site_url: str | None = None
    ingest_secret: str | None = None
    try:
        convex_site_url = normalize_url_env(
            require_any_env("CONVEX_SITE_URL", "CONVEX_SITE_URL_FACEBOOK")
        )
        ingest_secret = require_any_env(
            "INGEST_SHARED_SECRET",
            "INGEST_SHARED_SECRET_FACEBOOK",
        )
    except Exception as exc:  # noqa: BLE001
        if not supabase_configured():
            print(json.dumps({
                "status": "skipped",
                "reason": "missing_configuration",
                "error": str(exc),
            }, indent=2))
            return 0
        print(f"::warning::Convex not configured ({exc}); running Supabase-only", file=sys.stderr)
        convex_site_url = None

    queries = parse_list_env(os.getenv("FACEBOOK_QUERIES")) or DEFAULT_FACEBOOK_QUERIES
    listing_urls = parse_list_env(os.getenv("FACEBOOK_LISTING_URLS"))
    target_terms = parse_list_env(os.getenv("FACEBOOK_TARGET_TERMS")) or DEFAULT_FACEBOOK_TARGET_TERMS
    if not queries and not listing_urls:
        raise RuntimeError("No Facebook queries configured")

    strict_target_only = os.getenv("STRICT_TARGET_ONLY", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    location_slug = os.getenv("FACEBOOK_LOCATION_SLUG", "belfast").strip().lower()
    radius_miles = int(os.getenv("FACEBOOK_RADIUS_MILES", "40"))
    max_pages = int(os.getenv("MAX_PAGES", "2"))
    page_load_timeout_seconds = int(os.getenv("PAGE_LOAD_TIMEOUT_SECONDS", "20"))
    max_runtime_seconds = int(os.getenv("MAX_RUNTIME_SECONDS", "180"))
    max_item_age_hours = int(os.getenv("MAX_ITEM_AGE_HOURS", "24"))
    disable_notify = os.getenv("DISABLE_NOTIFY", "0").strip().lower() in {"1", "true", "yes", "on"}

    if listing_urls:
        scrape_result = scrape_facebook_listing_urls_with_stats(
            urls=listing_urls,
            page_load_timeout_seconds=page_load_timeout_seconds,
            max_runtime_seconds=max_runtime_seconds,
            max_item_age_hours=max_item_age_hours,
            strict_target_only=strict_target_only,
            target_terms=target_terms,
        )
        listings_for_query: List[Dict[str, Any]] = drop_none_values(scrape_result["listings"])
        scrape_results_by_query = [
            {
                "mode": "direct_urls",
                "provided_urls": len(listing_urls),
                "scraped": len(listings_for_query),
                "stats": scrape_result["stats"],
            }
        ]
        deduped_listings = {
            f"{item.get('source', 'unknown')}:{item.get('listingId', '')}": item
            for item in listings_for_query
        }
    else:
        per_query_runtime_seconds = max(30, int(max_runtime_seconds / max(1, len(queries))))
        deduped_listings = {}
        scrape_results_by_query = []
        for query in queries:
            scrape_result = scrape_facebook_listings_with_stats(
                query=query,
                location_slug=location_slug,
                radius_miles=radius_miles,
                max_pages=max_pages,
                page_load_timeout_seconds=page_load_timeout_seconds,
                max_runtime_seconds=per_query_runtime_seconds,
                max_item_age_hours=max_item_age_hours,
                strict_target_only=strict_target_only,
                target_terms=target_terms,
            )
            listings_for_query: List[Dict[str, Any]] = drop_none_values(scrape_result["listings"])
            scrape_results_by_query.append(
                {
                    "query": query,
                    "scraped": len(listings_for_query),
                    "stats": scrape_result["stats"],
                }
            )
            for listing in listings_for_query:
                key = f"{listing.get('source', 'unknown')}:{listing.get('listingId', '')}"
                deduped_listings[key] = listing

    listings = list(deduped_listings.values())
    if not listings:
        print(
            json.dumps(
                {
                    "scraped": 0,
                    "queries": queries,
                    "listing_urls": listing_urls,
                    "target_terms": target_terms,
                    "strict_target_only": strict_target_only,
                    "location_slug": location_slug,
                    "radius_miles": radius_miles,
                    "scrape_by_query": scrape_results_by_query,
                    "ingest": None,
                    "notify": None,
                },
                indent=2,
            )
        )
        return 0

    ingest_result, notify_result = run_convex(
        convex_site_url, ingest_secret, listings, disable_notify
    )
    convex_error = convex_failure(ingest_result, notify_result)
    supabase_result = run_supabase_backup(
        "facebook", listings, convex_ok=convex_error is None, disable_notify=disable_notify
    )

    print(
        json.dumps(
            {
                "scraped": len(listings),
                "queries": queries,
                "listing_urls": listing_urls,
                "target_terms": target_terms,
                "strict_target_only": strict_target_only,
                "location_slug": location_slug,
                "radius_miles": radius_miles,
                "screen_only_mode": disable_notify,
                "scrape_by_query": scrape_results_by_query,
                "ingest": ingest_result,
                "notify": notify_result,
                "supabase": supabase_result,
            },
            indent=2,
        )
    )

    return finish(convex_error, supabase_result)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({
            "status": "failed",
            "error": str(exc),
        }, indent=2), file=sys.stderr)
        raise
