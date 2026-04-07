"""
fetch_missing_dates.py — Backfill publication dates for archive items that have none.

For each dateless item in data/archive/*.json, fetches the article page and
tries to extract a publication date using (in order of preference):
  1. JSON-LD datePublished
  2. <meta property="article:published_time">
  3. <meta name="DC.date"> / <meta name="dcterms.created"> etc.
  4. <time datetime="..."> element
  5. Common visible date elements (.published, .hhblog-post-details, time, etc.)

Updates the archive files in-place. Skips items with no URL or that return
an HTTP error. Waits 1 second between requests to be polite.

Usage:
    uv run python fetch_missing_dates.py
"""

import json
import re
import time
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup

from scan import USER_AGENT, normalize_date

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ARCHIVE_DIR = Path(__file__).parent / "data" / "archive"
REQUEST_DELAY = 1.0  # seconds between fetches


def extract_date_from_page(html: str) -> str | None:
    """
    Try to extract a publication date from an article page.
    Returns ISO YYYY-MM-DD string or None.
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1. JSON-LD datePublished (handles single object, list, or @graph)
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            # Flatten: could be a single object, a list, or a @graph wrapper
            candidates = []
            if isinstance(data, list):
                candidates = data
            elif isinstance(data, dict):
                candidates = data.get("@graph", [data])
            for obj in candidates:
                date_str = obj.get("datePublished") or obj.get("dateCreated")
                if date_str:
                    result = normalize_date(str(date_str))
                    if result:
                        return result
        except Exception:
            continue

    # 2. <meta property="article:published_time"> or name variants
    meta_names = [
        ("property", "article:published_time"),
        ("name", "DC.date"),
        ("name", "dcterms.created"),
        ("name", "date"),
        ("name", "pubdate"),
        ("itemprop", "datePublished"),
    ]
    for attr, val in meta_names:
        tag = soup.find("meta", attrs={attr: re.compile(val, re.IGNORECASE)})
        if tag:
            content = tag.get("content", "")
            result = normalize_date(content)
            if result:
                return result

    # 3. <time datetime="...">
    for tag in soup.find_all("time"):
        dt = tag.get("datetime", "")
        result = normalize_date(dt) if dt else normalize_date(tag.get_text(strip=True))
        if result:
            return result

    # 4. Common visible date elements
    date_selectors = [
        ".published",
        ".hhblog-post-details",
        ".post-date",
        ".entry-date",
        ".article-date",
        ".date",
        ".post-meta",
        "[class*='date']",
        "[class*='Date']",
    ]
    for sel in date_selectors:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator=" ", strip=True)
            # Try to pull a date pattern out of the text
            m = re.search(r"(\w+ \d{1,2},\s*\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2} \w+ \d{4})", text)
            if m:
                result = normalize_date(m.group(1))
                if result:
                    return result

    return None


def fetch_date(url: str, session: requests.Session) -> str | None:
    """Fetch a URL and attempt to extract the publication date. Returns ISO date or None."""
    try:
        resp = session.get(url, timeout=15, verify=False)
        resp.raise_for_status()
        return extract_date_from_page(resp.text)
    except Exception as e:
        print(f"    ERROR fetching {url}: {e}")
        return None


def main():
    archive_files = sorted(ARCHIVE_DIR.glob("*.json"))
    print(f"Scanning {len(archive_files)} archive files for dateless items...")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    total_checked = 0
    total_found = 0
    total_failed = 0

    for archive_path in archive_files:
        items = json.loads(archive_path.read_text(encoding="utf-8"))
        dateless = [i for i in items if not i.get("date") and i.get("url")]
        if not dateless:
            continue

        print(f"\n{archive_path.stem}: {len(dateless)} dateless items")
        changed = False

        for item in items:
            if item.get("date") or not item.get("url"):
                continue

            total_checked += 1
            print(f"  [{total_checked}] {item['url'][:80]}", end=" ... ")
            time.sleep(REQUEST_DELAY)

            found = fetch_date(item["url"], session)
            if found:
                item["date"] = found
                changed = True
                total_found += 1
                print(found)
            else:
                total_failed += 1
                print("not found")

        if changed:
            archive_path.write_text(
                json.dumps(items, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"  -> Saved {archive_path.name}")

    print(f"\nDone. Checked: {total_checked}, Found: {total_found}, Not found: {total_failed}")


if __name__ == "__main__":
    main()
