import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import monotonic, sleep
from typing import Dict, List, Optional

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

DEFAULT_TARGET_TERMS = [
    "captain's chair",
    "captains chair",
    "captain chair",
    "chesterfield captain chair",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_minor_gbp(price_text: str) -> Optional[int]:
    if not price_text:
        return None
    cleaned = re.sub(r"[^0-9.,]", "", price_text).replace(",", "")
    if not cleaned:
        return None
    try:
        return int(round(float(cleaned) * 100))
    except ValueError:
        return None


def miles_to_radius_km(miles: int) -> int:
    return max(1, int(round(miles * 1.60934)))


def build_search_url(query: str, location_slug: str, radius_miles: int) -> str:
    encoded_q = urllib.parse.quote(query)
    radius_km = miles_to_radius_km(radius_miles)
    return (
        f"https://www.facebook.com/marketplace/{location_slug}/search/"
        f"?query={encoded_q}&sortBy=creation_time_descend&exact=false&radiusKM={radius_km}"
    )


def parse_listing_age_minutes(text: str) -> Optional[int]:
    if not text:
        return None
    hay = text.lower()
    if "just now" in hay:
        return 0
    if "today" in hay:
        return 0
    if "yesterday" in hay:
        return 24 * 60

    checks = [
        (r"(\d+)\s*minute", 1),
        (r"(\d+)\s*min\b", 1),
        (r"(\d+)\s*m\b", 1),
        (r"(\d+)\s*hour", 60),
        (r"(\d+)\s*hr\b", 60),
        (r"(\d+)\s*h\b", 60),
        (r"(\d+)\s*day", 24 * 60),
        (r"(\d+)\s*d\b", 24 * 60),
    ]
    for pattern, factor in checks:
        match = re.search(pattern, hay)
        if match:
            return int(match.group(1)) * factor

    if "an hour" in hay or "about an hour" in hay:
        return 60
    if "a day" in hay:
        return 24 * 60
    return None


def _published_at_from_page_text(page_text: str) -> Optional[str]:
    minute_age = parse_listing_age_minutes(page_text)
    if minute_age is None:
        return None
    published_at = datetime.now(timezone.utc) - timedelta(minutes=minute_age)
    return published_at.isoformat()


@dataclass
class FacebookScrapeConfig:
    query: str = "captain's chair"
    location_slug: str = "belfast"
    max_pages: int = 2
    radius_miles: int = 40
    strict_target_only: bool = True
    target_terms: List[str] = field(default_factory=lambda: DEFAULT_TARGET_TERMS.copy())
    page_load_timeout_seconds: int = 20
    max_runtime_seconds: int = 180
    max_item_age_hours: int = 1


class FacebookMarketplaceScraper:
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
        self.driver = webdriver.Chrome(options=options)
        self.driver.set_page_load_timeout(page_load_timeout_seconds)
        self.driver.set_script_timeout(page_load_timeout_seconds)
        self.wait = WebDriverWait(self.driver, 20)
        self.stats = {
            "pages_scanned": 0,
            "page_link_count": 0,
            "listing_errors": 0,
            "listing_skipped_not_target": 0,
            "listing_skipped_missing_published_at": 0,
            "listing_skipped_stale_24h": 0,
        }
        self.debug_dir = os.getenv("FB_DEBUG_DIR", "").strip()
        if self.debug_dir:
            os.makedirs(self.debug_dir, exist_ok=True)

    def close(self) -> None:
        self.driver.quit()

    def _extract_listing_id(self, url: str) -> str:
        match = re.search(r"/marketplace/item/(\d+)", url)
        if match:
            return match.group(1)
        return url.rstrip("/").split("/")[-1]

    def _is_target_match(self, item: Dict, target_terms: List[str]) -> bool:
        title = str(item.get("title") or "").lower()
        description = str(item.get("description") or "").lower()
        text = f"{title} {description}"
        normalized_text = re.sub(r"[^a-z0-9]+", " ", text).strip()
        for term in target_terms:
            normalized_term = re.sub(r"[^a-z0-9]+", " ", term.lower()).strip()
            if normalized_term and normalized_term in normalized_text:
                return True
        return False

    def _debug_write(self, name: str, content: str) -> None:
        if not self.debug_dir:
            return
        try:
            with open(os.path.join(self.debug_dir, name), "w", encoding="utf-8") as fp:
                fp.write(content)
        except Exception:
            pass

    @retry(wait=wait_exponential(multiplier=1, min=1, max=8), stop=stop_after_attempt(3))
    def _collect_listing_urls(self, search_url: str, max_pages: int) -> List[str]:
        try:
            self.driver.get(search_url)
        except TimeoutException:
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                pass

        try:
            self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="/marketplace/item/"]')))
        except TimeoutException:
            pass
        self.stats["search_page_title"] = self.driver.title
        self._debug_write("search_page_title.txt", self.driver.title or "")
        self._debug_write("search_page_source.html", self.driver.page_source)

        seen = set()
        out: List[str] = []
        for _ in range(max_pages):
            links = self.driver.find_elements(By.CSS_SELECTOR, 'a[href*="/marketplace/item/"]')
            for link in links:
                href = link.get_attribute("href")
                if not href:
                    continue
                href = href.split("?")[0]
                if href in seen:
                    continue
                seen.add(href)
                out.append(href)
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            sleep(1.2)

        self.stats["page_link_count"] += len(out)
        self.stats["search_url"] = search_url
        self.stats["discovered_links"] = len(out)
        if self.debug_dir:
            self._debug_write("discovered_links.json", json.dumps(out[:200], indent=2))
            try:
                self.driver.save_screenshot(os.path.join(self.debug_dir, "search_screenshot.png"))
            except Exception:
                pass
        return out

    @retry(wait=wait_exponential(multiplier=1, min=1, max=8), stop=stop_after_attempt(3))
    def scrape_listing(self, url: str) -> Optional[Dict]:
        try:
            self.driver.get(url)
        except TimeoutException:
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                return None

        page_text = self.driver.page_source

        title = ""
        title_el = self.driver.find_elements(By.CSS_SELECTOR, "h1")
        if title_el:
            title = title_el[0].text.strip()

        description = None
        desc_el = self.driver.find_elements(By.CSS_SELECTOR, 'meta[property="og:description"]')
        if desc_el:
            description = desc_el[0].get_attribute("content")

        image_url = None
        image_el = self.driver.find_elements(By.CSS_SELECTOR, 'meta[property="og:image"]')
        if image_el:
            image_url = image_el[0].get_attribute("content")

        price_match = re.search(r"(£\s?\d[\d,]*(?:\.\d{1,2})?)", page_text)
        price_text = price_match.group(1) if price_match else ""
        published_at = _published_at_from_page_text(page_text)

        record = {
            "source": "facebook_marketplace_uk",
            "listingId": self._extract_listing_id(url),
            "url": url,
            "title": title or "(untitled)",
            "brand": None,
            "priceMinor": _to_minor_gbp(price_text),
            "currency": "GBP",
            "size": None,
            "condition": None,
            "category": "furniture",
            "publishedAt": published_at,
            "fetchedAt": _utc_now_iso(),
            "location": "Belfast",
            "views": None,
            "interested": None,
            "description": description,
            "imageUrl": image_url,
        }

        if not record["listingId"] or not record["url"]:
            return None
        if self.debug_dir:
            safe_id = record["listingId"].replace("/", "_")
            self._debug_write(f"listing_{safe_id}.json", json.dumps(record, indent=2))
        return record

    def run(self, config: FacebookScrapeConfig) -> List[Dict]:
        started = monotonic()
        now_ts = datetime.now(timezone.utc).timestamp()
        max_age_minutes = config.max_item_age_hours * 60
        search_url = build_search_url(
            query=config.query,
            location_slug=config.location_slug,
            radius_miles=config.radius_miles,
        )

        self.stats["pages_scanned"] += max(1, config.max_pages)
        listing_urls = self._collect_listing_urls(search_url, max_pages=config.max_pages)

        listings: List[Dict] = []
        seen = set()
        for url in listing_urls:
            if monotonic() - started > config.max_runtime_seconds:
                self.stats["stopped_reason"] = "max_runtime_reached"
                break
            listing_id = self._extract_listing_id(url)
            if listing_id in seen:
                continue
            seen.add(listing_id)

            record = self.scrape_listing(url)
            if not record:
                self.stats["listing_errors"] += 1
                continue
            if config.strict_target_only and not self._is_target_match(record, config.target_terms):
                self.stats["listing_skipped_not_target"] += 1
                continue

            published_at = record.get("publishedAt")
            if not published_at:
                self.stats["listing_skipped_missing_published_at"] += 1
                continue
            try:
                published_ts = datetime.fromisoformat(published_at).timestamp()
            except Exception:
                self.stats["listing_skipped_missing_published_at"] += 1
                continue
            age_minutes = max(0, int((now_ts - published_ts) / 60))
            if age_minutes > max_age_minutes:
                self.stats["listing_skipped_stale_24h"] += 1
                continue
            listings.append(record)

        return listings


