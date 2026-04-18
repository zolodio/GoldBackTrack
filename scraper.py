#!/usr/bin/env python3
"""
Goldback Exchange Rate Scraper
Fetches the daily Goldback rate from goldback.com and the gold spot price
from apmex.com, then appends both to data/rates.json.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

DATA_FILE    = Path(__file__).parent / "data" / "rates.json"
GOLDBACK_URL = "https://www.goldback.com/exchange-rates/"
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


# ── Goldback rate ────────────────────────────────────────────────────────────

def extract_rate_from_html(html: str) -> float | None:
    """Try several strategies to extract the 1-Goldback USD rate."""
    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: element whose entire visible text is a bare dollar amount
    for tag in soup.find_all(["h2", "h3", "span", "p"]):
        text = tag.get_text(strip=True)
        m = re.search(r'^\$?([\d]{1,3}\.[\d]{2})$', text)
        if m:
            val = float(m.group(1))
            if 1.0 < val < 200.0:
                return round(val, 2)

    # Strategy 2: regex patterns against the raw HTML / JSON blob
    patterns = [
        r'"rate"\s*:\s*([\d]+\.[\d]{2,4})',
        r'"exchange_rate"\s*:\s*([\d]+\.[\d]+)',
        r'data-rate="([\d]+\.[\d]+)"',
        r'exchangeRate\s*[=:]\s*([\d]+\.[\d]+)',
        r'1\s*Goldback[^$]*\$\s*([\d]+\.[\d]{2})',
        r'\$\s*([\d]{1,3}\.[\d]{2})\b',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            val = float(m.group(1))
            if 1.0 < val < 200.0:
                return round(val, 4)

    return None


def fetch_goldback_rate_requests() -> float | None:
    headers = {"User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )}
    r = requests.get(GOLDBACK_URL, headers=headers, timeout=30)
    r.raise_for_status()
    return extract_rate_from_html(r.text)


def fetch_goldback_rate_playwright() -> float | None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        intercepted_rate = [None]

        def on_response(response):
            if intercepted_rate[0] is not None:
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct or "xml" in ct:
                    try:
                        body = response.json()
                        text = json.dumps(body)
                    except Exception:
                        text = response.text()
                    rate = extract_rate_from_html(text)
                    if rate:
                        intercepted_rate[0] = rate
            except Exception:
                pass

        page.on("response", on_response)
        page.goto(GOLDBACK_URL, wait_until="networkidle", timeout=60_000)

        try:
            page.wait_for_function(
                "() => Array.from(document.querySelectorAll('h2,h3,span'))"
                ".some(el => /^\\$?[\\d]{1,3}\\.[\\d]{2}$/.test(el.textContent.trim()))",
                timeout=20_000,
            )
        except Exception:
            page.wait_for_timeout(5_000)

        if intercepted_rate[0] is None:
            intercepted_rate[0] = extract_rate_from_html(page.content())

        browser.close()
        return intercepted_rate[0]


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
    rate = fetch_goldback_rate_requests()
    if rate is None and PLAYWRIGHT_AVAILABLE:
        print("  requests found no rate — trying Playwright …")
        rate = fetch_goldback_rate_playwright()

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
