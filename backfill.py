"""
backfill.py — Build a 90-day archive of press releases for each police service.

Stores results in data/archive/<slug>.json, one file per service.
Each file is a list of {title, url, date, service_name} dicts, newest first.
Existing entries are preserved; new ones are merged in (deduped by URL).

Pagination strategy:
- Follows rel="next" links or common "next page" anchors on each listing page.
- Stops when all items on a page are older than 90 days, or after MAX_PAGES pages.
- Special cases: OPP (API with skip), RCMP (single JSON blob — no pagination needed).
"""

import csv
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlencode, parse_qs, urlunparse

import requests
import urllib3
from bs4 import BeautifulSoup

from scan import (
    USER_AGENT,
    SOURCES_FILE,
    fetch_opp_items,
    fetch_rcmp_items,
    fetch_vpd_items,
    fetch_winnipeg_items,
    extract_links_by_selector,
    extract_links_title_from_heading,
    extract_links,
    load_sources,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Config ---

ARCHIVE_DATA_DIR = Path(__file__).parent / "data" / "archive"
CUTOFF_DAYS = 90
MAX_PAGES = 30          # safety cap per service
REQUEST_DELAY = 1.0     # seconds between page fetches

# --- Helpers ---


def slugify(name: str) -> str:
    """Convert a service name to a filename-safe slug."""
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def load_archive(slug: str) -> list[dict]:
    path = ARCHIVE_DATA_DIR / f"{slug}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return []


def save_archive(slug: str, items: list[dict]) -> None:
    ARCHIVE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DATA_DIR / f"{slug}.json"
    path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


def merge_items(existing: list[dict], new_items: list[dict], scraped_on: str | None = None) -> list[dict]:
    """Merge new_items into existing, deduping by URL. Newest first.
    New items that lack a scraped date are stamped with first_scraped=scraped_on."""
    seen_urls = {item["url"] for item in existing}
    merged = list(existing)
    for item in new_items:
        if item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            if scraped_on and not item.get("date"):
                item = {**item, "first_scraped": scraped_on}
            merged.append(item)
    # Sort newest first (items without dates go last)
    merged.sort(key=lambda x: x.get("date") or x.get("first_scraped") or "", reverse=True)
    return merged


def is_within_cutoff(date_str: str | None, cutoff: date) -> bool:
    """Return True if date_str is on or after cutoff. Handles multiple formats."""
    if not date_str:
        return True  # unknown date — keep it
    # Try ISO format first (YYYY-MM-DD or datetime)
    try:
        d = datetime.fromisoformat(date_str[:10]).date()
        return d >= cutoff
    except ValueError:
        pass
    # Try common human-readable formats (e.g. "Mar 12, 2026", "March 12, 2026")
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %B %Y", "%B %d %Y", "%b %d %Y"):
        try:
            d = datetime.strptime(date_str.strip()[:20], fmt).date()
            return d >= cutoff
        except ValueError:
            continue
    return True  # can't parse — keep it


def find_next_page_url(soup: BeautifulSoup, current_url: str) -> str | None:
    """Find the next-page URL from a listing page."""
    # 1. rel="next" in <head>
    link = soup.find("link", rel="next")
    if link and link.get("href"):
        return urljoin(current_url, link["href"])

    # 2. <a rel="next"> anywhere
    a = soup.find("a", rel="next")
    if a and a.get("href"):
        return urljoin(current_url, a["href"])

    # 3. Common "next" anchor text patterns
    for text_pat in [
        re.compile(r"^\s*(next|›|»|older|>)\s*$", re.IGNORECASE),
        re.compile(r"older\s+entries", re.IGNORECASE),
        re.compile(r"next\s+page", re.IGNORECASE),
    ]:
        a = soup.find("a", string=text_pat)
        if a and a.get("href"):
            href = a["href"]
            if href and not href.startswith("#"):
                return urljoin(current_url, href)

    # 4. .next or .pagination .next or .pager-next class on an anchor
    for sel in ["a.next", ".pagination a.next", ".pager-next a", ".nav-previous a", "li.next a"]:
        a = soup.select_one(sel)
        if a and a.get("href"):
            return urljoin(current_url, a["href"])

    return None


def fetch_page(url: str) -> tuple[BeautifulSoup | None, str | None]:
    """Fetch a URL and return (soup, error)."""
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": USER_AGENT},
            verify=False,
        )
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser"), None
    except Exception as e:
        return None, str(e)


