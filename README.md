# ◈ Goldback Exchange Tracker

A GitHub Pages dashboard that **automatically scrapes the Goldback daily exchange rate** from [goldback.com](https://www.goldback.com/exchange-rates/) every morning and displays a beautiful, data-rich UI.

## ✦ Features

- **Daily auto-scraping** via GitHub Actions (triggers at 10:05 AM MST, just after Goldback's 10 AM update)
- **Interactive rate chart** with 1M / 3M / 6M / 1Y / ALL time ranges
- **Full denomination table** — live USD values for all 9 denominations (¼ GB through 100 GB)
- **Transaction calculator** — convert any quantity/denomination to USD instantly
- **Annual returns bar chart** — year-by-year performance
- **Key stats** — ATH, YTD, 30-day, 1-year, and since-inception change
- **Full history table** with daily change % and gold spot premium
- Seed data from Jan 2024 → present included

## 🚀 Setup (5 minutes)

### 1. Fork / clone this repo
```bash
git clone https://github.com/YOUR_USERNAME/goldback-tracker
cd goldback-tracker
```

### 2. Enable GitHub Pages
In your repo → **Settings → Pages → Source: `main` branch, `/ (root)`**

Your dashboard will be live at `https://YOUR_USERNAME.github.io/goldback-tracker`

### 3. Enable GitHub Actions
Go to **Actions** tab → click **"I understand my workflows, go ahead and enable them"**

The workflow runs daily at 10:05 AM MST automatically.  
You can also trigger it manually: Actions → **Daily Goldback Rate Scraper** → **Run workflow**.

### 4. Grant write permissions
Repo → **Settings → Actions → General → Workflow permissions** → select **"Read and write permissions"**

That's it! The action will commit updated `data/rates.json` each day.

## 📁 Structure

```
goldback-tracker/
├── index.html              # Dashboard UI
├── scraper.py              # Python scraper (requests + Playwright fallback)
├── data/
│   └── rates.json          # Historical rate data (appended daily)
└── .github/
    └── workflows/
        └── scrape.yml      # Daily GitHub Actions workflow
```

## 🔧 Local development

```bash
pip install requests beautifulsoup4 playwright
playwright install chromium
python scraper.py          # scrapes today's rate and appends to data/rates.json

# Serve the dashboard locally
python -m http.server 8080
# open http://localhost:8080
```

## 📊 Data format

`data/rates.json` is a JSON array of daily records:

```json
[
  {
    "date": "2026-04-14",
    "rate_usd": 0.5621,
    "gold_spot_usd": 3187.00,
    "implied_spot_usd": 562.10,
    "scraped_at": "2026-04-14T17:07:23+00:00"
  }
]
```

## ⚠️ Notes

- Goldback's exchange rate page loads dynamically. The scraper uses `requests` first, then falls back to **Playwright** (headless Chromium) if needed.
- The seed data in `data/rates.json` contains approximate historical values reconstructed from gold price data; live scraping from the action date forward will have exact official rates.
- Gold spot prices are fetched from `api.metals.live` (free, no key required).
