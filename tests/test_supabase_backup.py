import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKERS_DIR = ROOT / "workers"
if str(WORKERS_DIR) not in sys.path:
    sys.path.insert(0, str(WORKERS_DIR))

import run_cycle
import supabase_backup


def iso_minutes_ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def vinted_listing(listing_id="1", minutes_old=3, **overrides):
    listing = {
        "source": "vinted_uk",
        "listingId": listing_id,
        "url": f"https://www.vinted.co.uk/items/{listing_id}",
        "title": "Patagonia Synchilla Snap-T Fleece",
        "brand": "Patagonia",
        "priceMinor": 3000,
        "currency": "GBP",
        "size": "M",
        "condition": "Very good",
        "category": "Men / Jumpers",
        "publishedAt": iso_minutes_ago(minutes_old),
        "fetchedAt": iso_minutes_ago(0),
    }
    listing.update(overrides)
    return listing


def screen(raw):
    return supabase_backup.screen_listing(supabase_backup.normalize_listing(raw))


class TestScreening:
    def test_fresh_target_listing_is_eligible(self):
        m = screen(vinted_listing())
        assert m["isMatch"] and m["eligibleForNotify"]
        assert m["freshnessBucket"] == "HOT"
        # 60 (HOT) + min(30, (5000-3000)//100) + 10 (condition)
        assert m["score"] == 90

    def test_rejections_match_convex_reason_codes(self):
        m = screen(vinted_listing(title="Nike hoodie", brand="Nike", priceMinor=9000, condition="Satisfactory"))
        assert not m["isMatch"]
        assert set(m["reasons"]) >= {
            "brand_not_target",
            "price_or_currency_not_allowed",
            "condition_not_allowed",
        }

    def test_older_than_hour_is_match_but_not_eligible(self):
        m = screen(vinted_listing(minutes_old=90))
        assert m["isMatch"] and not m["eligibleForNotify"]
        assert m["freshnessBucket"] == "RECENT"

    def test_facebook_needs_five_minute_window(self):
        base = {
            "source": "facebook_marketplace_uk",
            "listingId": "fb1",
            "url": "https://facebook.com/marketplace/item/fb1",
            "title": "Chesterfield captains chair",
            "fetchedAt": iso_minutes_ago(0),
        }
        assert screen({**base, "publishedAt": iso_minutes_ago(2)})["eligibleForNotify"]
        late = screen({**base, "publishedAt": iso_minutes_ago(30)})
        assert late["isMatch"] and not late["eligibleForNotify"]

    def test_normalize_rejects_incomplete_and_future(self):
        assert supabase_backup.normalize_listing({"source": "vinted_uk", "listingId": "x"}) is None
        future = supabase_backup.normalize_listing(vinted_listing(publishedAt=iso_minutes_ago(-60)))
        assert future["publishedAtMs"] is None


class FakeSupabase:
    """Minimal PostgREST stand-in covering the calls supabase_backup makes."""

    def __init__(self):
        self.tables = {"listings": {}, "notifications": {}, "runs": {}}

    def handle(self, method, url, params=None, data=None, headers=None, **_):
        table = url.split("/rest/v1/")[1].split("?")[0]
        body = json.loads(data) if data else None
        prefer = (headers or {}).get("Prefer", "")
        if method == "POST" and table == "runs":
            self.tables["runs"][1] = body
            return FakeResponse(201, [{"id": 1}])
        if method == "POST":
            key_fields = ("source", "listing_id") if table == "listings" else ("idempotency_key",)
            inserted = []
            for row in body:
                key = tuple(row[k] for k in key_fields)
                if key in self.tables[table] and "ignore-duplicates" in prefer:
                    continue
                self.tables[table][key] = dict(row)
                inserted.append(row)
            return FakeResponse(201, inserted if "representation" in prefer else None)
        if method == "PATCH" and table == "runs":
            self.tables["runs"][1].update(body)
            return FakeResponse(204, None)
        rows = [r for r in self.tables[table].values() if self._matches(r, params)]
        if method == "GET":
            return FakeResponse(200, rows)
        if method == "PATCH":
            for r in rows:
                r.update(body)
            return FakeResponse(200, rows if "representation" in prefer else None)
        raise AssertionError(method)

    @staticmethod
    def _matches(row, params):
        for col, cond in (params or {}).items():
            if col == "select":
                continue
            assert cond.startswith("eq."), cond
            if str(row.get(col)) != cond[3:]:
                return False
        return True


class FakeResponse:
    def __init__(self, status, payload=None, content=b""):
        self.status_code = status
        self.ok = status < 400
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise supabase_backup.requests.HTTPError(str(self.status_code))


JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


class FakeTelegram:
    def __init__(self):
        self.calls = []  # (method, payload)
        self.fail_methods = set()
        self.images = {"https://img.example/1.jpg": JPEG}
        self.pages = {}

    def post(self, url, data=None, json=None, files=None, **_):
        method = url.rsplit("/", 1)[1]
        if method in self.fail_methods:
            return FakeResponse(400, {"ok": False})
        payload = dict(json or data or {})
        if files:
            payload["uploaded"] = files["photo"][1]
        self.calls.append((method, payload))
        return FakeResponse(200, {"ok": True})

    def get(self, url, **_):
        if url in self.images:
            return FakeResponse(200, content=self.images[url])
        if url in self.pages:
            resp = FakeResponse(200)
            resp.text = self.pages[url]
            return resp
        return FakeResponse(404)


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    fake = FakeSupabase()
    telegram = FakeTelegram()
    monkeypatch.setattr(supabase_backup.requests, "request", fake.handle)
    monkeypatch.setattr(supabase_backup.requests, "post", telegram.post)
    monkeypatch.setattr(supabase_backup.requests, "get", telegram.get)
    return fake, telegram


