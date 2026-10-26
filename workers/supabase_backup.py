"""Supabase backup + Telegram failover for the worker cycles.

Every cycle mirrors its scraped listings into Supabase (screened with a Python
port of convex/lib/screening.ts). Convex stays the primary notifier; when the
Convex ingest or notify call fails, the worker sends the Telegram alerts itself
and records them in Supabase so the same listing is never alerted twice by the
fallback.

Env:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   enable the backup (skipped if unset)
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID      needed for fallback notifications
    SUPABASE_NOTIFY_MODE                      fallback (default) | always | off
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

NOTIFY_MAX_ATTEMPTS = 5
UPSERT_CHUNK_SIZE = 200
MAX_FUTURE_SKEW_MS = 5 * 60 * 1000

# --- Screening (keep in sync with convex/lib/screening.ts) -----------------


def _env_int(name: str, fallback: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except ValueError:
        return fallback


def _env_patterns(name: str, fallback: List[str]) -> List[str]:
    raw = os.getenv(name)
    if not raw:
        return fallback
    parts = [p.strip().lower() for p in raw.replace("\n", ",").split(",")]
    parts = [p for p in parts if p]
    return parts or fallback


DEFAULT_VINTED_TARGET_MODEL_PATTERNS = [
    "r1", "r2", "r3", "torrentshell", "h2no", "goretex", "gore-tex",
    "ski jacket", "jacket", "fleece", "synchilla", "snap-t", "snap t",
    "better sweater", "nano puff", "micro puff", "puffer", "sweater", "jumper",
    "capilene", "retro-x", "retro x", "das parka", "down sweater", "vest",
    "gilet", "parka", "pile fleece", "black hole", "duffel",
]

FACEBOOK_TARGET_PATTERNS = [
    "captain's chair",
    "captains chair",
    "captain chair",
    "chesterfield captain chair",
]

ALLOWED_CONDITION_PATTERNS = [
    "new with tags", "new without tags", "very good", "good condition", "good",
    "6", "1", "2", "3",  # Vinted status ids
]

ALLOWED_CATEGORY_PATTERNS = [
    "fleece", "jumper", "sweatshirt", "hoodie", "jackets", "clothes", "tops",
    "men", "women", "activewear", "sports", "outdoor",
]


def _brand_price_limits() -> List[Tuple[str, int]]:
    return [("patagonia", _env_int("VINTED_MAX_PRICE_MINOR", 5000))]


def _includes_any(value: Optional[str], patterns: Iterable[str]) -> bool:
    if not value:
        return False
    hay = value.lower()
    return any(p in hay for p in patterns)


def _freshness(age_minutes: int, hot_minutes: int) -> str:
    if age_minutes < hot_minutes:
        return "HOT"
    if age_minutes < 60:
        return "NEW"
    if age_minutes < 24 * 60:
        return "RECENT"
    return "STALE"


def _age_minutes(published_at_ms: Optional[int], now_ms: int) -> int:
    if published_at_ms is None:
        return 0
    return max(0, (now_ms - published_at_ms) // 60000)


def _screen_vinted(listing: Dict[str, Any], now_ms: int) -> Dict[str, Any]:
    reasons: List[str] = []
    brand_limits = _brand_price_limits()

    title_brand = f"{listing.get('brand') or ''} {listing['title']}".lower()
    matched_brand = next((b for b in brand_limits if b[0] in title_brand), None)
    brand_ok = matched_brand is not None
    if not brand_ok:
        reasons.append("brand_not_target")

    target_blob = (
        f"{listing.get('brand') or ''} {listing['title']} {listing.get('description') or ''}"
    ).lower()
    model_ok = _includes_any(
        target_blob,
        _env_patterns("VINTED_TARGET_TERMS", DEFAULT_VINTED_TARGET_MODEL_PATTERNS),
    )
    if not model_ok:
        reasons.append("missing_target_model")
        if re.search(r"\bregulator\b", target_blob):
            reasons.append("regulator_without_target_model")

    category_ok = _includes_any(listing.get("category"), ALLOWED_CATEGORY_PATTERNS)
    if not category_ok:
        reasons.append("category_not_allowed")

    condition_ok = _includes_any(listing.get("condition"), ALLOWED_CONDITION_PATTERNS)
    if not condition_ok:
        reasons.append("condition_not_allowed")

    age = _age_minutes(listing.get("publishedAtMs"), now_ms)
    in_24h = age < 24 * 60
    if not in_24h:
        reasons.append("outside_24h_window")

    max_price = matched_brand[1] if matched_brand else brand_limits[0][1]
    price = listing.get("priceMinor")
    price_ok = (
        (listing.get("currency") or "").upper() == "GBP"
        and isinstance(price, (int, float))
        and price <= max_price
    )
    if not price_ok:
        reasons.append("price_or_currency_not_allowed")

    bucket = _freshness(age, 10)
    score = {"HOT": 60, "NEW": 40, "RECENT": 20}.get(bucket, 0)
    if isinstance(price, (int, float)):
        score += min(30, int(max(0, max_price - price) // 100))
    if condition_ok:
        score += 10

    is_match = brand_ok and model_ok and category_ok and condition_ok and in_24h and price_ok
    return {
        "isMatch": is_match,
        "reasons": reasons,
        "score": score,
        "freshnessBucket": bucket,
        "ageMinutes": age,
        "eligibleForNotify": is_match and bucket in ("HOT", "NEW"),
    }


def _screen_facebook(listing: Dict[str, Any], now_ms: int) -> Dict[str, Any]:
    reasons: List[str] = []
    target_blob = f"{listing['title']} {listing.get('description') or ''}".lower()
    has_target = _includes_any(target_blob, FACEBOOK_TARGET_PATTERNS)
    if not has_target:
        reasons.append("missing_target_model")

    age = _age_minutes(listing.get("publishedAtMs"), now_ms)
    in_24h = age < 24 * 60
    if not in_24h:
        reasons.append("outside_24h_window")
    within_notify_window = age <= 5
    if not within_notify_window:
        reasons.append("outside_5m_notify_window")

    bucket = _freshness(age, 5)
    score = {"HOT": 70, "NEW": 40, "RECENT": 20}.get(bucket, 0)
    is_match = has_target and in_24h
    return {
        "isMatch": is_match,
        "reasons": reasons,
        "score": score,
        "freshnessBucket": bucket,
        "ageMinutes": age,
        "eligibleForNotify": is_match and within_notify_window,
    }


def screen_listing(listing: Dict[str, Any], now_ms: Optional[int] = None) -> Dict[str, Any]:
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if listing.get("source") == "facebook_marketplace_uk":
        return _screen_facebook(listing, now_ms)
    return _screen_vinted(listing, now_ms)


# --- Normalisation (port of convex/lib/normalize.ts) ------------------------


def _parse_ts_ms(value: Any) -> Optional[int]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)
    if ms > int(time.time() * 1000) + MAX_FUTURE_SKEW_MS:
        return None
    return ms


def _clean_str(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    out = value.strip()
    return out or None


def _clean_num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _clean_int(value: Any) -> Optional[int]:
    n = _clean_num(value)
    return int(n) if n is not None else None


def _iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def normalize_listing(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    source = _clean_str(raw.get("source"))
    listing_id = _clean_str(str(raw.get("listingId") or ""))
    title = _clean_str(raw.get("title"))
    url = _clean_str(raw.get("url"))
    if not (source and listing_id and title and url):
        return None
    description = _clean_str(raw.get("description"))
    currency = _clean_str(raw.get("currency"))
    return {
        "source": source,
        "listingId": listing_id,
        "url": url,
        "title": title,
        "brand": _clean_str(raw.get("brand")),
        "priceMinor": _clean_int(raw.get("priceMinor")),
        "currency": currency.upper() if currency else None,
        "size": _clean_str(raw.get("size")),
        "condition": _clean_str(raw.get("condition")),
        "category": _clean_str(raw.get("category")),
        "publishedAtMs": _parse_ts_ms(raw.get("publishedAt")),
        "fetchedAtMs": _parse_ts_ms(raw.get("fetchedAt")) or int(time.time() * 1000),
        "location": _clean_str(raw.get("location")),
        "views": _clean_int(raw.get("views")),
        "interested": _clean_int(raw.get("interested")),
        "description": description[:500] if description else None,
        "imageUrl": _clean_str(raw.get("imageUrl")),
    }


# --- Telegram (same format as convex/notifyTelegram.ts) ---------------------


def _source_label(source: str) -> str:
    if source == "facebook_marketplace_uk":
        return "Facebook Marketplace"
    if source == "vinted_uk":
        return "Vinted UK"
    return source


def _format_age(age_minutes: int) -> str:
    if age_minutes < 60:
        return f"{age_minutes}m"
    days, rem = divmod(age_minutes, 24 * 60)
    hours, minutes = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


def format_alert(listing: Dict[str, Any], match: Dict[str, Any]) -> str:
    price_minor = listing.get("priceMinor")
    price = (
        f"{price_minor / 100:.2f} {listing.get('currency') or 'GBP'}"
        if isinstance(price_minor, (int, float))
        else "unknown"
    )
    return "\n".join(
        [
            f"[{match['freshnessBucket']}] {_source_label(listing['source'])}",
            listing["title"],
            f"Price: {price}",
            f"Condition: {listing.get('condition') or 'unknown'}",
            f"Size: {listing.get('size') or 'unknown'}",
            f"Age: {_format_age(match['ageMinutes'])}",
            f"Score: {match['score']}",
            listing["url"],
        ]
    )


def send_telegram(token: str, chat_id: str, text: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": False}),
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"Telegram send failed: {response.status_code} {response.text[:500]}")


# --- Supabase REST -----------------------------------------------------------


class SupabaseClient:
    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = requests.request(
                    method, f"{self.base}/{path}", headers={**self.headers, **kwargs.pop("headers", {})},
                    timeout=30, **kwargs,
                )
                if response.status_code < 500:
                    if not response.ok:
                        raise RuntimeError(
                            f"Supabase {method} {path} -> {response.status_code}: {response.text[:500]}"
                        )
                    return response
                last_error = RuntimeError(f"Supabase {method} {path} -> {response.status_code}")
            except requests.RequestException as err:
                last_error = err
            time.sleep(2 * (attempt + 1))
        raise RuntimeError(str(last_error))

    def upsert(self, table: str, rows: List[Dict[str, Any]], on_conflict: str) -> None:
        for i in range(0, len(rows), UPSERT_CHUNK_SIZE):
            self._request(
                "POST",
                f"{table}?on_conflict={on_conflict}",
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                data=json.dumps(rows[i : i + UPSERT_CHUNK_SIZE]),
            )

    def select(self, table: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
        return self._request("GET", table, params=params).json()

    def insert_returning(self, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
        response = self._request(
            "POST", table, headers={"Prefer": "return=representation"}, data=json.dumps(row)
        )
        return response.json()[0]

    def update(self, table: str, match: Dict[str, str], values: Dict[str, Any]) -> None:
        self._request(
            "PATCH", table, params=match, headers={"Prefer": "return=minimal"}, data=json.dumps(values)
        )


def _listing_row(listing: Dict[str, Any], match: Dict[str, Any], now_iso: str) -> Dict[str, Any]:
    return {
        "source": listing["source"],
        "listing_id": listing["listingId"],
        "url": listing["url"],
        "title": listing["title"],
        "brand": listing["brand"],
        "price_minor": listing["priceMinor"],
        "currency": listing["currency"],
        "size": listing["size"],
        "condition": listing["condition"],
        "category": listing["category"],
        "published_at": _iso(listing["publishedAtMs"]),
        "fetched_at": _iso(listing["fetchedAtMs"]),
        "location": listing["location"],
        "views": listing["views"],
        "interested": listing["interested"],
        "description": listing["description"],
        "image_url": listing["imageUrl"],
        "is_match": match["isMatch"],
        "reasons": match["reasons"],
        "score": match["score"],
        "freshness_bucket": match["freshnessBucket"],
        "age_minutes": match["ageMinutes"],
        "eligible_for_notify": match["eligibleForNotify"],
        "updated_at": now_iso,
    }


def _max_notify_age_minutes(source: str) -> int:
    # Mirrors convex/jobs.ts getPendingMatches.
    return 5 if source == "facebook_marketplace_uk" else 60


def _send_fallback_alerts(
    client: SupabaseClient,
    eligible: List[Tuple[Dict[str, Any], Dict[str, Any]]],
) -> Dict[str, Any]:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return {"error": "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; cannot send fallback alerts"}

    sent = failed = skipped = 0
    errors: List[str] = []
    keys = [f"{l['source']}:{l['listingId']}" for l, _ in eligible]
    existing: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(keys), 50):
        batch = keys[i : i + 50]
        in_list = ",".join(f'"{k}"' for k in batch)
        for row in client.select(
            "notifications",
            {"select": "idempotency_key,status,attempts", "idempotency_key": f"in.({in_list})"},
        ):
            existing[row["idempotency_key"]] = row

    now_iso = datetime.now(timezone.utc).isoformat()
    for (listing, match), key in zip(eligible, keys):
        prior = existing.get(key)
        if prior and (prior["status"] == "sent" or prior["attempts"] >= NOTIFY_MAX_ATTEMPTS):
            skipped += 1
            continue
        try:
            send_telegram(token, chat_id, format_alert(listing, match))
            status, last_error = "sent", None
            sent += 1
        except Exception as err:  # noqa: BLE001
            status, last_error = "failed", str(err)
            failed += 1
            errors.append(str(err))
        client.upsert(
            "notifications",
            [{
                "idempotency_key": key,
                "source": listing["source"],
                "listing_id": listing["listingId"],
                "status": status,
                "attempts": (prior["attempts"] + 1) if prior else 1,
                "sent_at": now_iso if status == "sent" else None,
                "last_error": last_error,
                "updated_at": now_iso,
            }],
            on_conflict="idempotency_key",
        )
    return {"sent": sent, "failed": failed, "skipped": skipped, "errors": errors[:3]}


def run_supabase_backup(
    kind: str,
    raw_listings: List[Dict[str, Any]],
    convex_ok: bool,
    disable_notify: bool,
) -> Dict[str, Any]:
    """Mirror listings into Supabase and, if Convex failed, send alerts directly.

    Never raises: returns a result dict with an "error" key instead, so a
    Supabase problem can't break the primary Convex path.
    """
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return {"enabled": False, "reason": "SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY not set"}

    notify_mode = (os.getenv("SUPABASE_NOTIFY_MODE") or "fallback").strip().lower()
    client = SupabaseClient("".join(url.split()), "".join(key.split()))
    result: Dict[str, Any] = {"enabled": True, "notify_mode": notify_mode}
    run_id: Optional[int] = None

    try:
        run_id = client.insert_returning("runs", {"kind": kind, "convex_ok": convex_ok})["id"]
    except Exception as err:  # noqa: BLE001
        print(f"supabase run record failed: {err}", file=sys.stderr)

    try:
        now_ms = int(time.time() * 1000)
        now_iso = datetime.now(timezone.utc).isoformat()
        screened: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for raw in raw_listings:
            listing = normalize_listing(raw)
            if listing is not None:
                screened.append((listing, screen_listing(listing, now_ms)))

        client.upsert(
            "listings",
            [_listing_row(l, m, now_iso) for l, m in screened],
            on_conflict="source,listing_id",
        )

        eligible = [
            (l, m)
            for l, m in screened
            if m["eligibleForNotify"]
            and m["freshnessBucket"] != "STALE"
            and m["ageMinutes"] <= _max_notify_age_minutes(l["source"])
        ]
        result.update(
            upserted=len(screened),
            matched=sum(1 for _, m in screened if m["isMatch"]),
            eligible=len(eligible),
        )

        fallback = notify_mode == "always" or (notify_mode == "fallback" and not convex_ok)
        result["fallback_notify"] = fallback
        if disable_notify:
            result["notify"] = {"disabled": True}
        elif fallback and eligible:
            result["notify"] = _send_fallback_alerts(client, eligible)
        else:
            result["notify"] = None
    except Exception as err:  # noqa: BLE001
        result["error"] = str(err)
        print(f"supabase backup failed: {err}", file=sys.stderr)

    if run_id is not None:
        notify = result.get("notify") or {}
        try:
            client.update(
                "runs",
                {"id": f"eq.{run_id}"},
                {
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                    "scraped": len(raw_listings),
                    "upserted": result.get("upserted", 0),
                    "matched": result.get("matched", 0),
                    "eligible": result.get("eligible", 0),
                    "sent": notify.get("sent", 0),
                    "failed": notify.get("failed", 0),
                    "skipped": notify.get("skipped", 0),
                    "fallback_notify": bool(result.get("fallback_notify")),
                    "error": result.get("error") or notify.get("error"),
                },
            )
        except Exception as err:  # noqa: BLE001
            print(f"supabase run update failed: {err}", file=sys.stderr)

    if isinstance(result.get("notify"), dict) and result["notify"].get("error"):
        result["error"] = result.get("error") or result["notify"]["error"]
    return result


def supabase_configured() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
