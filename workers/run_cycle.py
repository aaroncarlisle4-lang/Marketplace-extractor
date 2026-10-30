import json
import os
import sys
from typing import Any, Dict, List

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from supabase_backup import run_supabase
from vinted_scraper import scrape_vinted_listings_with_stats

DEFAULT_VINTED_QUERIES = [
    "patagonia r1",
    "patagonia r2",
    "patagonia r3",
    "patagonia torrentshell jacket",
    "patagonia h2no",
    "patagonia goretex",
    "patagonia ski jacket",
    "patagonia fleece",
    "patagonia synchilla",
    "patagonia better sweater",
    "patagonia snap-t",
    "patagonia retro-x",
    "patagonia micro puff",
    "patagonia nano puff",
]

DEFAULT_TARGET_TERMS = [
    "r1",
    "r2",
    "r3",
    "torrentshell",
    "h2no",
    "goretex",
    "gore-tex",
    "ski jacket",
    "jacket",
    "fleece",
    "synchilla",
    "snap-t",
    "better sweater",
    "nano puff",
    "micro puff",
    "puffer",
    "sweater",
    "jumper",
    "capilene",
    "retro-x",
    "das parka",
    "down sweater",
    "regulator",
    "vest",
    "gilet",
    "parka",
]

MAX_PRICE_GBP = int(os.getenv("MAX_PRICE_GBP", "20"))
VINTED_MAX_PRICE_MINOR = int(os.getenv("VINTED_MAX_PRICE_MINOR", "5000"))


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
    # Supabase stores listings and sends the alerts; Convex is no longer used.
    require_env("SUPABASE_URL")
    require_env("SUPABASE_SERVICE_ROLE_KEY")

    from vinted_watchlist import configured_state, poll_updates
    client, state = configured_state()
    if os.getenv("VINTED_POLL_TELEGRAM", "1") == "1":
        poll_updates(client, state)
    watches = [dict(watch) for watch in state["items"]]
    queries = [watch["query"] for watch in watches]

    target_terms = parse_list_env(os.getenv("VINTED_TARGET_TERMS"))
    if not target_terms:
        target_terms = DEFAULT_TARGET_TERMS

    strict_target_only = os.getenv("STRICT_TARGET_ONLY", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    max_pages = int(os.getenv("MAX_PAGES", "10"))
    page_load_timeout_seconds = int(os.getenv("PAGE_LOAD_TIMEOUT_SECONDS", "20"))
    max_runtime_seconds = int(os.getenv("MAX_RUNTIME_SECONDS", "480"))
    max_item_age_hours = int(os.getenv("MAX_ITEM_AGE_HOURS", "24"))
    disable_notify = os.getenv("DISABLE_NOTIFY", "0").strip().lower() in {"1", "true", "yes", "on"}

    per_query_runtime_seconds = max(30, int(max_runtime_seconds / max(1, len(queries))))
    deduped_listings: Dict[str, Dict[str, Any]] = {}
    scrape_results_by_query: List[Dict[str, Any]] = []

    for watch in watches:
        query = watch["query"]
        current_max_price = (watch["max_price_minor"] + 99) // 100

        scrape_result = scrape_vinted_listings_with_stats(
            query=query,
            max_pages=max_pages,
            page_load_timeout_seconds=page_load_timeout_seconds,
            max_runtime_seconds=per_query_runtime_seconds,
            max_item_age_hours=max_item_age_hours,
            strict_target_only=strict_target_only if watch.get("legacy") else False,
            target_terms=target_terms,
            max_price_gbp=current_max_price,
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
            previous = deduped_listings.get(key, {})
            listing["_watches"] = previous.get("_watches", []) + [watch]
            deduped_listings[key] = listing

        if os.getenv("VINTED_POLL_TELEGRAM", "1") == "1":
            poll_updates(client, state)

    listings = list(deduped_listings.values())
    if not listings:
        print(json.dumps({
            "scraped": 0,
            "queries": queries,
            "target_terms": target_terms,
            "strict_target_only": strict_target_only,
            "scrape_by_query": scrape_results_by_query,
            "supabase": None,
        }, indent=2))
        return 0

    supabase_result = run_supabase("vinted", listings, disable_notify=disable_notify)

    print(json.dumps({
        "scraped": len(listings),
        "queries": queries,
        "target_terms": target_terms,
        "strict_target_only": strict_target_only,
        "scrape_by_query": scrape_results_by_query,
        "screen_only_mode": disable_notify,
        "supabase": supabase_result,
    }, indent=2))

    return finish(supabase_result)


def finish(supabase_result: Dict[str, Any]) -> int:
    error = supabase_result.get("error")
    if error:
        raise RuntimeError(f"Supabase failed: {error}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({
            "status": "failed",
            "error": str(exc),
        }, indent=2), file=sys.stderr)
        raise