def extract_items_from_page(
    soup: BeautifulSoup,
    base_url: str,
    link_selector: str,
    date_selector: str,
) -> list[dict]:
    """Extract items from a single page using the configured selector."""
    if link_selector.startswith("HEADING:"):
        item_selector = link_selector[len("HEADING:"):]
        return extract_links_title_from_heading(soup, base_url, item_selector, date_selector)
    elif link_selector:
        return extract_links_by_selector(soup, base_url, link_selector, date_selector)
    else:
        return extract_links(soup, base_url)


# --- OPP backfill (API supports skip/limit) ---

def backfill_opp(cutoff: date) -> list[dict]:
    """Fetch OPP items going back to cutoff using API pagination."""
    results = []
    skip = 0
    limit = 50
    while True:
        import json as _json
        payload = {
            "returnData": _json.dumps({
                "data.title": "1",
                "data.displaydate": "1",
                "data.category": "1",
            }),
            "findData": _json.dumps({"template.name": "General News"}),
            "limit": limit,
            "skip": skip,
        }
        try:
            resp = requests.post(
                "https://www.opp.ca/protonapi/entry/list/",
                json=payload,
                timeout=20,
                headers={"User-Agent": USER_AGENT},
                verify=False,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"    OPP API error at skip={skip}: {e}")
            break

        if not data:
            break

        page_items = []
        all_old = True
        for entry in data:
            entry_id = entry.get("id", "")
            d = entry.get("data", {})
            title = d.get("title", "").strip()
            date_str = d.get("displaydate", "")[:10] or None
            if not entry_id or not title:
                continue
            if is_within_cutoff(date_str, cutoff):
                all_old = False
                page_items.append({
                    "title": title,
                    "url": "https://www.opp.ca/news/viewnews/" + entry_id,
                    "date": date_str,
                })
        results.extend(page_items)

        if all_old or len(data) < limit:
            break
        skip += limit
        time.sleep(REQUEST_DELAY)

    return results


# --- RCMP backfill (all 1000 items already in page JSON) ---

def backfill_rcmp(cutoff: date) -> list[dict]:
    """All RCMP items are embedded in a single page — filter by cutoff."""
    all_items = fetch_rcmp_items()
    return [
        item for item in all_items
        if is_within_cutoff(item.get("date"), cutoff)
    ]


# --- VPD backfill (WordPress REST API, paginated) ---

def backfill_vpd(cutoff: date) -> list[dict]:
    """Fetch VPD news via WordPress REST API, paginating until cutoff."""
    results = []
    page = 1
    while True:
        resp = requests.get(
            "https://vpd.ca/wp-json/wp/v2/posts",
            params={"per_page": 100, "page": page, "_fields": "date,link,title"},
            timeout=20,
            headers={"User-Agent": USER_AGENT},
            verify=False,
        )
        if resp.status_code == 400:
            break  # past last page
        resp.raise_for_status()
        entries = resp.json()
        if not entries:
            break
        all_old = True
        for entry in entries:
            date_str = entry.get("date", "")[:10] or None
            if is_within_cutoff(date_str, cutoff):
                all_old = False
                results.append({
                    "title": entry.get("title", {}).get("rendered", "").strip(),
                    "url": entry.get("link", "").strip(),
                    "date": date_str,
                })
        if all_old:
            break
        page += 1
        time.sleep(REQUEST_DELAY)
    return results


# --- Winnipeg backfill (Drupal, paginated) ---

