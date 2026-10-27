import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic, sleep
from typing import Any, Dict, List, Optional

try:
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
except Exception:  # pragma: no cover
    webdriver = None
    TimeoutException = Exception
    Options = None
    By = None
    EC = None
    WebDriverWait = None
try:
    from tenacity import retry, stop_after_attempt, wait_exponential
except Exception:  # pragma: no cover
    def retry(*_args, **_kwargs):
        def decorator(fn):
            return fn

        return decorator

    def stop_after_attempt(_attempts):
        return None

    def wait_exponential(**_kwargs):
        return None

DICT_ORDER = {
    "relevance": "order=relevance",
    "decreasing price": "order=price_high_to_low",
    "increasing price": "order=price_low_to_high",
    "most recent first": "order=newest_first",
}

DICT_CATALOG = {
    "men": "catalog[]=5",
    "women": "catalog[]=1904",
    "children": "catalog[]=1193",
}

DICT_CONDITION = {
    "new with tags": "status[]=6",
    "new without tags": "status[]=1",
    "very good condition": "status[]=2",
    "good condition": "status[]=3",
    "satisfactory": "status[]=4",
}

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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


EMBEDDED_ITEMS_MARKER = '\\"items\\":{\\"items\\":['
EMBEDDED_ITEMS_END = '],\\"pagination\\":'

_AGE_UNITS_MINUTES = {
    "sec": 0, "second": 0, "min": 1, "minute": 1, "hour": 60, "hr": 60,
    "day": 1440, "week": 10080, "month": 43200, "year": 525600,
}

# The catalog payload has no timestamps, so ages come from item pages, and
# Vinted rate-limits (HTTP 429) quickly. Alerts only fire for items < 60 min
# old, so only items newer than this cutoff are kept and age-checked.
AGE_CHECK_CUTOFF_MINUTES = int(os.getenv("VINTED_AGE_CHECK_MINUTES", "90"))
ITEM_PAGE_DELAY_SECONDS = float(os.getenv("VINTED_ITEM_PAGE_DELAY_SECONDS", "1.5"))

# Vinted item ids increase with creation time. Once an item with id N is known
# to be older than the cutoff, every item with id <= N is too, so its page
# never needs to be opened. Shared across the per-query scraper instances.
_stale_id_floor = 0
# Set after repeated failed item-page reads (rate limiting): stop for this run.
_item_pages_blocked = False


def decode_embedded_items(html: str) -> Optional[List[Dict]]:
    """Pull the catalog items array out of the escaped Next.js payload."""
    start = html.find(EMBEDDED_ITEMS_MARKER)
    if start == -1:
        return None
    start += len(EMBEDDED_ITEMS_MARKER)
    end = html.find(EMBEDDED_ITEMS_END, start)
    if end == -1:
        return None
    escaped = "[" + html[start:end] + "]"
    try:
        # The array sits inside a JS string literal: unescape it as one so
        # non-ASCII text (e.g. "·", "£") survives intact.
        items = json.loads(json.loads('"' + escaped + '"'))
    except Exception:
        try:
            items = json.loads(escaped.encode("utf-8").decode("unicode_escape"))
        except Exception:
            return None
    return items if isinstance(items, list) else None


def parse_relative_age_minutes(text: str) -> Optional[int]:
    """'4 min ago' -> 4, 'an hour ago' -> 60, 'yesterday' -> 1440."""
    value = (text or "").strip().lower()
    if not value:
        return None
    if value in {"just now", "now", "a moment ago", "moments ago"}:
        return 0
    if value == "yesterday":
        return 1440
    match = re.search(
        r"(\d+|an?|one)\s*(sec|second|min|minute|hour|hr|day|week|month|year)s?\b.*ago", value
    )
    if not match:
        return None
    count = 1 if match.group(1) in {"a", "an", "one"} else int(match.group(1))
    return count * _AGE_UNITS_MINUTES[match.group(2)]


