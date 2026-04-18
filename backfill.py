#!/usr/bin/env python3
"""
backfill.py — One-time script to pull ALL historical Goldback rate data
from the "All Time" chart on goldback.com/exchange-rates/ and merge it
into data/rates.json.

Run once after cloning or whenever you want to resync history:
    python backfill.py

Requires Playwright:
    pip install playwright
    playwright install chromium
"""

import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, date
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, Request, Response
except ImportError:
    print("ERROR: playwright not installed.  Run: pip install playwright && playwright install chromium")
    sys.exit(1)

DATA_FILE    = Path(__file__).parent / "data" / "rates.json"
GOLDBACK_URL = "https://www.goldback.com/exchange-rates/"


# ── helpers ─────────────────────────────────────────────────────────────────

def load_existing() -> dict:
    """Return existing records keyed by date string."""
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            records = json.load(f)
        return {r["date"]: r for r in records}
    return {}


def save_records(by_date: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    records = sorted(by_date.values(), key=lambda r: r["date"])
    with open(DATA_FILE, "w") as f:
        json.dump(records, f, indent=2)
    print(f"✓  Saved {len(records)} records → {DATA_FILE}")


def normalise_date(raw) -> str | None:
    """
    Accept multiple date formats and return 'YYYY-MM-DD' or None.
    Handles: epoch ms, epoch s, 'YYYY-MM-DD', 'MM/DD/YYYY', 'M/D/YY', etc.
    """
    if raw is None:
        return None
    try:
        # Unix epoch in milliseconds
        if isinstance(raw, (int, float)) and raw > 1_000_000_000_000:
            return datetime.utcfromtimestamp(raw / 1000).strftime("%Y-%m-%d")
        # Unix epoch in seconds
        if isinstance(raw, (int, float)) and raw > 1_000_000_000:
            return datetime.utcfromtimestamp(raw).strftime("%Y-%m-%d")
        s = str(raw).strip()
        # Already ISO
        if re.match(r'^\d{4}-\d{2}-\d{2}', s):
            return s[:10]
        # MM/DD/YYYY or M/D/YYYY
        m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', s)
        if m:
            mo, dy, yr = m.groups()
            yr = int(yr)
            if yr < 100:
                yr += 2000
            return f"{yr:04d}-{int(mo):02d}-{int(dy):02d}"
    except Exception:
        pass
    return None


def plausible_rate(val) -> float | None:
    """Return val as a float if it looks like a Goldback USD rate, else None."""
    try:
        v = float(val)
        if 1.0 < v < 200.0:
            return round(v, 2)
    except (TypeError, ValueError):
        pass
    return None


# ── response parsers ─────────────────────────────────────────────────────────

def parse_json_response(body: dict | list) -> list[dict]:
    """
    Try every plausible shape of JSON that a chart API might return.
    Returns a list of {date, rate_usd} dicts.
    """
    results = []

    def try_item(item):
        """One object that might have date + rate fields."""
        if not isinstance(item, dict):
            return
        date_val = None
        rate_val = None
        for k, v in item.items():
            kl = k.lower()
            if date_val is None and any(s in kl for s in ("date", "time", "day", "period")):
                date_val = normalise_date(v)
            if rate_val is None and any(s in kl for s in ("rate", "value", "price", "close", "exchange")):
                rate_val = plausible_rate(v)
        if date_val and rate_val:
            results.append({"date": date_val, "rate_usd": rate_val})

    # Shape 1: top-level array of objects  [{date, rate}, …]
    if isinstance(body, list):
        for item in body:
            try_item(item)
        if results:
            return results

    if isinstance(body, dict):
        # Shape 2: {data: [{date, rate}, …]}  or  {rates: […]}  etc.
        for key in ("data", "rates", "history", "values", "chart", "records", "items", "results"):
            if key in body and isinstance(body[key], list):
                for item in body[key]:
                    try_item(item)
                if results:
                    return results

        # Shape 3: parallel arrays  {dates: […], values: […]}
        date_arr = rate_arr = None
        for k, v in body.items():
            kl = k.lower()
            if isinstance(v, list) and any(s in kl for s in ("date", "time", "label", "x")):
                date_arr = v
            if isinstance(v, list) and any(s in kl for s in ("rate", "value", "price", "close", "y")):
                rate_arr = v
        if date_arr and rate_arr and len(date_arr) == len(rate_arr):
            for d, r in zip(date_arr, rate_arr):
                d2 = normalise_date(d)
                r2 = plausible_rate(r)
                if d2 and r2:
                    results.append({"date": d2, "rate_usd": r2})
            if results:
                return results

        # Shape 4: flat dict  {"2024-01-15": 7.34, …}
        for k, v in body.items():
            d2 = normalise_date(k)
            r2 = plausible_rate(v)
            if d2 and r2:
                results.append({"date": d2, "rate_usd": r2})
        if results:
            return results

    return results


def parse_xml_response(text: str) -> list[dict]:
    """
    Parse an XML payload that might contain Goldback rate history.
    Handles both attribute-based and text-based element structures.
    """
    results = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return results

    def walk(node):
        attribs = {k.lower(): v for k, v in node.attrib.items()}
        children = list(node)

        date_val = rate_val = None

        # From attributes
        for k, v in attribs.items():
            if any(s in k for s in ("date", "time", "day")):
                date_val = date_val or normalise_date(v)
            if any(s in k for s in ("rate", "value", "price", "close")):
                rate_val = rate_val or plausible_rate(v)

        # From child text
        for child in children:
            tag = child.tag.lower().split("}")[-1]  # strip namespace
            text_val = (child.text or "").strip()
            if any(s in tag for s in ("date", "time", "day")) and text_val:
                date_val = date_val or normalise_date(text_val)
            if any(s in tag for s in ("rate", "value", "price", "close")) and text_val:
                rate_val = rate_val or plausible_rate(text_val)

        if date_val and rate_val:
            results.append({"date": date_val, "rate_usd": rate_val})

        for child in children:
            walk(child)

    walk(root)
    return results


def extract_from_chartjs(page) -> list[dict]:
    """
    Read Chart.js / ApexCharts data directly from the browser's JavaScript
    heap.  This catches cases where the chart data is embedded in a <script>
    tag or set via JS rather than fetched from an API.
    """
    try:
        raw = page.evaluate("""
        () => {
            const out = { chartjs: null, apex: null, globals: {} };

            // Chart.js 3+ / 4+
            if (typeof Chart !== 'undefined') {
                const instances = Object.values(Chart.instances || {});
                if (instances.length) {
                    out.chartjs = instances.map(c => ({
                        labels:   c.data && c.data.labels,
                        datasets: c.data && c.data.datasets && c.data.datasets.map(d => d.data)
                    }));
                }
            }

            // ApexCharts
            if (typeof Apex !== 'undefined' && Apex._chartInstances) {
                out.apex = Apex._chartInstances.map(c => c.opts && c.opts.series);
            }

            // Hunt for goldback-shaped global variables
            const suspects = ['goldbackData','gbRates','chartData','rateData',
                              'exchangeData','historicalRates','rateHistory'];
            for (const name of suspects) {
                try { if (window[name]) out.globals[name] = window[name]; }
                catch(e) {}
            }

            return out;
        }
        """)
    except Exception:
        return []

    results = []

    # Chart.js: labels = dates, datasets[0] = values
    if raw.get("chartjs"):
        for chart in raw["chartjs"]:
            labels   = chart.get("labels") or []
            datasets = chart.get("datasets") or []
            values   = datasets[0] if datasets else []
            if labels and values and len(labels) == len(values):
                for lbl, val in zip(labels, values):
                    d = normalise_date(lbl)
                    r = plausible_rate(val)
                    if d and r:
                        results.append({"date": d, "rate_usd": r})
        if results:
            return results

    # ApexCharts: series[0].data = [{x: date, y: value}]
    if raw.get("apex"):
        for series_group in raw["apex"]:
            if not series_group:
                continue
            for series in series_group:
                if not isinstance(series, dict):
                    continue
                for pt in (series.get("data") or []):
                    if isinstance(pt, dict):
                        d = normalise_date(pt.get("x"))
                        r = plausible_rate(pt.get("y"))
                    elif isinstance(pt, list) and len(pt) >= 2:
                        d = normalise_date(pt[0])
                        r = plausible_rate(pt[1])
                    else:
                        continue
                    if d and r:
                        results.append({"date": d, "rate_usd": r})
        if results:
            return results

    # Arbitrary global variables
    for obj in raw.get("globals", {}).values():
        parsed = parse_json_response(obj)
        if parsed:
            results.extend(parsed)

    return results


def extract_from_inline_scripts(page) -> list[dict]:
    """
    Search every <script> tag's text for date+rate arrays embedded as
    JavaScript literals — common with Bricks Builder and WP shortcodes.
    """
    results = []
    try:
        scripts = page.query_selector_all("script:not([src])")
        for s in scripts:
            text = s.inner_text() or ""
            # Look for ISO date + decimal pairs close together
            pairs = re.findall(
                r'["\'](\d{4}-\d{2}-\d{2})["\'].*?:\s*([\d]+\.[\d]{2})',
                text, re.DOTALL
            )
            for d, r in pairs:
                r2 = plausible_rate(r)
                if r2:
                    results.append({"date": d, "rate_usd": r2})

            # Also match JS arrays:  ["2024-01-15", 7.34],
            pairs2 = re.findall(
                r'\[["\'](20\d\d-\d{2}-\d{2})["\'],\s*([\d]+\.[\d]{1,4})\]',
                text
            )
            for d, r in pairs2:
                r2 = plausible_rate(r)
                if r2:
                    results.append({"date": d, "rate_usd": r2})
    except Exception:
        pass
    return results


# ── main scraping logic ──────────────────────────────────────────────────────

def scrape_history() -> list[dict]:
    """
    Visit goldback.com/exchange-rates/, intercept every network response,
    and also inspect Chart.js/ApexCharts heap data + inline scripts.
    Returns a deduplicated list of {date, rate_usd} dicts.
    """
    captured: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        def on_response(response: Response):
            try:
                ct = response.headers.get("content-type", "")
                status = response.status
                if status < 200 or status >= 300:
                    return

                # JSON
                if "json" in ct:
                    body = response.json()
                    rows = parse_json_response(body)
                    if rows:
                        print(f"  [net-intercept JSON] {response.url[:80]}  → {len(rows)} rows")
                        captured.extend(rows)

                # XML (Ideal Managed Solutions API uses XML)
                elif "xml" in ct or "text/plain" in ct:
                    text = response.text()
                    if "<" in text:
                        rows = parse_xml_response(text)
                        if rows:
                            print(f"  [net-intercept XML]  {response.url[:80]}  → {len(rows)} rows")
                            captured.extend(rows)
            except Exception:
                pass

        page.on("response", on_response)

        print(f"Loading {GOLDBACK_URL} …")
        page.goto(GOLDBACK_URL, wait_until="networkidle", timeout=90_000)

        # Try clicking the "All Time" tab to trigger a full-history fetch
        for selector in [
            'text="All Time"', 'text="All time"', 'text="ALL TIME"',
            '[data-period="all"]', '[data-range="all"]', '.all-time',
            'button:has-text("All")', 'a:has-text("All Time")',
        ]:
            try:
                page.click(selector, timeout=3_000)
                print(f'  Clicked "{selector}" tab')
                page.wait_for_timeout(4_000)
                break
            except Exception:
                pass
        else:
            page.wait_for_timeout(6_000)

        # Strategy B: Chart.js / ApexCharts heap
        if not captured:
            print("  No network intercepts — trying Chart.js heap …")
            rows = extract_from_chartjs(page)
            if rows:
                print(f"  [chartjs heap] → {len(rows)} rows")
                captured.extend(rows)

        # Strategy C: inline <script> literals
        if not captured:
            print("  Trying inline <script> scan …")
            rows = extract_from_inline_scripts(page)
            if rows:
                print(f"  [inline scripts] → {len(rows)} rows")
                captured.extend(rows)

        browser.close()

    return captured


# ── merge & save ─────────────────────────────────────────────────────────────

def merge(existing: dict, new_rows: list[dict]) -> tuple[dict, int]:
    """
    Merge new_rows into existing records.
    For historical backfill rows we do NOT overwrite gold_spot_usd /
    implied_spot_usd that were already set by the daily scraper.
    We DO fill in null gold_spot / implied_spot for older backfill entries.
    """
    added = 0
    for row in new_rows:
        d = row["date"]
        rate = row["rate_usd"]
        if d not in existing:
            existing[d] = {
                "date": d,
                "rate_usd": rate,
                "gold_spot_usd": None,
                "implied_spot_usd": round(rate * 1000, 2),
            }
            added += 1
        else:
            # Backfill only fills missing rate values (don't overwrite scraper data)
            if existing[d].get("rate_usd") is None:
                existing[d]["rate_usd"] = rate
                existing[d]["implied_spot_usd"] = round(rate * 1000, 2)
    return existing, added


def main():
    print("=" * 60)
    print("Goldback Historical Backfill")
    print("=" * 60)

    existing = load_existing()
    print(f"Existing records: {len(existing)}")

    new_rows = scrape_history()

    if not new_rows:
        print("\nNo historical data extracted.")
        print("Possible reasons:")
        print("  • Goldback's chart data is loaded from an endpoint we")
        print("    haven't seen yet.  Run with PWDEBUG=1 to open a browser")
        print("    and watch Network tab for the API call:")
        print("      PWDEBUG=1 python backfill.py")
        print("  • The 'All Time' button wasn't found — check the selector.")
        sys.exit(1)

    # Remove obvious duplicates from captured list
    seen = {}
    for row in new_rows:
        d = row["date"]
        if d not in seen:
            seen[d] = row

    new_rows = list(seen.values())
    print(f"\nUnique date-rate pairs extracted: {len(new_rows)}")

    existing, added = merge(existing, new_rows)
    print(f"New records added:                {added}")
    print(f"Total records after merge:        {len(existing)}")

    save_records(existing)


if __name__ == "__main__":
    main()
