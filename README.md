# Jackpot - Spring Radar

Worldwide scan for safe low- and mid-cap companies trading near the bottom of a wide 52-week range,
with hard gates against dilution and bankruptcy, a pharma pipeline track, a watchlist and a
tracker that measures the picks against the market.

Site: https://gummihurdal.github.io/Jackpot/

## Layout

| Path | What it is |
|---|---|
| `scan.py` | Runs the full scan |
| `collect_news.py` | Light job between scans: files feed headlines about radar stocks |
| `radar/config.py` | Every rule and threshold in one place |
| `radar/universe.py` | Worldwide universe from the Yahoo Finance screener |
| `radar/sources.py` | Fundamentals, share history, SEC EDGAR filings, ClinicalTrials.gov, news |
| `radar/gates.py` | The safety gate (funding, debt, liquidity, dilution, filings, news) |
| `radar/scores.py` | Quality, Safety, Setup, Potential and the Spring score |
| `radar/store.py` | Local database: Parquet files queried with DuckDB |
| `radar/watch.py` | Watchlist evaluation |
| `radar/pipeline.py` | The scan, end to end |
| `tests/` | Offline tests of the rules (`python -m unittest discover -s tests`) |
| `data/watchlist.json` | Names to always evaluate. Edit this file to add or remove one |
| `data/latest.json` | Latest scan, read by `index.html` |
| `data/db/` | Daily snapshots, news store and caches |

## Run

```
pip install -r requirements.txt
python scan.py                  # full scan, about 6 minutes with warm caches
python scan.py --regions no,se  # quick test
```

GitHub Actions runs the scan every weekday at 22:40 UTC and the news collector every 4 hours,
then copies `main` to `gh-pages`, which serves the site.

The SEC asks automated readers to name a contact e-mail. Set a real one in `radar/config.py`
(`CONTACT`) or as the `RADAR_CONTACT` environment variable.

A mechanical screen, not investment advice.
