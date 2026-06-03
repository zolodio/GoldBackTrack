#!/usr/bin/env python3
"""
Goldback Exchange Rate Scraper
Fetches the daily Goldback rate from the IMS XML feed used by the official
Goldback WordPress plugin, and the gold spot price from apmex.com,
then appends both to data/rates.json.
"""

import json
import re  # still used by APMEX DOM fallback
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

DATA_FILE    = Path(__file__).parent / "data" / "rates.json"
GOLDBACK_XML_URL = "https://services.idealmsp.com/IMSPlugins/goldback-exchange/goldbackrate.xml"
APMEX_URL    = "https://www.apmex.com/gold-price"
MST          = ZoneInfo("America/Denver")


# ── persistence ─────────────────────────────────────────────────────────────

def load_existing_data() -> list:
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    return []


def save_data(records: list) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(records, f, indent=2)


# ── Goldback rate (IMS XML feed) ─────────────────────────────────────────────

def fetch_goldback_rate() -> float | None:
    """
    Fetch the Goldback exchange rate from the IMS XML feed.
    This is the same source used by the official Goldback WordPress plugin
    (goldback-exchange-rate.php), so it's reliable and structured.

    Returns the USD rate (1 Goldback → USD) as a float, or None on failure.
    """
    headers = {"User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )}
    r = requests.get(GOLDBACK_XML_URL, headers=headers, timeout=30)
    r.raise_for_status()

    xml = ET.fromstring(r.text)
    rate_text = xml.findtext("Rate")
    if rate_text is None:
        return None
    val = float(rate_text)
    return round(val, 4) if 1.0 < val < 200.0 else None


# ── Gold spot price (APMEX) ──────────────────────────────────────────────────

def _scan_for_gold_price(obj, depth=0) -> float | None:
    """
    Recursively walk a decoded JSON object looking for a plausible gold
    spot price (1,000–15,000 USD/toz).  Prefers keys with 'spot', 'gold',
    'xau', 'price', 'ask', or 'bid' in the name.
    """
    if depth > 8:
        return None
    if isinstance(obj, dict):
        # First pass: preferred keys
        for k, v in obj.items():
            kl = k.lower()
            if any(s in kl for s in ("spot", "gold", "xau", "price", "ask", "bid")):
                try:
                    val = float(v)
                    if 1_000 < val < 15_000:
                        return round(val, 2)
                except (TypeError, ValueError):
                    pass
        # Second pass: recurse into values
        for v in obj.values():
            result = _scan_for_gold_price(v, depth + 1)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _scan_for_gold_price(item, depth + 1)
            if result:
                return result
    return None


def fetch_gold_spot_from_apmex() -> float | None:
    """
    Fetch the live gold spot price from apmex.com/gold-price.

    APMEX loads prices via internal JSON API calls.  We intercept those
    responses with Playwright before they hit the DOM, so we get the raw
    number rather than scraping formatted text.  Falls back to parsing the
    rendered page if no JSON endpoint is captured.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return fetch_gold_spot_fallback()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        intercepted_price = [None]

        def on_response(response):
            if intercepted_price[0] is not None:
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" not in ct:
                    return
                body = response.json()
                price = _scan_for_gold_price(body)
                if price:
                    intercepted_price[0] = price
            except Exception:
                pass

        page.on("response", on_response)

        try:
            page.goto(APMEX_URL, wait_until="networkidle", timeout=60_000)
        except Exception:
            pass

        page.wait_for_timeout(4_000)

        # DOM fallback: look for a prominent price like "$3,215.40"
        if intercepted_price[0] is None:
            html = page.content()
            for m in re.finditer(r'\$([\d]{1,2},[\d]{3}(?:\.[\d]{2})?)', html):
                val = float(m.group(1).replace(",", ""))
                if 1_000 < val < 15_000:
                    intercepted_price[0] = val
                    break

        browser.close()
        return intercepted_price[0]


def fetch_gold_spot_fallback() -> float | None:
    """No-Playwright fallback: free metals.live endpoint."""
    try:
        r = requests.get("https://api.metals.live/v1/spot/gold", timeout=10)
        data = r.json()
        if isinstance(data, list) and data:
            return float(data[0].get("price") or data[0].get("gold", 0)) or None
        if isinstance(data, dict):
            return float(data.get("price") or data.get("gold", 0)) or None
    except Exception:
        pass
    return None


# ── main ────────────────────────────────────────────────────────────────────

def main() -> None:
    today = datetime.now(tz=MST).strftime("%Y-%m-%d")
    records = load_existing_data()

    if records and records[-1].get("date") == today:
        print(f"Already have data for {today}. Skipping.")
        sys.exit(0)

    print(f"Fetching Goldback rate for {today} …")
    rate = fetch_goldback_rate()

    if rate is None:
        print("ERROR: Could not extract Goldback exchange rate.", file=sys.stderr)
        sys.exit(1)

    print(f"  Goldback rate: ${rate}")

    print("Fetching gold spot price from APMEX …")
    gold_spot = fetch_gold_spot_from_apmex()
    if gold_spot is None:
        print("  APMEX unavailable — falling back to metals.live")
        gold_spot = fetch_gold_spot_fallback()

    print(f"  Gold spot: ${gold_spot}")

    implied_spot = round(rate * 1000, 2) if rate else None

    record = {
        "date": today,
        "rate_usd": rate,
        "gold_spot_usd": gold_spot,
        "implied_spot_usd": implied_spot,
        "scraped_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    records.append(record)
    save_data(records)
    print(f"✓  1 Goldback = ${rate}  |  Gold spot ≈ ${gold_spot}")


if __name__ == "__main__":
    main()
