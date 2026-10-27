from workers.vinted_scraper import _to_minor_gbp, build_search_url, define_criteria_query


def test_define_criteria_query():
    query = define_criteria_query(
        order=["most recent first"],
        catalog=["men"],
        condition=["very good condition", "good condition"],
    )
    assert "order=newest_first" in query
    assert "catalog[]=5" in query
    assert "status[]=2" in query
    assert "status[]=3" in query


def test_build_search_url():
    assert (
        build_search_url("patagonia r1", "order=newest_first", page=1)
        == "https://www.vinted.co.uk/catalog?search_text=patagonia%20r1&page=1&order=newest_first"
    )


def test_to_minor_gbp():
    assert _to_minor_gbp("£80") == 8000
    assert _to_minor_gbp("£79.99") == 7999
    assert _to_minor_gbp("") is None


# --- 2026 catalog format: {id, productItem}, no timestamps -------------------

import json as _json

from workers import vinted_scraper as _vs


def _escaped_catalog_html(items):
    # Vinted embeds the array inside a JS string literal in the Next.js payload.
    inner = _json.dumps(_json.dumps({"items": {"items": items, "pagination": {}}}, separators=(",", ":")))[1:-1]
    return f'<script>self.__next_f.push([1,"{inner}"])</script>'


PRODUCT_ITEM = {
    "id": 10550474954,
    "productItem": {
        "id": 10550474954,
        "title": "Patagonia R1 Air Crew Fleece",
        "url": "/items/10550474954-patagonia-r1-air-crew-fleece",
        "favouriteCount": 1,
        "price": {"amount": "38.00", "currencyCode": "GBP"},
        "photos": [{"url": "https://images1.vinted.net/x.webp"}],
        "itemBox": {
            "firstLine": "Patagonia",
            "secondLine": "XXL · Very good",
            "accessibilityLabel": "Patagonia R1 Air Crew Fleece, Brand: Patagonia, "
            "Condition: Very good, Size: XXL, 38.00 £, 41.60 £",
        },
    },
    "catalogTracking": {"contentSource": "search"},
}


def test_decode_embedded_items_keeps_unicode():
    items = _vs.decode_embedded_items(_escaped_catalog_html([PRODUCT_ITEM]))
    assert items[0]["productItem"]["itemBox"]["secondLine"] == "XXL · Very good"


def test_record_from_product_item():
    record = _vs.VintedScraper.__new__(_vs.VintedScraper)._record_from_embedded_item(PRODUCT_ITEM)
    assert record["listingId"] == "10550474954"
    assert record["url"] == "https://www.vinted.co.uk/items/10550474954-patagonia-r1-air-crew-fleece"
    assert record["priceMinor"] == 3800
    assert (record["brand"], record["condition"], record["size"]) == ("Patagonia", "Very good", "XXL")
    assert record["publishedAt"] is None


def test_upload_age_from_item_page():
    html = (
        '\\"code\\":\\"upload_date\\",\\"data\\":{\\"title\\":\\"Uploaded\\",'
        '\\"value\\":\\"4 min ago\\"},\\"type\\":\\"text\\"'
    )
    assert _vs.extract_upload_age_minutes(html) == 4
    assert _vs.extract_upload_age_minutes('<div itemProp="upload_date"><span class="x">2 hours ago</span>') == 120
    assert _vs.extract_upload_age_minutes("<html>rate limited</html>") is None


def test_relative_age_parsing():
    cases = {"just now": 0, "an hour ago": 60, "3 days ago": 4320, "yesterday": 1440, "soon": None}
    for text, expected in cases.items():
        assert _vs.parse_relative_age_minutes(text) == expected


class _FakeDriver:
    def __init__(self, pages):
        self.pages = pages
        self.page_source = ""
        self.visited = []

    def get(self, url):
        self.visited.append(url)
        self.page_source = next(html for key, html in self.pages.items() if key in url)

    def execute_script(self, *_):
        pass


def _age_page(text):
    return f'<div itemProp="upload_date"><span>{text}</span></div>'


def test_run_resolves_ages_and_stops_at_stale(monkeypatch):
    monkeypatch.setattr(_vs, "_stale_id_floor", 0)
    monkeypatch.setattr(_vs, "_item_pages_blocked", False)
    monkeypatch.setattr(_vs, "ITEM_PAGE_DELAY_SECONDS", 0)

    def item(item_id, title):
        product = dict(PRODUCT_ITEM["productItem"], id=item_id, title=title, url=f"/items/{item_id}-x")
        return {"id": item_id, "productItem": product}

    catalog = _escaped_catalog_html([item(300, "Patagonia fleece new"), item(200, "Patagonia fleece old"), item(100, "Patagonia fleece older")])
    older_page = _escaped_catalog_html([item(150, "Patagonia fleece"), item(90, "Patagonia fleece")])
    driver = _FakeDriver({
        "/items/300": _age_page("4 min ago"),
        "/items/200": _age_page("3 hours ago"),
        "/items/100": _age_page("2 days ago"),
        "page=1": catalog,
        "page=2": older_page,
    })
    scraper = _vs.VintedScraper.__new__(_vs.VintedScraper)
    scraper.driver = driver
    scraper.stats = {k: 0 for k in ["pages_scanned", "page_link_count", "listing_errors", "listing_pages_scraped", "listing_skipped_not_target", "listing_skipped_missing_published_at", "listing_skipped_stale_24h", "listing_skipped_over_price"]}

    listings = scraper.run(_vs.ScrapeConfig(query="patagonia fleece", max_pages=3, max_item_age_hours=24, max_price_gbp=50))

    assert [l["listingId"] for l in listings] == ["300"]
    assert listings[0]["publishedAt"] is not None
    # ids below the stale floor set by id 200 are never opened, and page 2
    # (all older) ends the scan without a page 3.
    item_pages = [url for url in driver.visited if "/items/" in url]
    assert item_pages == ["https://www.vinted.co.uk/items/300-x", "https://www.vinted.co.uk/items/200-x"]
    assert scraper.stats["stopped_reason"] == "reached_stale_items"
    assert not any("page=3" in url for url in driver.visited)


def test_photo_url_handles_photo_shapes():
    from vinted_scraper import _photo_url

    assert _photo_url({"url": "https://a/1.jpg"}) == "https://a/1.jpg"
    assert _photo_url({"fullSizeUrl": "https://a/2.jpg"}) == "https://a/2.jpg"
    assert _photo_url({"thumbnails": [{"url": "https://a/s.jpg"}, {"url": "https://a/l.jpg"}]}) == "https://a/l.jpg"
    assert _photo_url("https://a/3.jpg") == "https://a/3.jpg"
    assert _photo_url({}) is None and _photo_url(None) is None
