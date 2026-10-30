"""Supabase store + Telegram notifier for the worker cycles.

Every cycle upserts its scraped listings into Supabase (screened with a Python
port of convex/lib/screening.ts) and sends a Telegram photo alert for each
eligible listing. Supabase's notifications table is the single dedupe record:
a listing is claimed there before its alert is sent, so it is alerted at most
once even if a run is cancelled mid-send. Convex is no longer used.

Env:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   required
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID      required to send alerts
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
TELEGRAM_CAPTION_LIMIT = 1024
IMAGE_MAX_BYTES = 10 * 1024 * 1024
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
}
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


def _telegram(token: str, method: str, **kwargs: Any) -> None:
    response = requests.post(f"https://api.telegram.org/bot{token}/{method}", timeout=30, **kwargs)
    if not response.ok:
        raise RuntimeError(f"Telegram {method} failed: {response.status_code} {response.text[:500]}")


def find_page_image(url: str) -> Optional[str]:
    """og:image of the listing page, for listings scraped without a photo."""
    try:
        response = requests.get(url, headers={**BROWSER_HEADERS, "Accept": "text/html"}, timeout=15)
    except requests.RequestException:
        return None
    if not response.ok:
        return None
    m = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', response.text
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', response.text
    )
    return m.group(1).replace("&amp;", "&") if m else None


def download_image(url: str) -> Tuple[bytes, str]:
    """Fetch the image ourselves (marketplace CDNs often refuse Telegram's
    fetcher) and convert formats Telegram won't show as a photo, e.g. WebP."""
    response = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
    response.raise_for_status()
    data = response.content
    if not data or len(data) > IMAGE_MAX_BYTES:
        raise RuntimeError(f"image unusable ({len(data)} bytes)")
    if data[:3] == b"\xff\xd8\xff":
        return data, "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data, "image/png"
    from io import BytesIO

    from PIL import Image

    out = BytesIO()
    Image.open(BytesIO(data)).convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue(), "image/jpeg"


