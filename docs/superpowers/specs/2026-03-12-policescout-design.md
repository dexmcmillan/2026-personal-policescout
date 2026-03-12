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
│   └── seen_items.json           # Dedup state: MD5(title|url) → ISO timestamp (UTC)
├── docs/
│   ├── index.html                # Today's digest (GitHub Pages root)
│   └── archive/
│       └── YYYY-MM-DD.html       # Daily archive files
├── templates/
│   └── digest.html               # Jinja2 HTML template (separate file)
├── sources.csv                   # 35 police services: name, url
├── scan.py                       # Main script
├── .python-version               # "3.12"
└── pyproject.toml                # Dependencies: requests, beautifulsoup4, jinja2
```

---

## Data Source

`sources.csv` — 35 Canadian police services. UTF-8 encoded, standard CSV quoting. Exact column headers: `Name of police service`, `url`. Read with Python's `csv.DictReader`.

---

## scan.py Logic

At script start, capture `today_utc = datetime.utcnow().date()` once. Use this single value for all date references (archive filename, digest heading, pruning threshold) throughout the run.

Runs top-to-bottom as a single script:

1. **Load state** — read `data/seen_items.json`; initialize empty dict if file is missing
2. **Load sources** — read `sources.csv` with `csv.DictReader`, extract `Name of police service` + `url` pairs
3. **Scrape each site** — for each service:
   - `requests.get(url, timeout=15)` with a browser-like User-Agent header; no retries
   - Parse with BeautifulSoup (`html.parser`)
   - **Link extraction heuristic:**
     - Step A: find all `<a>` tags whose `href` starts with `http`, `https`, or `/` (positive rule — this excludes `#…`, `mailto:`, `tel:`, `javascript:`, and empty hrefs), and whose stripped `.get_text()` is non-empty. Filter to only those `<a>` tags that are descendants of a `<main>`, `<article>`, `<ul>`, `<ol>`, or `<table>` element.
     - Step B: if Step A yields zero qualifying links, fall back to all `<a>` tags matching the same href and text rules across the whole page, excluding any that are descendants of `<nav>`, `<footer>`, or `<header>` elements.
     - Use `.strip()` (Python whitespace stripping only — no Unicode normalization, no collapsing of internal whitespace) on link text to get the title.
     - Resolve relative URLs (those starting with `/`) to absolute using the base URL of the scraped page (`urljoin(url, href)`).
   - Collect: `{"title": title, "url": absolute_url, "service_name": service_name}` for each extracted link
   - On any exception (requests exception, HTTP error status, BeautifulSoup parse failure): log to stdout, add service name to a failed list, continue to next service. Do not modify state for failed services.
4. **Deduplicate** — for each scraped item, compute `MD5((title.strip() + "|" + url.strip()).encode("utf-8")).hexdigest()`; skip if that hash already exists in `seen_items.json`
5. **Collect new items** — items whose hash is not in state, grouped by `service_name`, sorted alphabetically by service name. Services with no new items are omitted from the output entirely.
6. **Render HTML** — pass the following context to `templates/digest.html`:
   - `date_str`: formatted date string (e.g., `"Thursday, March 12, 2026"`)
   - `services`: list of dicts `{"name": str, "items": [{"title": str, "url": str}, ...]}`, sorted alphabetically by `name`, only services with new items included
   - `failed`: list of service name strings that failed to scrape (empty list if none)
   - `archive_links`: list of dicts `{"date": str, "filename": str}` for the 30 most recent files found in `docs/archive/` by filename sort (most recent first), where `date` is the raw ISO filename stem (e.g., `"2026-03-11"`) and `filename` is the relative path from `docs/` (e.g., `"archive/2026-03-11.html"`). Today's archive file (just written) is **included** in the list — it should appear as the first entry. The `href` in the template uses `filename` directly (e.g., `<a href="archive/2026-03-11.html">`), which works correctly since `index.html` is served from the `docs/` root on GitHub Pages.
   - Write rendered HTML to `docs/index.html` (overwrite) and `docs/archive/{today_utc}.html`
7. **Update state** — prune existing state entries whose stored timestamp is more than 30 calendar days before `today_utc` (UTC). Then merge new item hashes into state: for each new item (those not already in state), add `hash → datetime.utcnow().isoformat() + "Z"`. Write updated state to `data/seen_items.json` unconditionally (always rewrite the file, even if nothing changed).
8. **Commit behaviour** — scan.py does not commit. The GitHub Actions step handles git operations after the script exits.

### First-Run Baseline Behaviour