def extract_upload_age_minutes(html: str) -> Optional[int]:
    patterns = [
        r'upload_date\\",\\"data\\":\{\\"title\\":\\"[^"\\]*\\",\\"value\\":\\"([^"\\]+)\\"',
        r'itemProp="upload_date"><span[^>]*>([^<]+)<',
        r'"code":"upload_date","data":\{"title":"[^"]*","value":"([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            minutes = parse_relative_age_minutes(match.group(1))
            if minutes is not None:
                return minutes
    return None


def _photo_url(photo: Any) -> Optional[str]:
    """Best image URL from a Vinted photo object (old and 2026 catalog formats)."""
    if isinstance(photo, str):
        return photo or None
    if not isinstance(photo, dict):
        return None
    for key in ("url", "fullSizeUrl", "full_size_url", "imageUrl"):
        if isinstance(photo.get(key), str) and photo[key]:
            return photo[key]
    thumbnails = photo.get("thumbnails") or []
    urls = [t.get("url") for t in thumbnails if isinstance(t, dict) and t.get("url")]
    return urls[-1] if urls else None


def _og_image(html: str) -> Optional[str]:
    m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html) or re.search(
        r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', html
    )
    return m.group(1).replace("&amp;", "&") if m else None


def _label_value(label: str, key: str) -> Optional[str]:
    match = re.search(rf"\b{key}:\s*([^,]+)", label or "")
    return match.group(1).strip() if match else None


def _to_minor_gbp(price_text: str) -> Optional[int]:
    if not price_text:
        return None
    cleaned = re.sub(r"[^0-9.,]", "", price_text)
    cleaned = cleaned.replace(",", "")
    try:
        return int(round(float(cleaned) * 100))
    except ValueError:
        return None


def define_criteria_query(
    order: Optional[List[str]] = None,
    catalog: Optional[List[str]] = None,
    condition: Optional[List[str]] = None,
) -> str:
    params: List[str] = []
    for key in order or []:
        if key in DICT_ORDER:
            params.append(DICT_ORDER[key])
    for key in catalog or []:
        if key in DICT_CATALOG:
            params.append(DICT_CATALOG[key])
    for key in condition or []:
        if key in DICT_CONDITION:
            params.append(DICT_CONDITION[key])
    return "&".join(params)


def build_search_url(query: str, criteria: str, page: int) -> str:
    encoded_q = urllib.parse.quote(query)
    base = f"https://www.vinted.co.uk/catalog?search_text={encoded_q}&page={page}"
    return f"{base}&{criteria}" if criteria else base


@dataclass
class ScrapeConfig:
    query: str = "patagonia r1"
    max_pages: int = 3
    order: str = "most recent first"
    catalog: str = "men"
    strict_target_only: bool = True
    target_terms: List[str] = field(default_factory=lambda: DEFAULT_TARGET_TERMS.copy())
    page_load_timeout_seconds: int = 20
    max_runtime_seconds: int = 240
    max_item_age_hours: int = 2
    max_price_gbp: float = 20.0


