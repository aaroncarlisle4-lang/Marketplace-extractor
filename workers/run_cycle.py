import json
import logging
import os
import sys
import time
from typing import Any, Dict, List

import requests
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from vinted_scraper import (
    ScrapeConfig,
    VintedScraper,
    _get_resource_snapshot,
)

logger = logging.getLogger("run-cycle")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)

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
    "patagonia shorts",
    "tala leggings",
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
    "baggies",
    "retro-x",
    "das parka",
    "down sweater",
    "regulator",
    "vest",
    "gilet",
    "parka",
    "leggings",
]


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def normalize_url_env(value: str) -> str:
    # Guard against accidental newlines/spaces in GitHub secrets.
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


def post_json(url: str, secret: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, 4):
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
            response.raise_for_status()
            return response.json()
        except Exception as err:  # noqa: BLE001
            last_error = err
            if attempt < 3:
                time.sleep(attempt * 2)
    raise RuntimeError(f"POST failed for {url}: {last_error}")


def _run_queries_with_shared_scraper(
    queries: List[str],
    target_terms: List[str],
    strict_target_only: bool,
    max_pages: int,
    page_load_timeout_seconds: int,
    per_query_runtime_seconds: int,
    max_item_age_hours: int,
) -> tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Run all queries through a single shared VintedScraper (one Chrome instance).
    Returns (deduped_listings, scrape_results_by_query).
    """
    from time import monotonic

    deduped_listings: Dict[str, Dict[str, Any]] = {}
    scrape_results_by_query: List[Dict[str, Any]] = []

    before = _get_resource_snapshot()
    logger.info(
        "Opening shared Chrome instance for %d queries (fds=%d procs=%d)",
        len(queries),
        before["fd_count"],
        before["proc_count"],
    )

    with VintedScraper(page_load_timeout_seconds=page_load_timeout_seconds) as scraper:
        for query in queries:
            query_start = monotonic()
            logger.info("Scraping query: %r", query)
            config = ScrapeConfig(
                query=query,
                max_pages=max_pages,
                strict_target_only=strict_target_only,
                target_terms=target_terms,
                page_load_timeout_seconds=page_load_timeout_seconds,
                max_runtime_seconds=per_query_runtime_seconds,
                max_item_age_hours=max_item_age_hours,
            )
            try:
                listings_for_query_raw = scraper.run(config)
            except Exception as exc:
                logger.error("Query %r failed: %s", query, exc, exc_info=True)
                scrape_results_by_query.append(
                    {"query": query, "scraped": 0, "stats": scraper.stats, "error": str(exc)}
                )
                continue

            listings_for_query: List[Dict[str, Any]] = drop_none_values(listings_for_query_raw)
            elapsed = monotonic() - query_start
            logger.info(
                "Query %r done: %d listings in %.1fs (stats=%s)",
                query,
                len(listings_for_query),
                elapsed,
                scraper.stats,
            )
            scrape_results_by_query.append(
                {"query": query, "scraped": len(listings_for_query), "stats": dict(scraper.stats)}
            )
            for listing in listings_for_query:
                key = f"{listing.get('source', 'unknown')}:{listing.get('listingId', '')}"
                deduped_listings[key] = listing

            # Reset per-query stats for the next query
            scraper.stats = {k: 0 for k in scraper.stats}

    after = _get_resource_snapshot()
    fd_delta = after["fd_count"] - before["fd_count"]
    proc_delta = after["proc_count"] - before["proc_count"]
    if fd_delta > 10 or proc_delta > 3:
        logger.warning(
            "Resource leak after all queries: fds %d→%d (delta=%+d), procs %d→%d (delta=%+d)",
            before["fd_count"],
            after["fd_count"],
            fd_delta,
            before["proc_count"],
            after["proc_count"],
            proc_delta,
        )
    else:
        logger.info(
            "Shared Chrome closed cleanly: fds %d→%d (delta=%+d), procs %d→%d (delta=%+d)",
            before["fd_count"],
            after["fd_count"],
            fd_delta,
            before["proc_count"],
            after["proc_count"],
            proc_delta,
        )

    return deduped_listings, scrape_results_by_query


def main() -> int:
    convex_site_url = normalize_url_env(require_env("CONVEX_SITE_URL"))
    ingest_secret = require_env("INGEST_SHARED_SECRET")

    queries = parse_list_env(os.getenv("VINTED_QUERIES"))
    if not queries:
        single_query = os.getenv("VINTED_QUERY")
        queries = [single_query] if single_query else DEFAULT_VINTED_QUERIES
    queries = [q.strip() for q in queries if q.strip()]
    if not queries:
        raise RuntimeError("No Vinted queries configured")

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

    deduped_listings, scrape_results_by_query = _run_queries_with_shared_scraper(
        queries=queries,
        target_terms=target_terms,
        strict_target_only=strict_target_only,
        max_pages=max_pages,
        page_load_timeout_seconds=page_load_timeout_seconds,
        per_query_runtime_seconds=per_query_runtime_seconds,
        max_item_age_hours=max_item_age_hours,
    )

    listings = list(deduped_listings.values())
    if not listings:
        print(json.dumps({
            "scraped": 0,
            "queries": queries,
            "target_terms": target_terms,
            "strict_target_only": strict_target_only,
            "scrape_by_query": scrape_results_by_query,
            "ingest": None,
            "notify": None,
        }, indent=2))
        return 0

    ingest_url = f"{convex_site_url}/ingest/listings"
    notify_url = f"{convex_site_url}/jobs/run-notifications"

    ingest_result = post_json(ingest_url, ingest_secret, {"listings": listings})
    notify_result = None
    if not disable_notify:
        notify_result = post_json(notify_url, ingest_secret, {"limit": 100})
    else:
        notify_result = {"disabled": True, "reason": "DISABLE_NOTIFY is enabled"}

    print(json.dumps({
        "scraped": len(listings),
        "queries": queries,
        "target_terms": target_terms,
        "strict_target_only": strict_target_only,
        "scrape_by_query": scrape_results_by_query,
        "screen_only_mode": disable_notify,
        "ingest": ingest_result,
        "notify": notify_result,
    }, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"run_cycle failed: {exc}", file=sys.stderr)
        raise
