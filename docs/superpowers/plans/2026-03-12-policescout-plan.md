# Police Scout Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a weekday-automated scraper that monitors 34 Canadian police service press release pages and publishes a plain HTML digest via GitHub Pages.

**Architecture:** A single Python script (`scan.py`) loads sources from CSV, scrapes each site with requests + BeautifulSoup using a heuristic link extractor, deduplicates against a JSON state file, renders a Jinja2 template, and writes static HTML. GitHub Actions runs this on a weekday cron and commits the output.

**Tech Stack:** Python 3.12, uv, requests, beautifulsoup4, jinja2, GitHub Actions, GitHub Pages

---

## File Map

| File | Responsibility |
|------|---------------|
| `scan.py` | Main script: load state, scrape sites, deduplicate, render HTML, update state |
| `templates/digest.html` | Jinja2 template for the digest page |
| `sources.csv` | List of 34 police services with names and URLs |
| `data/seen_items.json` | Deduplication state (created on first run) |
| `docs/index.html` | Generated digest (today's, overwritten each run) |
| `docs/archive/YYYY-MM-DD.html` | Generated archive copies |
| `.github/workflows/scan.yml` | GitHub Actions weekday cron workflow |
| `pyproject.toml` | Python project config and dependencies |
| `.python-version` | Pins Python 3.12 |
| `.gitignore` | Excludes `.venv`, `__pycache__` |
| `tests/test_scan.py` | Unit tests for heuristic extractor and state management |

---

## Chunk 1: Project scaffold, core logic, and tests

### Task 1: Initialize the project

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `.gitignore`
- Create: `sources.csv`

- [ ] **Step 1: Create `.python-version`**

```
3.12
```

File path: `.python-version`

- [ ] **Step 2: Initialize uv project**

```bash
cd /path/to/2026-personal-policescout
uv init --no-readme
```

This creates `pyproject.toml`. If it already exists (from git init), skip `uv init` and manually edit `pyproject.toml`.

- [ ] **Step 3: Set pyproject.toml content**

Replace the contents of `pyproject.toml` with:

```toml
[project]
name = "2026-personal-policescout"
version = "0.1.0"
description = "Automated daily police press release digest"
requires-python = ">=3.12"
dependencies = [
    "beautifulsoup4>=4.14.3",
    "jinja2>=3.1.6",
    "requests>=2.32.5",
]

[dependency-groups]
dev = ["pytest>=8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- [ ] **Step 4: Install dependencies**

```bash
uv sync
```

Expected: lock file created, `.venv` populated.

- [ ] **Step 5: Create `.gitignore`**

```
__pycache__/
*.py[oc]
.venv/
```

- [ ] **Step 6: Create `sources.csv`**

Populate with the 34 police services from the verified list. Use the exact headers `Name of police service,url`. Content:

```csv
Name of police service,url
Abbotsford Police Department,https://www.abbypd.ca/blog/news_releases
Akwesasne Mohawk Police Service,https://akwesasnepolice.ca/news-and-updates/
Altona Police Service,https://altona.ca/m/altona-police-service/local-notices
Amherst Police Department,https://www.amherst.ca/town-news/media-releases/
Amherstburg Police Service,https://windsorpolice.ca/newsroom/
Anishinabek Police Service,https://www.anishinabekpolice.ca/
Annapolis Royal Police Department,https://annapolisroyal.com/police/
Aylmer Police,https://www.aylmerpolice.com/events
Barrie Police Service,https://www.barriepolice.ca/newsroom/
Bathurst Police Force,https://www.bathurst.ca/en/services/1/bathurst-police-force
Belleville Police Service,https://www.bellevilleps.ca/news-stories/
Blood Tribe Police Service,https://www.bloodtribepolice.com/
Brandon Police Service,https://www.brandon.ca/news/police-media-releases/
Brantford Police Service,https://www.brantfordpolice.ca/news-and-media-releases/categories/media-releases/
Bridgewater Police Department,https://www.bridgewaterpolice.ca/news-room
Brockville Police,https://brockvillepolice.com/news/
Calgary Police Service,https://newsroom.calgary.ca/police-news-releases/
Cape Breton Regional Police,https://www.cbrps.ca/media-releases/
Chatham-Kent Police Service,https://ckpolice.com/daily-news-release/
Cobourg Police Service,https://cobourgpoliceservice.com/category/news/
Cornwall Community Police Service,https://cornwallpolice.ca/news
Delta Police Department,https://www.deltapolice.ca/media/releases
Durham Regional Police Service,https://www.drps.ca/news/media-releases/
Edmonton Police Service,https://www.edmontonpolice.ca/News/MediaReleases
Fredericton Police Force,https://www.fredericton.ca/your-government/news
Greater Sudbury Police Service,https://www.gsps.ca/Modules/News/en
Guelph Police Service,https://www.guelphpolice.ca/news/media-releases/
Halifax Regional Police,https://www.halifax.ca/home/news
Halton Regional Police Service,https://www.haltonpolice.ca/news-releases/
Hamilton Police Service,https://hamiltonpolice.on.ca/news/
Nishnawbe-Aski Police Service,https://www.naps.ca/news/
Ontario Provincial Police,https://www.opp.ca/news/
Ottawa Police Service,https://www.ottawapolice.ca/modules/news/en
RCMP,https://rcmp.ca/en/news
```

- [ ] **Step 7: Create required directories**

```bash
mkdir -p data docs/archive templates tests
```

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock .python-version .gitignore sources.csv
git commit -m "chore: scaffold project with dependencies and sources"
```

---

### Task 2: Write tests for core utilities

**Files:**
- Create: `tests/__init__.py` (empty)
- Create: `tests/test_scan.py`

These tests are written first (TDD). They will fail until Task 3 implements the code.

- [ ] **Step 1: Create `tests/__init__.py`**

Empty file.

- [ ] **Step 2: Write `tests/test_scan.py`**

```python
"""Tests for scan.py core utilities."""
import json
import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

# We import from scan at test time; scan.py must exist but functions tested here
# are pure/unit-testable without network access.
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import scan


# --- item_hash ---

def test_item_hash_stable():
    h1 = scan.item_hash("Title", "https://example.com/release")
    h2 = scan.item_hash("Title", "https://example.com/release")
    assert h1 == h2

def test_item_hash_strips_whitespace():
    h1 = scan.item_hash("Title", "https://example.com/release")
    h2 = scan.item_hash("  Title  ", "  https://example.com/release  ")
    assert h1 == h2

def test_item_hash_different_inputs_produce_different_hashes():
    # Distinct title/url pairs should produce distinct hashes
    h1 = scan.item_hash("Press Release One", "https://example.com/release/1")
    h2 = scan.item_hash("Press Release Two", "https://example.com/release/2")
    assert h1 != h2

def test_item_hash_is_md5_hex():
    h = scan.item_hash("T", "U")
    assert len(h) == 32
    assert all(c in "0123456789abcdef" for c in h)


# --- prune_state ---

def test_prune_state_removes_old_entries():
    today = date(2026, 3, 12)
    old_ts = "2026-01-01T00:00:00Z"  # >30 days ago
    recent_ts = "2026-03-11T00:00:00Z"  # 1 day ago
    state = {"aaa": old_ts, "bbb": recent_ts}
    result = scan.prune_state(state, today)
    assert "aaa" not in result
    assert "bbb" in result

def test_prune_state_keeps_exactly_30_day_boundary():
    today = date(2026, 3, 12)
    exactly_30 = "2026-02-10T00:00:00Z"  # exactly 30 days ago
    result = scan.prune_state({"x": exactly_30}, today)
    # 30 days before 2026-03-12 is 2026-02-10; entries older than 30 days are pruned.
    # "older than" means strictly before the cutoff date, so exactly 30 days is kept.
    assert "x" in result

def test_prune_state_removes_31_days_ago():
    today = date(2026, 3, 12)
    ts_31 = "2026-02-09T00:00:00Z"  # 31 days ago
    result = scan.prune_state({"x": ts_31}, today)
    assert "x" not in result

def test_prune_state_empty_input():
    result = scan.prune_state({}, date(2026, 3, 12))
    assert result == {}


# --- extract_links ---

def test_extract_links_prefers_main_content():
    html = """
    <html>
    <nav><a href="/nav-link">Nav</a></nav>
    <main>
      <ul>
        <li><a href="https://example.com/release/1">Press Release 1</a></li>
        <li><a href="https://example.com/release/2">Press Release 2</a></li>
      </ul>
    </main>
    </html>
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    links = scan.extract_links(soup, "https://example.com/news/")
    urls = [l["url"] for l in links]
    assert "https://example.com/release/1" in urls
    assert "https://example.com/release/2" in urls
    # Nav link should not appear if main content has links
    assert "/nav-link" not in urls and "https://example.com/nav-link" not in urls

def test_extract_links_resolves_relative_urls():
    html = """
    <main>
      <ul><li><a href="/news/release-1">Release 1</a></li></ul>
    </main>
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    links = scan.extract_links(soup, "https://police.ca/newsroom/")
    assert links[0]["url"] == "https://police.ca/news/release-1"

def test_extract_links_excludes_invalid_hrefs():
    html = """
    <main>
      <ul>
        <li><a href="#">Anchor</a></li>
        <li><a href="mailto:info@police.ca">Email</a></li>
        <li><a href="tel:+15551234567">Phone</a></li>
        <li><a href="javascript:void(0)">JS</a></li>
        <li><a href="https://example.com/valid">Valid</a></li>
      </ul>
    </main>
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    links = scan.extract_links(soup, "https://example.com/")
    assert len(links) == 1
    assert links[0]["url"] == "https://example.com/valid"

def test_extract_links_excludes_empty_text():
    html = """
    <main>
      <ul>
        <li><a href="https://example.com/release-1">  </a></li>
        <li><a href="https://example.com/release-2">Real Title</a></li>
      </ul>
    </main>
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    links = scan.extract_links(soup, "https://example.com/")
    assert len(links) == 1
    assert links[0]["title"] == "Real Title"

def test_extract_links_fallback_when_no_main_content():
    html = """
    <html>
    <nav><a href="/nav">Nav</a></nav>
    <header><a href="/head">Head</a></header>
    <footer><a href="/foot">Footer</a></footer>
    <div><a href="https://example.com/release">Release</a></div>
    </html>
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    links = scan.extract_links(soup, "https://example.com/")
    urls = [l["url"] for l in links]
    assert "https://example.com/release" in urls
    # nav/header/footer links excluded in fallback
    for u in urls:
        assert u not in ("https://example.com/nav", "https://example.com/head", "https://example.com/foot")

def test_extract_links_uses_stripped_title():
    html = """
    <main><ul>
      <li><a href="https://example.com/1">  Spaced Title  </a></li>
    </ul></main>
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    links = scan.extract_links(soup, "https://example.com/")
    assert links[0]["title"] == "Spaced Title"


# --- get_archive_links ---

def test_get_archive_links_returns_30_most_recent(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    # Create 35 fake archive files
    for i in range(35):
        d = date(2026, 1, 1) + timedelta(days=i)
        (archive_dir / f"{d.isoformat()}.html").write_text("")
    links = scan.get_archive_links(archive_dir)
    assert len(links) == 30
    # Most recent first
    assert links[0]["date"] == "2026-02-04"  # day 34
    assert links[-1]["date"] == "2026-01-06"  # day 5

def test_get_archive_links_filename_is_relative_path(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "2026-03-12.html").write_text("")
    links = scan.get_archive_links(archive_dir)
    assert links[0]["filename"] == "archive/2026-03-12.html"

def test_get_archive_links_empty_dir(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    links = scan.get_archive_links(archive_dir)
    assert links == []

def test_get_archive_links_missing_dir(tmp_path):
    archive_dir = tmp_path / "archive"  # does not exist
    links = scan.get_archive_links(archive_dir)
    assert links == []
```

- [ ] **Step 3: Run tests — verify they all fail (scan.py doesn't exist yet)**

```bash
uv run pytest tests/test_scan.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError` or similar — scan.py not found. That's correct.

- [ ] **Step 4: Commit the tests**

```bash
git add tests/
git commit -m "test: add unit tests for item_hash, prune_state, extract_links, get_archive_links"
```

---

### Task 3: Implement `scan.py`

**Files:**
- Create: `scan.py`

- [ ] **Step 1: Write `scan.py`**

```python
"""
Police Scout — Daily police press release digest generator.
Scrapes press release listing pages for 34 Canadian police services,
deduplicates, and publishes a static HTML digest via GitHub Pages.
"""

import csv
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

# --- Paths ---

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DOCS_DIR = BASE_DIR / "docs"
ARCHIVE_DIR = DOCS_DIR / "archive"
TEMPLATE_DIR = BASE_DIR / "templates"
STATE_FILE = DATA_DIR / "seen_items.json"
SOURCES_FILE = BASE_DIR / "sources.csv"

USER_AGENT = (
    "Mozilla/5.0 (compatible; PoliceScout/1.0; +https://github.com)"
)

# --- Core utilities ---


def item_hash(title: str, url: str) -> str:
    """Return MD5 hex digest of 'title|url' (both stripped)."""
    raw = title.strip() + "|" + url.strip()
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def load_state() -> dict:
    """Load seen-items state from JSON. Return empty dict if missing."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    """Write state to JSON unconditionally."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def prune_state(state: dict, today: date) -> dict:
    """Remove entries older than 30 calendar days before today."""
    cutoff = today - timedelta(days=30)
    cutoff_str = cutoff.isoformat()
    return {k: v for k, v in state.items() if v[:10] >= cutoff_str}


def load_sources() -> list[dict]:
    """Read sources.csv and return list of {name, url} dicts."""
    sources = []
    with open(SOURCES_FILE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row["Name of police service"].strip()
            url = row["url"].strip()
            if name and url:
                sources.append({"name": name, "url": url})
    return sources


def extract_links(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """
    Extract press release links from a parsed page.

    Step A: links inside <main>, <article>, <ul>, <ol>, <table>.
    Step B (fallback): all links on page, excluding <nav>, <footer>, <header>.

    Only links with href starting with http, https, or / and non-empty
    stripped text are included. Relative URLs are resolved to absolute.
    """

    def is_valid_href(href: str | None) -> bool:
        if not href:
            return False
        return href.startswith("http://") or href.startswith("https://") or href.startswith("/")

    def collect_from_tags(tags) -> list[dict]:
        results = []
        for a in tags:
            href = a.get("href", "")
            if not is_valid_href(href):
                continue
            title = a.get_text().strip()
            if not title:
                continue
            absolute_url = urljoin(base_url, href) if href.startswith("/") else href
            results.append({"title": title, "url": absolute_url})
        return results

    # Step A: preferred containers
    preferred_tags = []
    for container_name in ("main", "article", "ul", "ol", "table"):
        for container in soup.find_all(container_name):
            preferred_tags.extend(container.find_all("a"))

    links = collect_from_tags(preferred_tags)

    if links:
        return links

    # Step B: whole page minus nav/footer/header
    excluded = set()
    for tag_name in ("nav", "footer", "header"):
        for el in soup.find_all(tag_name):
            excluded.update(el.find_all("a"))

    all_anchors = [a for a in soup.find_all("a") if a not in excluded]
    return collect_from_tags(all_anchors)


def scrape_site(service_name: str, url: str) -> tuple[list[dict], str | None]:
    """
    Scrape a police service listing page.

    Returns (items, error_message). On success, error_message is None.
    Each item is {title, url, service_name}.
    """
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        raw_links = extract_links(soup, url)
        items = [
            {"title": lnk["title"], "url": lnk["url"], "service_name": service_name}
            for lnk in raw_links
        ]
        return items, None
    except Exception as e:
        return [], str(e)


def get_archive_links(archive_dir: Path) -> list[dict]:
    """
    Return up to 30 most recent archive files as [{date, filename}].
    date is the ISO filename stem; filename is relative from docs/.
    """
    if not archive_dir.exists():
        return []
    files = sorted(archive_dir.glob("*.html"), key=lambda f: f.stem, reverse=True)
    return [
        {"date": f.stem, "filename": f"archive/{f.name}"}
        for f in files[:30]
    ]


def render_digest(
    today_utc: date,
    services: list[dict],
    failed: list[str],
    archive_links: list[dict],
) -> str:
    """Render the Jinja2 digest template and return HTML string."""
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    template = env.get_template("digest.html")
    date_str = today_utc.strftime("%A, %B %-d, %Y")
    return template.render(
        date_str=date_str,
        services=services,
        failed=failed,
        archive_links=archive_links,
    )


# --- Main ---


def main():
    today_utc = datetime.now(timezone.utc).date()
    print(f"Police Scout — {today_utc}")

    # Load state
    state = load_state()
    print(f"Loaded state: {len(state)} seen items")

    # Load sources
    sources = load_sources()
    print(f"Loaded {len(sources)} sources")

    # Scrape
    all_new_items = []
    failed_services = []

    for source in sources:
        print(f"  Scraping {source['name']}...", end=" ")
        items, error = scrape_site(source["name"], source["url"])
        if error:
            print(f"FAILED: {error}")
            failed_services.append(source["name"])
            continue
        # Deduplicate against state
        new_items = [
            item for item in items
            if item_hash(item["title"], item["url"]) not in state
        ]
        print(f"{len(items)} links found, {len(new_items)} new")
        all_new_items.extend(new_items)

    print(f"\nTotal new items: {len(all_new_items)}")
    print(f"Failed services: {len(failed_services)}")

    # Group by service, alphabetical
    services_map: dict[str, list] = {}
    for item in all_new_items:
        services_map.setdefault(item["service_name"], []).append(
            {"title": item["title"], "url": item["url"]}
        )
    services_list = [
        {"name": name, "items": items}
        for name, items in sorted(services_map.items())
    ]

    # Ensure archive dir exists
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    # Touch today's archive file before calling get_archive_links so it appears
    # as the first entry in the rendered footer (spec requirement).
    archive_path = ARCHIVE_DIR / f"{today_utc}.html"
    if not archive_path.exists():
        archive_path.write_text("", encoding="utf-8")

    archive_links = get_archive_links(ARCHIVE_DIR)
    html = render_digest(today_utc, services_list, failed_services, archive_links)

    index_path = DOCS_DIR / "index.html"
    index_path.write_text(html, encoding="utf-8")
    archive_path.write_text(html, encoding="utf-8")
    print(f"Wrote docs/index.html and docs/archive/{today_utc}.html")

    # Update state: prune first, then merge new hashes
    state = prune_state(state, today_utc)
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for item in all_new_items:
        h = item_hash(item["title"], item["url"])
        if h not in state:
            state[h] = now_ts
    save_state(state)
    print(f"State saved: {len(state)} items")
    print("Done.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the unit tests — they should now pass**

```bash
uv run pytest tests/test_scan.py -v
```

Expected: all tests pass. If any fail, fix `scan.py` until they do before continuing.

- [ ] **Step 3: Commit**

```bash
git add scan.py
git commit -m "feat: implement scan.py with scraper, dedup, and state management"
```

---

## Chunk 2: HTML template and GitHub Actions workflow

### Task 4: Create the Jinja2 digest template

**Files:**
- Create: `templates/digest.html`

- [ ] **Step 1: Write `templates/digest.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Police Press Release Digest — {{ date_str }}</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body {
      font-family: system-ui, -apple-system, sans-serif;
      line-height: 1.6;
      color: #222;
      max-width: 800px;
      margin: 0 auto;
      padding: 1.5rem 1rem;
    }
    h1 {
      font-size: 1.4rem;
      font-weight: 700;
      margin: 0 0 0.25rem;
      border-bottom: 2px solid #222;
      padding-bottom: 0.4rem;
    }
    .date {
      font-size: 0.9rem;
      color: #555;
      margin-bottom: 2rem;
    }
    h2 {
      font-size: 1rem;
      font-weight: 600;
      margin: 1.5rem 0 0.4rem;
      color: #333;
    }
    ul {
      margin: 0 0 0.5rem;
      padding-left: 1.2rem;
    }
    li {
      margin-bottom: 0.2rem;
    }
    a {
      color: #0060a0;
      text-decoration: none;
    }
    a:hover {
      text-decoration: underline;
    }
    .empty-state {
      color: #555;
      margin: 2rem 0;
    }
    .failed-section {
      margin-top: 2.5rem;
      padding-top: 1rem;
      border-top: 1px solid #ddd;
    }
    .failed-section h2 {
      color: #777;
      font-size: 0.9rem;
      font-weight: 600;
    }
    .failed-section ul {
      color: #777;
      font-size: 0.9rem;
    }
    footer {
      margin-top: 2.5rem;
      padding-top: 1rem;
      border-top: 1px solid #ddd;
      font-size: 0.85rem;
      color: #555;
    }
    footer a {
      color: #555;
    }
    .archive-links {
      display: flex;
      flex-wrap: wrap;
      gap: 0.25rem 0.5rem;
      margin-top: 0.25rem;
    }
  </style>
</head>
<body>

  <h1>Police Press Release Digest</h1>
  <p class="date">{{ date_str }}</p>

  {% if services %}
    {% for service in services %}
      <h2>{{ service.name }}</h2>
      <ul>
        {% for item in service.items %}
          <li><a href="{{ item.url }}" target="_blank" rel="noopener">{{ item.title }}</a></li>
        {% endfor %}
      </ul>
    {% endfor %}
  {% else %}
    <p class="empty-state">No new press releases found today.</p>
  {% endif %}

  {% if failed %}
    <div class="failed-section">
      <h2>Sites that could not be scraped</h2>
      <ul>
        {% for name in failed %}
          <li>{{ name }}</li>
        {% endfor %}
      </ul>
    </div>
  {% endif %}

  <footer>
    <div>Archive:</div>
    <div class="archive-links">
      {% for link in archive_links %}
        <a href="{{ link.filename }}">{{ link.date }}</a>
      {% endfor %}
    </div>
  </footer>

</body>
</html>
```

- [ ] **Step 2: Do a local smoke test — run scan.py once and inspect output**

```bash
uv run python scan.py 2>&1 | head -60
```

Expected: output shows scraping attempts for each service, completes without Python exceptions, creates `docs/index.html`.

Check the generated file exists:

```bash
ls -lh docs/index.html docs/archive/
```

Briefly open `docs/index.html` in a text editor or browser to verify it renders correctly — it will contain today's results (which may be large on first run).

- [ ] **Step 3: Commit**

```bash
git add templates/digest.html docs/ data/
git commit -m "feat: add digest Jinja2 template and initial generated output"
```

---

### Task 5: Create GitHub Actions workflow

**Files:**
- Create: `.github/workflows/scan.yml`

- [ ] **Step 1: Create `.github/workflows/` directory**

```bash
mkdir -p .github/workflows
```

- [ ] **Step 2: Write `.github/workflows/scan.yml`**

```yaml
name: Police Scout

on:
  schedule:
    # 7 AM ET (noon UTC) on weekdays
    - cron: '0 12 * * 1-5'
  workflow_dispatch:  # Allow manual trigger

jobs:
  scan:
    runs-on: ubuntu-latest
    permissions:
      contents: write  # Needed to commit docs/ and data/

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Set up Python
        run: uv python install

      - name: Install dependencies
        run: uv sync

      - name: Run Police Scout
        run: uv run python scan.py

      - name: Commit and push changes
        run: |
          git config --local user.email "github-actions[bot]@users.noreply.github.com"
          git config --local user.name "github-actions[bot]"
          git add docs/ data/seen_items.json
          git diff --staged --quiet || git commit -m "Update Police Scout digest for $(date -u +%Y-%m-%d)"
          git push
```

- [ ] **Step 3: Commit**

```bash
git add .github/
git commit -m "feat: add GitHub Actions weekday cron workflow"
```

---

### Task 6: Final verification

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 2: Verify project structure is complete**

```bash
find . -not -path './.git/*' -not -path './.venv/*' -not -path './__pycache__/*' | sort
```

Expected output includes:
```
./.github/workflows/scan.yml
./.gitignore
./.python-version
./data/seen_items.json
./docs/archive/<today>.html
./docs/index.html
./pyproject.toml
./scan.py
./sources.csv
./templates/digest.html
./tests/__init__.py
./tests/test_scan.py
./uv.lock
```

- [ ] **Step 3: Verify docs/index.html is valid HTML**

```bash
python3 -c "
from pathlib import Path
from html.parser import HTMLParser
class V(HTMLParser): pass
V().feed(Path('docs/index.html').read_text())
print('HTML parses without errors')
"
```

Expected: `HTML parses without errors`

- [ ] **Step 4: Final commit**

If any files were modified during verification:

```bash
git add -A
git status
```

Only commit if there are actual changes.

- [ ] **Step 5: Set up GitHub repo and enable GitHub Pages**

These steps must be done by the user (require GitHub account actions):

1. Create a new GitHub repository named `2026-personal-policescout`
2. Push the local repo:
   ```bash
   git remote add origin https://github.com/<your-username>/2026-personal-policescout.git
   git push -u origin main
   ```
3. In the GitHub repo settings → Pages → set source to `Deploy from a branch`, branch `main`, folder `/docs`
4. Trigger a manual workflow run via Actions → Police Scout → Run workflow to verify end-to-end
