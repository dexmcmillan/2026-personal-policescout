# Police Scout — Design Spec
**Date:** 2026-03-12

## Overview

A weekday-automated web scraper that monitors the press release pages of 35 Canadian police services, detects new releases, and publishes a plain HTML digest via GitHub Pages. Forked from `2026-personal-datascout`.

## Goals

- Monitor 35 Canadian police service websites for new press releases
- Show only what is new since the last run (deduplication)
- Publish a clean, minimal HTML digest daily (weekdays)
- Archive each day's digest
- No AI filtering — show everything new, unfiltered

## Non-Goals

- Full press release text extraction (title + link only)
- AI scoring or summarization
- RSS feed detection
- Per-site custom scrapers

---

## Project Structure

```
2026-personal-policescout/
├── .github/
│   └── workflows/
│       └── scan.yml              # Weekday cron + manual trigger
├── data/
│   └── seen_items.json           # Dedup state: MD5(title|url) → ISO timestamp
├── docs/
│   ├── index.html                # Today's digest (GitHub Pages root)
│   └── archive/
│       └── YYYY-MM-DD.html       # Daily archive
├── templates/
│   └── digest.html               # Jinja2 HTML template
├── sources.csv                   # 35 police services: name, url
├── scan.py                       # Main script
├── .python-version               # "3.12"
└── pyproject.toml                # Dependencies: requests, beautifulsoup4, jinja2
```

---

## Data Source

`sources.csv` — 35 Canadian police services, populated from the verified list provided. Columns: `Name of police service`, `url`.

---

## scan.py Logic

Runs top-to-bottom as a single script:

1. **Load state** — read `data/seen_items.json`; initialize empty dict if missing
2. **Load sources** — read `sources.csv`, extract name + url pairs
3. **Scrape each site** — for each service:
   - `requests.get(url, timeout=15)` with a browser-like User-Agent header
   - Parse with BeautifulSoup
   - Extract anchor tags from listing page; heuristically identify press release links (links within list/article/table elements, or links whose text or href contains release-related keywords)
   - Collect: `{title, url, service_name}`
   - On any exception (timeout, HTTP error, parse failure): log the failure, add service to a failed list, continue
4. **Deduplicate** — compute MD5 of `title|url` for each item; skip if hash exists in `seen_items.json`
5. **Collect new items** — list of new items grouped by service name, sorted alphabetically
6. **Render HTML** — render Jinja2 template with new items, date, failed services list; write to `docs/index.html` and `docs/archive/YYYY-MM-DD.html`
7. **Update state** — add hashes for all new items with current timestamp; prune entries older than 30 days; write `data/seen_items.json`

---

## Output HTML (digest.html template)

**Structure:**
- Header: "Police Press Release Digest" + formatted date
- Body (if new items exist):
  - For each police service with new releases (alphabetical):
    - Service name as `<h2>`
    - Unordered list of new releases, each as a hyperlink (`<a href="...">title</a>`) opening in a new tab
- Empty state: single message — "No new press releases found today."
- Failed sites notice (if any): small section at page bottom listing services that could not be scraped
- Footer: archive links for the last 30 days (most recent first)

**Styling:**
- Minimal CSS inline in the template
- Readable body font, modest padding, no grid layout
- Clean utility aesthetic — not the newspaper style of data-scout

---

## GitHub Actions Workflow (scan.yml)

- **Schedule:** `0 12 * * 1-5` (noon UTC = 7 AM ET, Monday–Friday)
- **Manual trigger:** `workflow_dispatch`
- **Steps:**
  1. Checkout repo
  2. Install `uv` (astral-sh/setup-uv)
  3. Set up Python 3.12
  4. `uv sync`
  5. `python scan.py`
  6. Commit and push `docs/` and `data/seen_items.json`
  7. Commit message: `"Update Police Scout digest for YYYY-MM-DD"`
- **Secrets:** None required beyond default `GITHUB_TOKEN`

---

## Dependencies

```toml
dependencies = [
    "beautifulsoup4>=4.14.3",
    "jinja2>=3.1.6",
    "requests>=2.32.5",
]
```

No Gemini/AI dependency.

---

## State Management

- `seen_items.json`: dict of `MD5(title|url) → ISO timestamp`
- Entries older than 30 days are pruned on each run
- Prevents duplicate press releases across runs

---

## Known Limitations / Expected First-Run Behaviour

- Heuristic link extraction will work well on most sites but may miss some releases or include false positives on the first few runs. Site-specific tweaks are expected after observing real output.
- Some services in the source list have no dedicated news page and rely primarily on Facebook — these will likely fail gracefully and appear in the failed sites notice.
- First run will mark all currently visible items as "seen" without surfacing them, establishing the baseline state.