def backfill_winnipeg(cutoff: date) -> list[dict]:
    """Fetch Winnipeg Police news releases, paginating via ?page=N."""
    results = []
    page = 0
    seen_urls: set[str] = set()
    base_url = "https://www.winnipeg.ca/police/community/news-releases"
    while page < MAX_PAGES:
        url = f"{base_url}?page={page}" if page > 0 else base_url
        print(f"    Page {page + 1}: {url}")
        soup, error = fetch_page(url)
        if error:
            print(f"    Error: {error}")
            break
        page_items = []
        all_old = True
        for item in fetch_winnipeg_items(soup):
            if item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            if is_within_cutoff(item.get("date"), cutoff):
                all_old = False
                page_items.append(item)
        results.extend(page_items)
        if all_old and page > 0:
            print(f"    All items older than cutoff, stopping.")
            break
        next_url = find_next_page_url(soup, url)
        if not next_url or next_url == url:
            break
        page += 1
        time.sleep(REQUEST_DELAY)
    return results


# --- Generic paginated backfill ---

def backfill_site(
    service_name: str,
    start_url: str,
    link_selector: str,
    date_selector: str,
    cutoff: date,
) -> list[dict]:
    """Scrape a site across multiple pages until items are older than cutoff."""
    all_items = []
    current_url = start_url
    seen_urls: set[str] = set()
    pages_fetched = 0

    while current_url and pages_fetched < MAX_PAGES:
        print(f"    Page {pages_fetched + 1}: {current_url}")
        soup, error = fetch_page(current_url)
        if error:
            print(f"    Error: {error}")
            break

        raw = extract_items_from_page(soup, current_url, link_selector, date_selector)
        pages_fetched += 1

        page_new_items = []
        all_old = True
        for lnk in raw:
            url = lnk["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            if is_within_cutoff(lnk.get("date"), cutoff):
                all_old = False
                page_new_items.append({
                    "title": lnk["title"],
                    "url": url,
                    "date": lnk.get("date"),
                    "service_name": service_name,
                })

        all_items.extend(page_new_items)

        # Stop if all items on this page predate the cutoff
        if all_old and pages_fetched > 1:
            print(f"    All items older than cutoff, stopping.")
            break

        # Find next page
        next_url = find_next_page_url(soup, current_url)
        if not next_url or next_url == current_url:
            break
        current_url = next_url
        time.sleep(REQUEST_DELAY)

    return all_items


# --- Main ---

def main():
    cutoff = date.today() - timedelta(days=CUTOFF_DAYS)
    today_iso = date.today().isoformat()
    print(f"Backfill: collecting releases since {cutoff} ({CUTOFF_DAYS} days)")

    sources = load_sources()
    print(f"Sources: {len(sources)}")

    ARCHIVE_DATA_DIR.mkdir(parents=True, exist_ok=True)

    for source in sources:
        name = source["name"]
        url = source["url"]
        link_selector = source["link_selector"]
        date_selector = source["date_selector"]
        slug = slugify(name)

        print(f"\n[{name}]")

        # Load existing archive
        existing = load_archive(slug)
        print(f"  Existing: {len(existing)} items")

        # Fetch new items
        try:
            if "opp.ca" in url:
                new_items = backfill_opp(cutoff)
            elif "rcmp.ca" in url:
                new_items = backfill_rcmp(cutoff)
            elif "vpd.ca" in url:
                new_items = backfill_vpd(cutoff)
            elif "winnipeg.ca/police" in url:
                new_items = backfill_winnipeg(cutoff)
            else:
                new_items = backfill_site(name, url, link_selector, date_selector, cutoff)
        except Exception as e:
            print(f"  FAILED: {e}")
            continue

        # Add service_name to items that don't have it
        for item in new_items:
            item.setdefault("service_name", name)

        # Merge and save
        merged = merge_items(existing, new_items, scraped_on=today_iso)
        save_archive(slug, merged)
        added = len(merged) - len(existing)
        print(f"  Fetched: {len(new_items)}, Added: {added}, Total: {len(merged)}")

    print("\nDone.")


if __name__ == "__main__":
    main()