def run(listings, disable_notify=False):
    return supabase_backup.run_supabase("vinted", listings, disable_notify)


class TestNotify:
    def test_error_without_config(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        result = run([vinted_listing()])
        assert result["enabled"] is False and result["error"]

    def test_sends_photo_alert_and_records_it(self, backend):
        fake, telegram = backend
        listings = [
            vinted_listing("1", imageUrl="https://img.example/1.jpg"),
            vinted_listing("2", minutes_old=120),
        ]
        result = run(listings)
        assert result["notify"]["sent"] == 1 and result["notify"]["without_image"] == 0
        [(method, payload)] = telegram.calls
        assert method == "sendPhoto" and payload["uploaded"] == JPEG
        assert "[HOT] Vinted UK" in payload["caption"]
        assert "https://www.vinted.co.uk/items/1" in payload["caption"]
        row = fake.tables["notifications"][("vinted_uk:1",)]
        assert row["status"] == "sent" and row["sent_at"]
        assert ("vinted_uk", "2") in fake.tables["listings"]

    def test_never_alerts_same_listing_twice(self, backend):
        _, telegram = backend
        listing = vinted_listing("1", imageUrl="https://img.example/1.jpg")
        run([listing])
        result = run([listing])
        assert result["notify"]["skipped"] == 1
        assert len(telegram.calls) == 1

    def test_claimed_but_unconfirmed_alert_is_not_resent(self, backend):
        # A run killed after claiming leaves status 'sent' with no sent_at.
        fake, telegram = backend
        fake.tables["notifications"][("vinted_uk:1",)] = {
            "idempotency_key": "vinted_uk:1", "status": "sent", "attempts": 1, "sent_at": None,
        }
        result = run([vinted_listing("1")])
        assert result["notify"]["skipped"] == 1 and telegram.calls == []

    def test_failed_alert_is_retried(self, backend):
        fake, telegram = backend
        fake.tables["notifications"][("vinted_uk:1",)] = {
            "idempotency_key": "vinted_uk:1", "status": "failed", "attempts": 2,
        }
        result = run([vinted_listing("1", imageUrl="https://img.example/1.jpg")])
        assert result["notify"]["sent"] == 1 and len(telegram.calls) == 1
        assert fake.tables["notifications"][("vinted_uk:1",)]["attempts"] == 3

    def test_send_failure_is_recorded_for_retry(self, backend):
        fake, telegram = backend
        telegram.fail_methods = {"sendPhoto", "sendMessage"}
        result = run([vinted_listing("1", imageUrl="https://img.example/1.jpg")])
        assert result["notify"]["failed"] == 1
        assert fake.tables["notifications"][("vinted_uk:1",)]["status"] == "failed"

    def test_missing_image_is_looked_up_on_listing_page(self, backend):
        _, telegram = backend
        telegram.pages["https://www.vinted.co.uk/items/1"] = (
            '<meta property="og:image" content="https://img.example/1.jpg">'
        )
        run([vinted_listing("1")])
        [(method, payload)] = telegram.calls
        assert method == "sendPhoto" and payload["uploaded"] == JPEG

    def test_webp_is_converted_to_jpeg(self, backend):
        from io import BytesIO

        from PIL import Image

        _, telegram = backend
        buf = BytesIO()
        Image.new("RGB", (4, 4), "red").save(buf, format="WEBP")
        telegram.images["https://img.example/1.webp"] = buf.getvalue()
        run([vinted_listing("1", imageUrl="https://img.example/1.webp")])
        [(method, payload)] = telegram.calls
        assert method == "sendPhoto" and payload["uploaded"][:3] == b"\xff\xd8\xff"

    def test_photo_by_url_when_download_fails(self, backend):
        _, telegram = backend
        run([vinted_listing("1", imageUrl="https://img.example/blocked.jpg")])
        [(method, payload)] = telegram.calls
        assert method == "sendPhoto" and payload["photo"] == "https://img.example/blocked.jpg"

    def test_text_alert_when_no_image_exists(self, backend):
        _, telegram = backend
        result = run([vinted_listing("1")])
        assert result["notify"]["without_image"] == 1
        [(method, payload)] = telegram.calls
        assert method == "sendMessage" and "no image available" in payload["text"]

    def test_disable_notify_respected(self, backend):
        _, telegram = backend
        result = run([vinted_listing()], disable_notify=True)
        assert result["notify"] == {"disabled": True} and telegram.calls == []


class TestFinish:
    def test_ok_without_error(self):
        assert run_cycle.finish({"enabled": True}) == 0

    def test_raises_on_supabase_error(self):
        with pytest.raises(RuntimeError):
            run_cycle.finish({"enabled": True, "error": "boom"})