On the first run, `seen_items.json` does not exist. The script initializes an empty state, scrapes all sites, and treats every discovered item as "new." All items are written to the digest HTML and added to state. Subsequent runs will only surface items not already in state. The first run will produce a large digest. This is intentional.

### All-Sites-Failed Behaviour

If every site fails to scrape (zero successful scrapes), the script still writes `docs/index.html` and `docs/archive/{today_utc}.html` (showing "No new press releases found today." with the failed sites list), and still rewrites `seen_items.json` (pruning only). This is correct — the digest documents that a run occurred and which sites failed.

### Directory Creation

`scan.py` must create `docs/archive/` if it does not exist (using `Path.mkdir(parents=True, exist_ok=True)`) before writing archive files. This handles fresh clones and the first run.

---

## Output HTML (`templates/digest.html`)

A separate Jinja2 template file (not an inline string in scan.py).

**Template context variables** (as passed from scan.py above):
- `date_str` — string
- `services` — list of `{name, items[{title, url}]}` dicts
- `failed` — list of strings
- `archive_links` — list of `{date, filename}` dicts

**Structure:**
- `<h1>`: "Police Press Release Digest" + `{{ date_str }}`
- Body (if `services` is non-empty):
  - For each service in `services`:
    - `<h2>`: `{{ service.name }}`
    - `<ul>`: one `<li>` per item — `<a href="{{ item.url }}" target="_blank" rel="noopener">{{ item.title }}</a>`
- Empty state (if `services` is empty): `<p>No new press releases found today.</p>`
- Failed sites section (rendered only if `failed` is non-empty):
  - `<h2>Sites that could not be scraped</h2>`
  - `<ul>`: one `<li>` per failed service name
- Footer:
  - Text: "Archive:"
  - For each link in `archive_links`: `<a href="{{ link.filename }}">{{ link.date }}</a>` separated by pipe characters or line breaks

**Styling:**
- Minimal CSS in a `<style>` block in the template
- System font stack (`font-family: system-ui, sans-serif`), `line-height: 1.6`, modest padding
- `max-width: 800px` with `margin: 0 auto` for centered body
- No grid, no colour-coded tags, no newspaper aesthetic

---

## GitHub Actions Workflow (`scan.yml`)

- **Schedule:** `0 12 * * 1-5` (noon UTC = 7 AM ET, Monday–Friday)
- **Manual trigger:** `workflow_dispatch`
- **Steps:**
  1. Checkout repo with `fetch-depth: 0`
  2. Install `uv` (astral-sh/setup-uv)
  3. Set up Python 3.12
  4. `uv sync`
  5. `uv run python scan.py`
  6. `git config user.name "github-actions"` and `git config user.email "github-actions@github.com"`
  7. `git add docs/ data/seen_items.json`
  8. `git diff --cached --quiet || git commit -m "Update Police Scout digest for $(date -u +%Y-%m-%d)"` — only commits if there are staged changes; avoids `--allow-empty`. Commit message format: `"Update Police Scout digest for YYYY-MM-DD"` (e.g., `"Update Police Scout digest for 2026-03-12"`)
  9. `git push`
- **Secrets:** None required beyond default `GITHUB_TOKEN` for push access

---

## Dependencies

```toml
dependencies = [
    "beautifulsoup4>=4.14.3",
    "jinja2>=3.1.6",
    "requests>=2.32.5",
]
```

No Gemini/AI dependency. `lxml` is not required; `html.parser` is used.

---

## State Management

- **File:** `data/seen_items.json` — committed to the repo, always rewritten on each run
- **Format:** JSON object: `{ "<md5_hash>": "<utc_iso_timestamp>", ... }`
- **Key:** `MD5((title.strip() + "|" + url.strip()).encode("utf-8")).hexdigest()`
- **Value:** UTC ISO 8601 timestamp, e.g. `"2026-03-12T12:03:41Z"`
- **Pruning order:** prune stale entries first, then merge new hashes; pruning threshold is 30 calendar days before `today_utc`
- **Failed sites:** state entries for services that fail to scrape are left unchanged

---

## Known Limitations

- Heuristic link extraction will work on most sites but may produce false positives (navigation links) or miss releases on some sites. Site-specific tweaks are expected after observing real output.
- Some services in the source list have no dedicated news page and rely primarily on Facebook — these will fail gracefully (HTTP error or no qualifying links) and appear in the failed sites notice.
- No retry logic — transient network failures cause a site to appear in the failed list for that run only; it will be retried on the next scheduled run.
- Sites that require JavaScript rendering will not be scraped correctly by `requests` + BeautifulSoup.
