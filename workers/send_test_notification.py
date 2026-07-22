"""Send a sample listing alert straight to Telegram.

This exercises the exact message format produced by convex/notifyTelegram.ts so you
can confirm end-to-end that notifications reach your Telegram chat.

Usage:
    TELEGRAM_BOT_TOKEN=xxxxx TELEGRAM_CHAT_ID=123456 python workers/send_test_notification.py

The two env vars are the same secrets your Convex deployment uses.
"""

import json
import os
import sys

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def source_label(source: str) -> str:
    if source == "facebook_marketplace_uk":
        return "Facebook Marketplace"
    if source == "vinted_uk":
        return "Vinted UK"
    return source


def build_message() -> str:
    # A realistic Vinted listing that would now pass screening after the fix.
    listing = {
        "source": "vinted_uk",
        "title": "Patagonia Synchilla Snap-T Fleece (TEST)",
        "priceMinor": 3000,
        "currency": "GBP",
        "condition": "Very good",
        "size": "M",
        "url": "https://www.vinted.co.uk/",
    }
    match = {"freshnessBucket": "NEW", "ageMinutes": 12, "score": 70}

    price = (
        f"{listing['priceMinor'] / 100:.2f} {listing['currency']}"
        if isinstance(listing.get("priceMinor"), int)
        else "unknown"
    )

    return "\n".join(
        [
            f"[{match['freshnessBucket']}] {source_label(listing['source'])}",
            listing["title"],
            f"Price: {price}",
            f"Condition: {listing['condition']}",
            f"Size: {listing['size']}",
            f"Age: {match['ageMinutes']}m",
            f"Score: {match['score']}",
            listing["url"],
        ]
    )


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(
            "Missing TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID. "
            "Set them (they are the same secrets Convex uses) and re-run.",
            file=sys.stderr,
        )
        return 1

    message = build_message()
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        headers={"Content-Type": "application/json"},
        data=json.dumps(
            {
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": False,
            }
        ),
        timeout=30,
    )

    if not response.ok:
        print(f"Telegram send failed: {response.status_code} {response.text}", file=sys.stderr)
        return 1

    print("Sent test notification to Telegram. Check your chat.")
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
