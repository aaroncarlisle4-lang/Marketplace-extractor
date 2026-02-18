import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    strict_r1_only: bool = True
    page_load_timeout_seconds: int = 20
    max_runtime_seconds: int = 240


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
        self.driver = webdriver.Chrome(options=options)
        self.driver.set_page_load_timeout(page_load_timeout_seconds)
        self.driver.set_script_timeout(page_load_timeout_seconds)
        self.wait = WebDriverWait(self.driver, 20)
        self.stats = {
            "pages_scanned": 0,
            "page_link_count": 0,
            "listing_errors": 0,
            "listing_skipped_not_r1": 0,
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
        marker = '\\"items\\":{\\"items\\":['
        start = html.find(marker)
        if start == -1:
            return []
        start += len(marker)
        end = html.find('],\\"pagination\\":', start)
        if end == -1:
            return []
        escaped = "[" + html[start:end] + "]"
        try:
            payload = escaped.encode("utf-8").decode("unicode_escape")
            items = json.loads(payload)
        except Exception:
            return []
        out: List[str] = []
        seen = set()
        for item in items:
            path = item.get("path")
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
        marker = '\\"items\\":{\\"items\\":['
        start = html.find(marker)
        if start == -1:
            return []
        start += len(marker)
        end = html.find('],\\"pagination\\":', start)
        if end == -1:
            return []
        escaped = "[" + html[start:end] + "]"
        try:
            payload = escaped.encode("utf-8").decode("unicode_escape")
            items = json.loads(payload)
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        self.stats["page_link_count"] += len(items)
        return items

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

    def _is_strict_r1(self, item: Dict) -> bool:
        brand = str(item.get("brand") or "").lower()
        title = str(item.get("title") or "").lower()
        description = str(item.get("description") or "").lower()
        text = f"{title} {description}"
        return "patagonia" in brand and bool(re.search(r"\br1\b", text))

    def _record_from_embedded_item(self, item: Dict) -> Optional[Dict]:
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

        return {
            "source": "vinted_uk",
            "listingId": str(listing_id),
            "url": f"https://www.vinted.co.uk{path}" if path.startswith("/") else path,
            "title": item.get("title") or "(untitled)",
            "brand": item.get("brand_title"),
            "priceMinor": int(round(float(amount) * 100)) if amount is not None else None,
            "currency": currency,
            "size": item.get("size_title"),
            "condition": item.get("status"),
            "category": "clothes",
            "publishedAt": published_at,
            "fetchedAt": _utc_now_iso(),
            "location": None,
            "views": item.get("view_count"),
            "interested": item.get("favourite_count"),
            "description": item.get("description"),
            "imageUrl": (item.get("photo") or {}).get("url"),
        }

    def run(self, config: ScrapeConfig) -> List[Dict]:
        criteria = define_criteria_query(
            order=[config.order],
            catalog=[config.catalog],
            condition=["new with tags", "new without tags", "very good condition", "good condition"],
        )
        listings: List[Dict] = []
        seen = set()
        started = monotonic()

        for page in range(1, config.max_pages + 1):
            if monotonic() - started > config.max_runtime_seconds:
                self.stats["stopped_reason"] = "max_runtime_reached"
                break
            self.stats["pages_scanned"] += 1
            search_url = build_search_url(config.query, criteria, page)
            items = self._extract_embedded_items(search_url)
            for raw_item in items:
                if monotonic() - started > config.max_runtime_seconds:
                    self.stats["stopped_reason"] = "max_runtime_reached"
                    return listings
                record = self._record_from_embedded_item(raw_item)
                if not record:
                    self.stats["listing_errors"] += 1
                    continue
                listing_id = record["listingId"]
                if listing_id in seen:
                    continue
                seen.add(listing_id)
                if config.strict_r1_only and not self._is_strict_r1(record):
                    self.stats["listing_skipped_not_r1"] += 1
                    continue
                listings.append(record)

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
) -> Dict:
    scraper = VintedScraper(page_load_timeout_seconds=page_load_timeout_seconds)
    try:
        config = ScrapeConfig(
            query=query,
            max_pages=max_pages,
            page_load_timeout_seconds=page_load_timeout_seconds,
            max_runtime_seconds=max_runtime_seconds,
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
