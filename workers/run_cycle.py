import json
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


MAX_POST_ATTEMPTS = 4
POST_BACKOFF_SECONDS = [3, 8, 20]  # one entry per retry (len == MAX_POST_ATTEMPTS - 1)
# Kept short on purpose: the calling workflows have a 10-minute job timeout and
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
    deduped_listings: Dict[str, Dict[str, Any]] = {}
    scrape_results_by_query: List[Dict[str, Any]] = []

    for query in queries:
        q_lower = query.lower()
        # Use 50 for Patagonia, otherwise use the configured MAX_PRICE_GBP (default 20)
        current_max_price = VINTED_MAX_PRICE_MINOR // 100 if "patagonia" in q_lower else MAX_PRICE_GBP

        scrape_result = scrape_vinted_listings_with_stats(
            query=query,
            max_pages=max_pages,
            page_load_timeout_seconds=page_load_timeout_seconds,
            max_runtime_seconds=per_query_runtime_seconds,
            max_item_age_hours=max_item_age_hours,
            strict_target_only=strict_target_only,
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
            deduped_listings[key] = listing

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

    # Send in smaller chunks rather than one big request: a single oversized
    # POST is more likely to make the Convex http action run long enough to be
    # killed mid-flight, which still leaves earlier batches committed server-side
    # but reports the whole call as failed.
    ingest_chunk_size = int(os.getenv("INGEST_CHUNK_SIZE", "100"))
    ingest_accum = {"inserted": 0, "updated": 0, "matched": 0, "rejected": 0}
    ingest_errors: List[str] = []
    for i in range(0, len(listings), ingest_chunk_size):
        chunk = listings[i : i + ingest_chunk_size]
        try:
            chunk_result = post_json(ingest_url, ingest_secret, {"listings": chunk})
            for key in ingest_accum:
                ingest_accum[key] += chunk_result.get(key, 0)
        except Exception as err:  # noqa: BLE001
            ingest_errors.append(str(err))
            print(f"ingest chunk of {len(chunk)} listings failed: {err}", file=sys.stderr)
    ingest_result: Dict[str, Any] = {**ingest_accum, "errors": ingest_errors}

    # Always attempt notifications, even if ingest partially or fully failed:
    # batches that did commit server-side before a failure can still contain
    # real matches that deserve a Telegram alert.
    if disable_notify:
        notify_result: Dict[str, Any] = {"disabled": True, "reason": "DISABLE_NOTIFY is enabled"}
    else:
        try:
            notify_result = post_json(notify_url, ingest_secret, {"limit": 100})
        except Exception as err:  # noqa: BLE001
            notify_result = {"error": str(err)}
            print(f"notify call failed: {err}", file=sys.stderr)

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

    if ingest_errors:
        raise RuntimeError(
            f"{len(ingest_errors)} of "
            f"{(len(listings) + ingest_chunk_size - 1) // ingest_chunk_size} ingest chunk(s) failed: "
            f"{'; '.join(ingest_errors[:3])}"
        )
    if isinstance(notify_result, dict) and notify_result.get("error"):
        raise RuntimeError(f"notify call failed: {notify_result['error']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"run_cycle failed: {exc}", file=sys.stderr)
        raise