def scrape_facebook_listings_with_stats(
    query: str,
    location_slug: str,
    radius_miles: int,
    max_pages: int,
    page_load_timeout_seconds: int = 20,
    max_runtime_seconds: int = 180,
    max_item_age_hours: int = 24,
    strict_target_only: bool = True,
    target_terms: Optional[List[str]] = None,
) -> Dict:
    scraper = FacebookMarketplaceScraper(page_load_timeout_seconds=page_load_timeout_seconds)
    try:
        config = FacebookScrapeConfig(
            query=query,
            location_slug=location_slug,
            max_pages=max_pages,
            radius_miles=radius_miles,
            strict_target_only=strict_target_only,
            target_terms=(target_terms or DEFAULT_TARGET_TERMS.copy()),
            page_load_timeout_seconds=page_load_timeout_seconds,
            max_runtime_seconds=max_runtime_seconds,
            max_item_age_hours=max_item_age_hours,
        )
        listings = scraper.run(config)
        return {"listings": listings, "stats": scraper.stats}
    finally:
        scraper.close()


def scrape_facebook_listing_urls_with_stats(
    urls: List[str],
    page_load_timeout_seconds: int = 20,
    max_runtime_seconds: int = 180,
    max_item_age_hours: int = 24,
    strict_target_only: bool = True,
    target_terms: Optional[List[str]] = None,
) -> Dict:
    scraper = FacebookMarketplaceScraper(page_load_timeout_seconds=page_load_timeout_seconds)
    try:
        started = monotonic()
        now_ts = datetime.now(timezone.utc).timestamp()
        max_age_minutes = max_item_age_hours * 60
        listings: List[Dict] = []
        seen = set()

        terms = target_terms or DEFAULT_TARGET_TERMS.copy()
        for url in urls:
            if monotonic() - started > max_runtime_seconds:
                scraper.stats["stopped_reason"] = "max_runtime_reached"
                break
            listing_id = scraper._extract_listing_id(url)
            if listing_id in seen:
                continue
            seen.add(listing_id)
            record = scraper.scrape_listing(url)
            if not record:
                scraper.stats["listing_errors"] += 1
                continue
            if strict_target_only and not scraper._is_target_match(record, terms):
                scraper.stats["listing_skipped_not_target"] += 1
                continue
            published_at = record.get("publishedAt")
            if not published_at:
                scraper.stats["listing_skipped_missing_published_at"] += 1
                continue
            try:
                published_ts = datetime.fromisoformat(published_at).timestamp()
            except Exception:
                scraper.stats["listing_skipped_missing_published_at"] += 1
                continue
            age_minutes = max(0, int((now_ts - published_ts) / 60))
            if age_minutes > max_age_minutes:
                scraper.stats["listing_skipped_stale_24h"] += 1
                continue
            listings.append(record)

        scraper.stats["provided_urls"] = len(urls)
        scraper.stats["direct_url_mode"] = True
        return {"listings": listings, "stats": scraper.stats}
    finally:
        scraper.close()


if __name__ == "__main__":
    result = scrape_facebook_listings_with_stats(
        query="captain's chair",
        location_slug="belfast",
        radius_miles=40,
        max_pages=2,
    )
    print(json.dumps({"count": len(result["listings"]), **result}, indent=2))