class VintedScraper:
    def __init__(self, page_load_timeout_seconds: int = 20) -> None:
        if webdriver is None or Options is None or WebDriverWait is None:
            raise RuntimeError(
                "selenium is not installed in this environment. "
                "Install workers/requirements.txt to run scraper."
            )
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-sync")
        options.add_argument("--no-first-run")
        options.add_argument("--window-size=1280,800")
        options.add_argument("--js-flags=--max-old-space-size=256")
        options.add_argument("--blink-settings=imagesEnabled=false")
        self.driver = webdriver.Chrome(options=options)
        self.driver.set_page_load_timeout(page_load_timeout_seconds)
        self.driver.set_script_timeout(page_load_timeout_seconds)
        self.wait = WebDriverWait(self.driver, 20)
        self.stats = {
            "pages_scanned": 0,
            "page_link_count": 0,
            "listing_errors": 0,
            "listing_pages_scraped": 0,
            "listing_skipped_not_target": 0,
            "listing_skipped_missing_published_at": 0,
            "listing_skipped_stale_24h": 0,
            "listing_skipped_over_price": 0,
        }

    def close(self) -> None:
        self.driver.quit()

    @retry(wait=wait_exponential(multiplier=1, min=1, max=8), stop=stop_after_attempt(3))
    def _extract_listing_urls(self, search_url: str) -> List[str]:
        try:
            self.driver.get(search_url)
        except TimeoutException:
            # Continue parsing whatever HTML was received before timeout.
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                pass
        try:
            self.wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="/items/"]'))
            )
        except TimeoutException:
            pass

        links = self.driver.find_elements(By.CSS_SELECTOR, 'a[href*="/items/"]')
        out = []
        seen = set()
        for link in links:
            href = link.get_attribute("href")
            if not href:
                continue
            href = href.split("?")[0]
            if href in seen:
                continue
            seen.add(href)
            out.append(href)
        if out:
            self.stats["page_link_count"] += len(out)
            return out
        return self._extract_listing_urls_from_embedded_items(self.driver.page_source)

    def _extract_listing_urls_from_embedded_items(self, html: str) -> List[str]:
        items = decode_embedded_items(html) or []
        out: List[str] = []
        seen = set()
        for item in items:
            path = item.get("path") or (item.get("productItem") or {}).get("url")
            if not isinstance(path, str) or not path.startswith("/items/"):
                continue
            href = f"https://www.vinted.co.uk{path}"
            if href in seen:
                continue
            seen.add(href)
            out.append(href)
        self.stats["page_link_count"] += len(out)
        return out

    def _extract_embedded_items(self, search_url: str) -> List[Dict]:
        try:
            self.driver.get(search_url)
        except TimeoutException:
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                return []
        return self._extract_embedded_items_from_html(self.driver.page_source)

    def _extract_embedded_items_from_html(self, html: str) -> List[Dict]:
        items = decode_embedded_items(html)
        if not items:
            return []
        self.stats["page_link_count"] += len(items)
        return items

    def _fetch_upload_age_minutes(self, url: str) -> Optional[int]:
        """Upload age from the item page; also keeps its og:image in _last_page_image."""
        self.stats["listing_pages_scraped"] += 1
        self._last_page_image: Optional[str] = None
        try:
            self.driver.get(url)
        except TimeoutException:
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                return None
        except Exception:
            return None
        try:
            page_source = self.driver.page_source
            self._last_page_image = _og_image(page_source)
            return extract_upload_age_minutes(page_source)
        except Exception:
            return None

    def _extract_listing_id(self, url: str) -> str:
        match = re.search(r"/items/(\d+)", url)
        if match:
            return match.group(1)
        slug = url.rstrip("/").split("/")[-1]
        return slug.split("-")[0]

    @retry(wait=wait_exponential(multiplier=1, min=1, max=8), stop=stop_after_attempt(3))
    def scrape_listing(self, url: str) -> Optional[Dict]:
        try:
            self.driver.get(url)
        except TimeoutException:
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                return None
        time_el = self.driver.find_elements(By.CSS_SELECTOR, "time[datetime]")
        published_at = None
        if time_el:
            published_at = time_el[0].get_attribute("datetime")

        title = ""
        title_el = self.driver.find_elements(By.CSS_SELECTOR, "h1")
        if title_el:
            title = title_el[0].text.strip()

        page_text = self.driver.page_source

        def extract_value(label: str) -> Optional[str]:
            pattern = re.compile(rf"{label}</[^>]+>\s*<[^>]+>([^<]+)<", re.IGNORECASE)
            m = pattern.search(page_text)
            return m.group(1).strip() if m else None

        price = None
        price_el = self.driver.find_elements(By.CSS_SELECTOR, '[data-testid*="price"]')
        if price_el:
            price = price_el[0].text.strip()
        if not price:
            m = re.search(r'"price"\s*:\s*"([0-9.,]+)"', page_text)
            price = m.group(1) if m else ""

        image_url = None
        image_el = self.driver.find_elements(By.CSS_SELECTOR, 'meta[property="og:image"]')
        if image_el:
            image_url = image_el[0].get_attribute("content")

        description = None
        desc_el = self.driver.find_elements(By.CSS_SELECTOR, 'meta[property="og:description"]')
        if desc_el:
            description = desc_el[0].get_attribute("content")

        record = {
            "source": "vinted_uk",
            "listingId": self._extract_listing_id(url),
            "url": url,
            "title": title or "(untitled)",
            "brand": extract_value("Brand") or extract_value("BRAND"),
            "priceMinor": _to_minor_gbp(price or ""),
            "currency": "GBP",
            "size": extract_value("Size") or extract_value("SIZE"),
            "condition": extract_value("Condition") or extract_value("CONDITION"),
            "category": extract_value("Category") or "clothes",
            "publishedAt": published_at,
            "fetchedAt": _utc_now_iso(),
            "location": extract_value("Location") or extract_value("LOCATION"),
            "views": None,
            "interested": None,
            "description": description,
            "imageUrl": image_url,
        }

        if not record["listingId"] or not record["url"]:
            return None
        return record

    def _is_target_match(self, item: Dict, target_terms: List[str], query: str = "") -> bool:
        brand = str(item.get("brand") or "").lower()
        title = str(item.get("title") or "").lower()
        description = str(item.get("description") or "").lower()
        text = f"{brand} {title} {description}"

        if "patagonia" in query.lower() and "patagonia" not in text:
            return False

        normalized_text = re.sub(r"[^a-z0-9]+", " ", text).strip()
        for term in target_terms:
            normalized_term = re.sub(r"[^a-z0-9]+", " ", term.lower()).strip()
            if normalized_term and normalized_term in normalized_text:
                return True
        return False

    def _record_from_embedded_item(self, item: Dict) -> Optional[Dict]:
        if isinstance(item.get("productItem"), dict):
            return self._record_from_product_item(item)
        listing_id = item.get("id")
        path = item.get("path")
        if listing_id is None or not isinstance(path, str):
            return None

        price = item.get("price") or {}
        amount = price.get("amount")
        currency = price.get("currency_code") or "GBP"

        published_ts = (
            item.get("created_at_ts")
            or item.get("updated_at_ts")
            or ((item.get("photo") or {}).get("high_resolution") or {}).get("timestamp")
        )
        published_at = None
        if isinstance(published_ts, (int, float)):
            published_at = datetime.fromtimestamp(published_ts, timezone.utc).isoformat()

        status = item.get("status")
        return {
            "source": "vinted_uk",
            "listingId": str(listing_id),
            "url": f"https://www.vinted.co.uk{path}" if path.startswith("/") else path,
            "title": item.get("title") or "(untitled)",
            "brand": item.get("brand_title"),
            "priceMinor": int(round(float(amount) * 100)) if amount is not None else None,
            "currency": currency,
            "size": item.get("size_title"),
            "condition": str(status) if status is not None else None,
            "category": "clothes",
            "publishedAt": published_at,
            "fetchedAt": _utc_now_iso(),
            "location": None,
            "views": item.get("view_count"),
            "interested": item.get("favourite_count"),
            "description": item.get("description"),
            "imageUrl": _photo_url(item.get("photo"))
            or next(filter(None, map(_photo_url, item.get("photos") or [])), None),
        }

    def _record_from_product_item(self, item: Dict) -> Optional[Dict]:
        """Current (2026) catalog format: {id, productItem: {...}, catalogTracking}.

        It carries no timestamp, so publishedAt stays None until the item page
        is checked (see run()).
        """
        product = item["productItem"]
        listing_id = item.get("id") or product.get("id")
        url = product.get("url")
        if listing_id is None or not isinstance(url, str):
            return None

        price = product.get("price") or {}
        amount = price.get("amount")
        try:
            price_minor = int(round(float(amount) * 100)) if amount is not None else None
        except (TypeError, ValueError):
            price_minor = None

        item_box = product.get("itemBox") or {}
        label = item_box.get("accessibilityLabel") or ""
        photos = product.get("photos") or []
        image_url = next(filter(None, map(_photo_url, photos)), None) or _photo_url(
            product.get("thumbnailUrl") or product.get("photo")
        )

        return {
            "source": "vinted_uk",
            "listingId": str(listing_id),
            "url": f"https://www.vinted.co.uk{url}" if url.startswith("/") else url,
            "title": product.get("title") or "(untitled)",
            "brand": _label_value(label, "Brand") or item_box.get("firstLine"),
            "priceMinor": price_minor,
            "currency": price.get("currencyCode") or "GBP",
            "size": _label_value(label, "Size"),
            "condition": _label_value(label, "Condition"),
            "category": "clothes",
            "publishedAt": None,
            "fetchedAt": _utc_now_iso(),
            "location": None,
            "views": None,
            "interested": product.get("favouriteCount"),
            "description": None,
            "imageUrl": image_url,
        }

    def run(self, config: ScrapeConfig) -> List[Dict]:
        criteria = define_criteria_query(
            order=[config.order],
            condition=["new with tags", "new without tags", "very good condition", "good condition"],
        )
        listings: List[Dict] = []
        seen = set()
        started = monotonic()
        now_ts = datetime.now(timezone.utc).timestamp()
        max_age_minutes = config.max_item_age_hours * 60

        def passes_cheap_filters(record: Optional[Dict]) -> bool:
            if not record:
                self.stats["listing_errors"] += 1
                return False
            listing_id = record["listingId"]
            if listing_id in seen:
                return False
            seen.add(listing_id)
            if config.strict_target_only and not self._is_target_match(record, config.target_terms, config.query):
                self.stats["listing_skipped_not_target"] += 1
                return False
            # Check price threshold
            price_minor = record.get("priceMinor")
            if price_minor is not None:
                price_gbp = price_minor / 100.0
                if price_gbp > config.max_price_gbp:
                    self.stats["listing_skipped_over_price"] = self.stats.get("listing_skipped_over_price", 0) + 1
                    return False
            return True

        def add_if_fresh(record: Dict) -> None:
            published_at = record.get("publishedAt")
            if not published_at:
                # Unknown age would screen as brand new and trigger an alert
                # for an old listing, so drop it instead.
                self.stats["listing_skipped_missing_published_at"] += 1
                return
            try:
                published_ts = datetime.fromisoformat(published_at).timestamp()
                age_minutes = max(0, int((now_ts - published_ts) / 60))
            except Exception:
                self.stats["listing_skipped_missing_published_at"] += 1
                return
            if age_minutes > max_age_minutes:
                self.stats["listing_skipped_stale_24h"] += 1
                return
            listings.append(record)

        def resolve_ages(records: List[Dict]) -> None:
            """Open item pages (newest id first) to fill in publishedAt.

            Stops at the first item older than the cutoff: ids are assigned in
            creation order, so everything after it in this order is older too.
            """
            global _stale_id_floor, _item_pages_blocked
            cutoff = min(max_age_minutes, AGE_CHECK_CUTOFF_MINUTES)
            consecutive_failures = 0
            for record in sorted(records, key=lambda r: int(r["listingId"]), reverse=True):
                listing_num = int(record["listingId"])
                if listing_num <= _stale_id_floor:
                    self.stats["listing_skipped_stale_24h"] += 1
                    continue
                if _item_pages_blocked:
                    self.stats["listing_skipped_missing_published_at"] += 1
                    continue
                if monotonic() - started > config.max_runtime_seconds:
                    self.stats["stopped_reason"] = "max_runtime_reached"
                    return
                if self.stats["listing_pages_scraped"]:
                    sleep(ITEM_PAGE_DELAY_SECONDS)
                age = self._fetch_upload_age_minutes(record["url"])
                if age is None:
                    self.stats["listing_skipped_missing_published_at"] += 1
                    consecutive_failures += 1
                    if consecutive_failures >= 2:
                        _item_pages_blocked = True
                        self.stats["item_pages_blocked"] = True
                    continue
                consecutive_failures = 0
                if age > cutoff:
                    _stale_id_floor = max(_stale_id_floor, listing_num)
                    self.stats["listing_skipped_stale_24h"] += 1
                    continue
                record["publishedAt"] = datetime.fromtimestamp(
                    now_ts - age * 60, timezone.utc
                ).isoformat()
                if not record.get("imageUrl"):
                    record["imageUrl"] = getattr(self, "_last_page_image", None)
                listings.append(record)

        for page in range(1, config.max_pages + 1):
            if monotonic() - started > config.max_runtime_seconds:
                self.stats["stopped_reason"] = "max_runtime_reached"
                break
            self.stats["pages_scanned"] += 1
            search_url = build_search_url(config.query, criteria, page)
            items = self._extract_embedded_items(search_url)
            if items:
                needs_age: List[Dict] = []
                for raw_item in items:
                    record = self._record_from_embedded_item(raw_item)
                    if not passes_cheap_filters(record):
                        continue
                    if record.get("publishedAt"):
                        add_if_fresh(record)
                    else:
                        needs_age.append(record)
                resolve_ages(needs_age)
                if self.stats.get("stopped_reason"):
                    return listings
                # Newest-first results: once a whole page is older than the
                # window, later pages will be too.
                page_ids = [int(i["id"]) for i in items if str(i.get("id", "")).isdigit()]
                if page_ids and max(page_ids) <= _stale_id_floor:
                    self.stats["stopped_reason"] = "reached_stale_items"
                    break
                continue

            listing_urls = self._extract_listing_urls(search_url)
            for listing_url in listing_urls:
                if monotonic() - started > config.max_runtime_seconds:
                    self.stats["stopped_reason"] = "max_runtime_reached"
                    return listings
                self.stats["listing_pages_scraped"] += 1
                record = self.scrape_listing(listing_url)
                if passes_cheap_filters(record):
                    add_if_fresh(record)

        return listings


