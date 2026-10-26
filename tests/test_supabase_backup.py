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
    def __init__(self, existing=None):
        self.tables = {"listings": {}, "notifications": dict(existing or {}), "runs": {}}

    def handle(self, method, url, params=None, data=None, **_):
        table = url.split("/rest/v1/")[1].split("?")[0]
        body = json.loads(data) if data else None
        if method == "POST" and table == "runs":
            self.tables["runs"][1] = body
            return FakeResponse(201, [{"id": 1}])
        if method == "POST":
            key_fields = ("source", "listing_id") if table == "listings" else ("idempotency_key",)
            for row in body:
                self.tables[table][tuple(row[k] for k in key_fields)] = row
            return FakeResponse(201, None)
        if method == "GET":
            return FakeResponse(200, [
                {"idempotency_key": k[0], **v} for k, v in self.tables["notifications"].items()
            ])
        if method == "PATCH":
            self.tables["runs"][1].update(body)
            return FakeResponse(204, None)
        raise AssertionError(method)


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self.ok = status < 400
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.delenv("SUPABASE_NOTIFY_MODE", raising=False)
    fake = FakeSupabase()
    sent = []
    monkeypatch.setattr(supabase_backup.requests, "request", fake.handle)
    monkeypatch.setattr(
        supabase_backup.requests,
        "post",
        lambda url, data=None, **_: sent.append(json.loads(data)["text"]) or FakeResponse(200, {"ok": True}),
    )
    return fake, sent


class TestBackup:
    def test_skipped_without_config(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        result = supabase_backup.run_supabase_backup("vinted", [vinted_listing()], True, False)
        assert result["enabled"] is False

    def test_mirrors_but_does_not_notify_when_convex_ok(self, backend):
        fake, sent = backend
        result = supabase_backup.run_supabase_backup("vinted", [vinted_listing()], True, False)
        assert result["upserted"] == 1 and result["eligible"] == 1
        assert sent == []
        assert ("vinted_uk", "1") in fake.tables["listings"]

    def test_falls_back_to_telegram_when_convex_fails(self, backend):
        fake, sent = backend
        listings = [vinted_listing("1"), vinted_listing("2", minutes_old=120)]
        result = supabase_backup.run_supabase_backup("vinted", listings, False, False)
        assert result["notify"]["sent"] == 1
        assert len(sent) == 1 and "[HOT] Vinted UK" in sent[0]
        assert fake.tables["notifications"][("vinted_uk:1",)]["status"] == "sent"
        assert fake.tables["runs"][1]["fallback_notify"] is True

    def test_fallback_does_not_resend(self, backend):
        fake, sent = backend
        fake.tables["notifications"][("vinted_uk:1",)] = {"status": "sent", "attempts": 1}
        result = supabase_backup.run_supabase_backup("vinted", [vinted_listing("1")], False, False)
        assert result["notify"]["skipped"] == 1 and sent == []

    def test_disable_notify_respected(self, backend):
        _, sent = backend
        result = supabase_backup.run_supabase_backup("vinted", [vinted_listing()], False, True)
        assert result["notify"] == {"disabled": True} and sent == []


class TestFinish:
    def test_ok_when_convex_ok(self):
        assert run_cycle.finish(None, {"enabled": False}) == 0

    def test_ok_when_supabase_covers_convex_failure(self):
        assert run_cycle.finish("boom", {"enabled": True}) == 0

    def test_fails_when_both_fail(self):
        with pytest.raises(RuntimeError):
            run_cycle.finish("boom", {"enabled": True, "error": "also boom"})
        with pytest.raises(RuntimeError):
            run_cycle.finish("boom", {"enabled": False, "reason": "not set"})


class TestPrimaryMode:
    def test_always_mode_sends_even_when_convex_ok(self, backend, monkeypatch):
        monkeypatch.setenv("SUPABASE_NOTIFY_MODE", "always")
        _, sent = backend
        result = supabase_backup.run_supabase_backup("vinted", [vinted_listing()], True, False)
        assert result["notify"]["sent"] == 1 and len(sent) == 1

    def test_convex_notify_skipped_when_supabase_primary(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
        monkeypatch.setenv("SUPABASE_NOTIFY_MODE", "always")
        calls = []
        monkeypatch.setattr(run_cycle, "post_json", lambda url, *_: calls.append(url) or {"inserted": 1})
        _, notify = run_cycle.run_convex("https://x.convex.site", "s", [vinted_listing()], False)
        assert notify["disabled"] is True
        assert calls == ["https://x.convex.site/ingest/listings"]

    def test_finish_in_always_mode(self):
        assert run_cycle.finish("convex down", {"enabled": True, "notify_mode": "always"}) == 0
        with pytest.raises(RuntimeError):
            run_cycle.finish(None, {"enabled": True, "notify_mode": "always", "error": "boom"})
