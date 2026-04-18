#!/usr/bin/env python3
"""
Goldback Exchange Rate Scraper
Fetches the daily rate from goldback.com and appends to data/rates.json
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

DATA_FILE = Path(__file__).parent / "data" / "rates.json"
URL = "https://www.goldback.com/exchange-rates/"
MST = ZoneInfo("America/Denver")


def load_existing_data() -> list:
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    return []


def save_data(records: list) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(records, f, indent=2)


def extract_rate_from_html(html: str) -> float | None:
    """
    Try several patterns to find the USD rate in rendered HTML.
    The Goldback rate is currently in the $5–$50 range (as of 2026).
    """
    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: find h2 elements — the rate lives in an <h2> on the exchange page
    for tag in soup.find_all(["h2", "h3", "span", "p"]):
        text = tag.get_text(strip=True)
        # Match a bare dollar amount like "$9.73" or "9.73"
        m = re.search(r'^\$?([\d]{1,3}\.[\d]{2})$', text)
        if m:
            val = float(m.group(1))
            if 1.0 < val < 200.0:
                return round(val, 2)

    # Strategy 2: regex sweep over full HTML
    patterns = [
        r'"rate"\s*:\s*([\d]+\.[\d]{2,4})',           # JSON key
        r'"exchange_rate"\s*:\s*([\d]+\.[\d]+)',
        r'data-rate="([\d]+\.[\d]+)"',
        r'exchangeRate\s*[=:]\s*([\d]+\.[\d]+)',
        r'1\s*Goldback[^$]*\$\s*([\d]+\.[\d]{2})',    # "1 Goldback = $9.73"
        r'\$\s*([\d]{1,3}\.[\d]{2})\b',               # "$9.73" anywhere
    ]
    for pat in patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            val = float(m.group(1))
            # Real Goldback rate: ~$2.50 (2019) to ~$50 (far future)
            if 1.0 < val < 200.0:
                return round(val, 4)

    return None


def fetch_with_requests() -> float | None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    r = requests.get(URL, headers=headers, timeout=30)
    r.raise_for_status()
    return extract_rate_from_html(r.text)


def fetch_with_playwright() -> float | None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60_000)

        # Wait until an h2 on the page contains a dollar amount (the live rate)
        try:
            page.wait_for_function(
                "() => Array.from(document.querySelectorAll('h2, h3, span'))"
                ".some(el => /^\\$?[\\d]{1,3}\\.[\\d]{2}$/.test(el.textContent.trim()))",
                timeout=20_000,
            )
        except Exception:
            # Fallback: just wait a few extra seconds for JS to settle
            page.wait_for_timeout(5_000)

        html = page.content()
        browser.close()
        return extract_rate_from_html(html)


def fetch_gold_spot_usd() -> float | None:
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


def main() -> None:
    today = datetime.now(tz=MST).strftime("%Y-%m-%d")
    records = load_existing_data()

    if records and records[-1].get("date") == today:
        print(f"Already have data for {today}. Skipping.")
        sys.exit(0)

    print(f"Fetching Goldback rate for {today} …")

    rate = fetch_with_requests()
    if rate is None and PLAYWRIGHT_AVAILABLE:
        print("  requests found no rate — trying Playwright …")
        rate = fetch_with_playwright()

    if rate is None:
        print("ERROR: Could not extract exchange rate from page.", file=sys.stderr)
        sys.exit(1)

    gold_spot = fetch_gold_spot_usd()
    # 1 Goldback = 1/1000 troy oz of 24k gold → implied gold spot = rate × 1000
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
    print(f"✓ Saved: 1 Goldback = ${rate} USD  (gold spot ~${gold_spot})")


if __name__ == "__main__":
    main()