def scrape_vinted_listings(query: str, max_pages: int) -> List[Dict]:
    scraper = VintedScraper()
    try:
        config = ScrapeConfig(query=query, max_pages=max_pages)
        return scraper.run(config)
    finally:
        scraper.close()


def scrape_vinted_listings_with_stats(
    query: str,
    max_pages: int,
    page_load_timeout_seconds: int = 20,
    max_runtime_seconds: int = 240,
    max_item_age_hours: int = 24,
    strict_target_only: bool = True,
    target_terms: Optional[List[str]] = None,
    max_price_gbp: float = 20.0,
) -> Dict:
    scraper = VintedScraper(page_load_timeout_seconds=page_load_timeout_seconds)
    try:
        config = ScrapeConfig(
            query=query,
            max_pages=max_pages,
            strict_target_only=strict_target_only,
            target_terms=(target_terms or DEFAULT_TARGET_TERMS.copy()),
            page_load_timeout_seconds=page_load_timeout_seconds,
            max_runtime_seconds=max_runtime_seconds,
            max_item_age_hours=max_item_age_hours,
            max_price_gbp=max_price_gbp,
        )
        listings = scraper.run(config)
        return {"listings": listings, "stats": scraper.stats}
    finally:
        scraper.close()


if __name__ == "__main__":
    query = os.getenv("VINTED_QUERY", "patagonia r1")
    max_pages = int(os.getenv("MAX_PAGES", "3"))
    result = scrape_vinted_listings_with_stats(query=query, max_pages=max_pages)
    print(json.dumps({"count": len(result["listings"]), **result}, indent=2))
