import json
import os
import sys
import time
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv

from vinted_scraper import scrape_vinted_listings_with_stats


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


def main() -> int:
    load_dotenv()

    convex_site_url = normalize_url_env(require_env("CONVEX_SITE_URL"))
    ingest_secret = require_env("INGEST_SHARED_SECRET")

    query = os.getenv("VINTED_QUERY", "patagonia r1")
    max_pages = int(os.getenv("MAX_PAGES", "3"))
    page_load_timeout_seconds = int(os.getenv("PAGE_LOAD_TIMEOUT_SECONDS", "20"))
    max_runtime_seconds = int(os.getenv("MAX_RUNTIME_SECONDS", "240"))
    max_item_age_hours = int(os.getenv("MAX_ITEM_AGE_HOURS", "24"))
    disable_notify = os.getenv("DISABLE_NOTIFY", "1").strip().lower() in {"1", "true", "yes", "on"}

    scrape_result = scrape_vinted_listings_with_stats(
        query=query,
        max_pages=max_pages,
        page_load_timeout_seconds=page_load_timeout_seconds,
        max_runtime_seconds=max_runtime_seconds,
        max_item_age_hours=max_item_age_hours,
    )
    listings: List[Dict[str, Any]] = drop_none_values(scrape_result["listings"])
    if not listings:
        print(json.dumps({
            "scraped": 0,
            "scrape_stats": scrape_result["stats"],
            "ingest": None,
            "notify": None,
        }, indent=2))
        return 0

    ingest_url = f"{convex_site_url}/ingest/listings"
    notify_url = f"{convex_site_url}/jobs/run-notifications"

    ingest_result = post_json(ingest_url, ingest_secret, {"listings": listings})
    notify_result = None
    if not disable_notify:
        notify_result = post_json(notify_url, ingest_secret, {"limit": 25})
    else:
        notify_result = {"disabled": True, "reason": "DISABLE_NOTIFY is enabled"}

    print(json.dumps({
        "scraped": len(listings),
        "scrape_stats": scrape_result["stats"],
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
