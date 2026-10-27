import json
import os
import sys
from typing import Any, Dict, List

from dotenv import load_dotenv

from facebook_scraper import (
    scrape_facebook_listing_urls_with_stats,
    scrape_facebook_listings_with_stats,
)
from run_cycle import finish
from supabase_backup import run_supabase

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


def main() -> int:
    load_dotenv()

    # Supabase stores listings and sends the alerts; Convex is no longer used.
    require_env("SUPABASE_URL")
    require_env("SUPABASE_SERVICE_ROLE_KEY")

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
                    "supabase": None,
                },
                indent=2,
            )
        )
        return 0

    supabase_result = run_supabase("facebook", listings, disable_notify=disable_notify)

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
                "supabase": supabase_result,
            },
            indent=2,
        )
    )

    return finish(supabase_result)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({
            "status": "failed",
            "error": str(exc),
        }, indent=2), file=sys.stderr)
        raise