def send_alert(token: str, chat_id: str, listing: Dict[str, Any], match: Dict[str, Any]) -> str:
    """Send the alert as a photo with the details as its caption.

    Returns "photo", or "text" if no image could be found or delivered (the
    text alert is still sent so a listing is never silently dropped).
    """
    caption = format_alert(listing, match)
    image_url = listing.get("imageUrl") or find_page_image(listing["url"])
    if image_url:
        try:
            data, mime = download_image(image_url)
            _telegram(
                token,
                "sendPhoto",
                data={"chat_id": chat_id, "caption": caption[:TELEGRAM_CAPTION_LIMIT]},
                files={"photo": ("listing.jpg" if mime == "image/jpeg" else "listing.png", data, mime)},
            )
            return "photo"
        except Exception as err:  # noqa: BLE001
            print(f"photo upload failed for {listing['url']}: {err}", file=sys.stderr)
        try:
            _telegram(
                token,
                "sendPhoto",
                json={"chat_id": chat_id, "photo": image_url, "caption": caption[:TELEGRAM_CAPTION_LIMIT]},
            )
            return "photo"
        except Exception as err:  # noqa: BLE001
            print(f"photo by URL failed for {listing['url']}: {err}", file=sys.stderr)
    _telegram(
        token,
        "sendMessage",
        json={"chat_id": chat_id, "text": f"{caption}\n(no image available)", "disable_web_page_preview": False},
    )
    return "text"


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

    def insert_if_absent(self, table: str, row: Dict[str, Any], on_conflict: str) -> bool:
        """Insert row unless its key exists; True if this call inserted it."""
        response = self._request(
            "POST",
            f"{table}?on_conflict={on_conflict}",
            headers={"Prefer": "resolution=ignore-duplicates,return=representation"},
            data=json.dumps([row]),
        )
        return bool(response.json())

    def update_returning(
        self, table: str, match: Dict[str, str], values: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        return self._request(
            "PATCH", table, params=match, headers={"Prefer": "return=representation"}, data=json.dumps(values)
        ).json()


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


def _claim(client: SupabaseClient, key: str, listing: Dict[str, Any], now_iso: str) -> bool:
    """Reserve a listing's alert before sending it.

    The claimed row is written as status 'sent' with no sent_at; sent_at is
    filled in once Telegram accepts the message. A run killed between claim
    and send therefore never resends (at-most-once), which is what stops the
    duplicate alerts. Previously failed alerts are re-claimed for a retry.
    """
    row = {
        "idempotency_key": key,
        "source": listing["source"],
        "listing_id": listing["listingId"],
        "status": "sent",
        "attempts": 1,
        "sent_at": None,
        "last_error": None,
        "updated_at": now_iso,
    }
    if client.insert_if_absent("notifications", row, on_conflict="idempotency_key"):
        return True
    for prior in client.select(
        "notifications",
        {"select": "status,attempts", "idempotency_key": f"eq.{key}", "status": "eq.failed"},
    ):
        if prior["attempts"] >= NOTIFY_MAX_ATTEMPTS:
            return False
        return bool(client.update_returning(
            "notifications",
            {"idempotency_key": f"eq.{key}", "status": "eq.failed", "attempts": f"eq.{prior['attempts']}"},
            {"status": "sent", "attempts": prior["attempts"] + 1, "last_error": None, "updated_at": now_iso},
        ))
    return False


def _send_alerts(
    client: SupabaseClient,
    eligible: List[Tuple[Dict[str, Any], Dict[str, Any]]],
) -> Dict[str, Any]:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return {"error": "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; cannot send alerts"}

    sent = failed = skipped = without_image = 0
    errors: List[str] = []
    for listing, match in eligible:
        key = f"{listing['source']}:{listing['listingId']}"
        now_iso = datetime.now(timezone.utc).isoformat()
        if not _claim(client, key, listing, now_iso):
            skipped += 1
            continue
        try:
            if send_alert(token, chat_id, listing, match) == "text":
                without_image += 1
            values: Dict[str, Any] = {"sent_at": now_iso}
            sent += 1
        except Exception as err:  # noqa: BLE001
            values = {"status": "failed", "last_error": str(err)[:1000]}
            failed += 1
            errors.append(str(err))
        try:
            client.update("notifications", {"idempotency_key": f"eq.{key}"}, {**values, "updated_at": now_iso})
        except Exception as err:  # noqa: BLE001
            print(f"notification record update failed for {key}: {err}", file=sys.stderr)
    return {
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "without_image": without_image,
        "errors": errors[:3],
    }


def run_supabase(
    kind: str,
    raw_listings: List[Dict[str, Any]],
    disable_notify: bool,
) -> Dict[str, Any]:
    """Store listings in Supabase and send Telegram alerts for eligible ones.

    Never raises: returns a result dict with an "error" key instead.
    """
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return {"enabled": False, "error": "SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY not set"}

    client = SupabaseClient("".join(url.split()), "".join(key.split()))
    result: Dict[str, Any] = {"enabled": True}
    run_id: Optional[int] = None

    try:
        run_id = client.insert_returning("runs", {"kind": kind})["id"]
    except Exception as err:  # noqa: BLE001
        print(f"supabase run record failed: {err}", file=sys.stderr)

    try:
        now_ms = int(time.time() * 1000)
        now_iso = datetime.now(timezone.utc).isoformat()
        screened: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for raw in raw_listings:
            listing = normalize_listing(raw)
            if listing is not None:
                watches = raw.get("_watches")
                if watches and listing["source"] == "vinted_uk":
                    from vinted_watchlist import screen_watch
                    matches = [screen_listing(listing, now_ms) if w.get("legacy")
                               else screen_watch(listing, w, now_ms) for w in watches]
                    match = next((m for m in matches if m["eligibleForNotify"]),
                                 next((m for m in matches if m["isMatch"]), matches[0]))
                else:
                    match = screen_listing(listing, now_ms)
                screened.append((listing, match))

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

        if disable_notify:
            result["notify"] = {"disabled": True}
        elif eligible:
            result["notify"] = _send_alerts(client, eligible)
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
                    "error": result.get("error") or notify.get("error"),
                },
            )
        except Exception as err:  # noqa: BLE001
            print(f"supabase run update failed: {err}", file=sys.stderr)

    if isinstance(result.get("notify"), dict) and result["notify"].get("error"):
        result["error"] = result.get("error") or result["notify"]["error"]
    return result
