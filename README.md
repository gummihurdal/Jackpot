# Jackpot - Spring Radar

Worldwide scan for strong low- and mid-cap companies trading near the bottom of a wide 52-week range, with hard safety gates against dilution and bankruptcy and a dedicated pharma track.

Site: https://gummihurdal.github.io/Jackpot/

- `scan.py` - the scan engine (Yahoo Finance data, no API key needed)
- `index.html` - the radar page
- `data/latest.json` - latest scan, `data/tracker.json` - log of every top-40 pick and its return since
- `.github/workflows/radar.yml` - runs the scan every weekday at 22:40 UTC and commits the results

Run it by hand: `pip install yfinance pandas && python scan.py`

A mechanical screen, not investment advice.
