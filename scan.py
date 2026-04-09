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
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Paths ---

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DOCS_DIR = BASE_DIR / "docs"
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
    """Read sources.csv and return list of source dicts."""
    sources = []
    with open(SOURCES_FILE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row["Name of police service"].strip()
            url = row["url"].strip()
            if name and url:
                sources.append({
                    "name": name,
                    "url": url,
                    "link_selector": row.get("link_selector", "").strip(),
                    "date_selector": row.get("date_selector", "").strip(),
                })
    return sources


PRESS_RELEASE_KEYWORDS = (
    "news",
    "release",
    "media",
    "press",
    "newsroom",
    "communique",
    "bulletin",
    "update",
    "notice",
    "alert",
)


def is_press_release_url(url: str) -> bool:
    """Return True if the URL path contains at least one press-release keyword."""
    path = urlparse(url).path.lower()
    return any(kw in path for kw in PRESS_RELEASE_KEYWORDS)


def normalize_date(date_str: str | None) -> str | None:
    """
    Convert a date string in any known format to ISO YYYY-MM-DD.
    Returns None if the input is None or cannot be parsed.
    Handles: "2026-03-13", "March 13, 2026", "Mar 13, 2026", "13 March 2026", etc.
    """
    if not date_str:
        return None
    # ISO format (also handles datetimes — take first 10 chars)
    try:
        return datetime.fromisoformat(date_str[:10]).date().isoformat()
    except ValueError:
        pass
    # Strip ordinal suffixes: "23rd" -> "23", "4th" -> "4", "1st" -> "1"
    import re as _re
    date_str = _re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", date_str.strip())
    # Human-readable formats
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %B %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(date_str.strip()[:20], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def extract_date_near(anchor: BeautifulSoup, date_selector: str) -> str | None:
    """
    Try to extract a date string near a link element.

    If date_selector is 'time', look for a <time> element in the ancestor chain.
    If date_selector starts with '.', look for that class in ancestor containers.
    Returns a stripped string or None.
    """
    if not date_selector:
        return None

    node = anchor.parent
    for _ in range(5):
        if node is None:
            break
        if date_selector == "time":
            t = node.find("time")
            if t:
                text = t.get("datetime", "").strip() or t.get_text(strip=True)
                if text:
                    return text[:40]
        else:
            el = node.select_one(date_selector)
            if el:
                text = el.get_text(separator=" ", strip=True)
                import re as _re
                # Try to extract "Month D, YYYY" first (e.g. "Posted: March 12, 2026 - 10:38 am")
                m = _re.search(r"(\w+ \d{1,2},\s*\d{4})", text)
                if m:
                    text = m.group(1)
                else:
                    # Strip author prefixes like "By Brandon Police Service-Mar 12, 2026"
                    if "-" in text:
                        text = text.split("-")[-1].strip()
                    # Strip time/timezone noise like "12 March 2026 | 11:47 America/Denver"
                    if "|" in text:
                        text = text.split("|")[0].strip()
                if text:
                    return text[:40]
        node = node.parent
    return None


def extract_links_by_selector(
    soup: BeautifulSoup,
    base_url: str,
    link_selector: str,
    date_selector: str,
) -> list[dict]:
    """Extract links using a specific CSS selector."""
    results = []
    seen_urls = set()
    for a in soup.select(link_selector):
        href = a.get("href", "")
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        title = a.get_text(strip=True)
        if not title:
            # Try aria-label for anchor-wrapping patterns (e.g. ppUnit)
            title = a.get("aria-label", "").strip()
        if not title:
            continue
        absolute_url = urljoin(base_url, href)
        if absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)
        date_str = extract_date_near(a, date_selector)
        results.append({"title": title, "url": absolute_url, "date": date_str})
    return results


def extract_links(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """
    Heuristic link extraction fallback.

    Step A: links inside <main>, <article>, <ul>, <ol>, <table>.
    Step B (fallback): all links on page, excluding <nav>, <footer>, <header>.

    Only links with href starting with http, https, or / and non-empty
    stripped text are included. Relative URLs are resolved to absolute.
    Links are further filtered to those whose URL path contains at least
    one press-release-style keyword (news, release, media, press, etc.).
    """

    def is_valid_href(href: str | None) -> bool:
        if not href:
            return False
        return href.startswith("http://") or href.startswith("https://") or href.startswith("/")

    def collect_from_tags(tags) -> list[dict]:
        results = []
        seen_urls = set()
        for a in tags:
            href = a.get("href", "")
            if not is_valid_href(href):
                continue
            title = a.get_text().strip()
            if not title:
                continue
            absolute_url = urljoin(base_url, href) if href.startswith("/") else href
            if absolute_url in seen_urls:
                continue
            seen_urls.add(absolute_url)
            results.append({"title": title, "url": absolute_url, "date": None})
        return results

    # Step A: preferred containers
    seen_ids = set()
    preferred_tags = []
    for container_name in ("main", "article", "ul", "ol", "table"):
        for container in soup.find_all(container_name):
            for a in container.find_all("a"):
                if id(a) not in seen_ids:
                    seen_ids.add(id(a))
                    preferred_tags.append(a)

    links = collect_from_tags(preferred_tags)

    if not links:
        # Step B: whole page minus nav/footer/header
        excluded = set()
        for tag_name in ("nav", "footer", "header"):
            for el in soup.find_all(tag_name):
                excluded.update(el.find_all("a"))

        all_anchors = [a for a in soup.find_all("a") if a not in excluded]
        links = collect_from_tags(all_anchors)

    return [lnk for lnk in links if is_press_release_url(lnk["url"])]


# Sites that use JS rendering and can't be scraped for content via static HTML.
# OPP content is fetched via API in fetch_opp_items() instead.
_JS_RENDERED_HOSTS = {
    "www.opp.ca",
    "www.edmontonpolice.ca",
}


def fetch_release_content(url: str) -> str | None:
    """
    Fetch an individual press release page and return its plain-text body.

    Tries to extract text from the most specific content container available
    (<article>, <main>, .content, .entry-content, etc.). Falls back to <body>.
    Returns None on network error, JS-rendered sites, or if no usable text is found.
    """
    host = urlparse(url).hostname or ""
    if host in _JS_RENDERED_HOSTS:
        return None
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": USER_AGENT},
            verify=False,
        )
        resp.raise_for_status()
    except Exception:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Remove noisy tags before extracting text
    for tag in soup(["script", "style", "nav", "header", "footer", "form", "noscript", "aside"]):
        tag.decompose()

    # Try progressively broader containers until we find something with real text
    selectors = [
        "article",
        "main",
        ".entry-content",
        ".post-content",
        ".article-body",
        ".content-body",
        ".news-body",
        ".field--name-body",
        ".field-name-body",
        "#content",
        ".content",
        "div[class*='content']",
        "body",
    ]
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator="\n", strip=True)
            # Strip very short results (nav remnants, etc.)
            if len(text) > 100:
                import re as _re
                # Collapse excessive blank lines
                text = _re.sub(r"\n{3,}", "\n\n", text)
                return text.strip()

    return None


def _fetch_opp_content_for_entry(entry_id: str) -> str | None:
    """
    Fetch content for a single OPP entry via the Proton API by entry ID.
    Returns plain text or None on failure.
    """
    import json as _json
    import re as _re

    payload = {
        "returnData": _json.dumps({"data.content": "1"}),
        "findData": _json.dumps({"id": entry_id}),
        "limit": 1,
        "skip": 0,
    }
    try:
        resp = requests.post(
            OPP_API_URL,
            json=payload,
            timeout=20,
            headers={"User-Agent": USER_AGENT},
            verify=False,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    if not data:
        return None
    raw_html = (data[0].get("data") or {}).get("content", "") or ""
    if not raw_html:
        return None
    text = BeautifulSoup(raw_html, "html.parser").get_text(separator="\n", strip=True)
    text = _re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() if len(text) > 50 else None


def backfill_content(archive_dir: Path) -> None:
    """
    For every item in the archive that has no 'content' field, fetch and store it.

    Processes all *.json files in archive_dir. Modifies files in-place.
    Skips items whose URL is None or empty.
    Adds a small delay between requests to be polite.
    OPP items are fetched via the Proton API per-entry rather than HTML scraping.
    """
    import time

    archive_files = sorted(archive_dir.glob("*.json"))
    total_fetched = 0
    total_skipped = 0

    for path in archive_files:
        try:
            items: list[dict] = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [backfill_content] WARNING: could not read {path.name}: {e}")
            continue

        missing = [i for i, item in enumerate(items) if not item.get("content") and item.get("url")]
        if not missing:
            continue

        print(f"  [backfill_content] {path.name}: fetching content for {len(missing)} item(s)...")
        modified = False
        for idx in missing:
            url = items[idx]["url"]
            if urlparse(url).hostname == "www.opp.ca":
                entry_id = url.rstrip("/").split("/")[-1]
                content = _fetch_opp_content_for_entry(entry_id)
            else:
                content = fetch_release_content(url)
            if content:
                items[idx]["content"] = content
                modified = True
                total_fetched += 1
            else:
                total_skipped += 1
            time.sleep(0.5)

        if modified:
            path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"  [backfill_content] Done: {total_fetched} fetched, {total_skipped} failed/empty")


OPP_API_URL = "https://www.opp.ca/protonapi/entry/list/"
OPP_NEWS_BASE = "https://www.opp.ca/news/viewnews/"

RCMP_NEWS_URL = "https://rcmp.ca/en/news"

VPD_API_URL = "https://vpd.ca/wp-json/wp/v2/posts"
WINNIPEG_NEWS_URL = "https://www.winnipeg.ca/police/community/news-releases"


def fetch_opp_items(limit: int = 200) -> list[dict]:
    """Fetch press releases from the OPP Proton API, including full body content."""
    import json as _json
    import re as _re

    payload = {
        "returnData": _json.dumps({
            "data.title": "1",
            "data.displaydate": "1",
            "data.category": "1",
            "data.content": "1",
        }),
        "findData": _json.dumps({"template.name": "General News"}),
        "limit": limit,
        "skip": 0,
    }
    resp = requests.post(
        OPP_API_URL,
        json=payload,
        timeout=20,
        headers={"User-Agent": USER_AGENT},
        verify=False,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for entry in data:
        entry_id = entry.get("id", "")
        d = entry.get("data", {})
        title = d.get("title", "").strip()
        date_str = d.get("displaydate", "")[:10] or None
        if not entry_id or not title:
            continue
        # Strip HTML tags from the content field
        raw_html = d.get("content", "") or ""
        content = None
        if raw_html:
            text = BeautifulSoup(raw_html, "html.parser").get_text(separator="\n", strip=True)
            text = _re.sub(r"\n{3,}", "\n\n", text)
            if len(text) > 50:
                content = text
        results.append({
            "title": title,
            "url": OPP_NEWS_BASE + entry_id,
            "date": date_str,
            "content": content,
        })
    return results


def fetch_rcmp_items() -> list[dict]:
    """
    Fetch RCMP news releases from the embedded Drupal JSON on the news page.

    The page embeds all news items as a JSON string in drupalSettings under
    poweb.all_news.rest_export_all_news. Items include title, URL, date, and type.
    """
    import json as _json
    import re as _re

    resp = requests.get(
        RCMP_NEWS_URL,
        timeout=20,
        headers={"User-Agent": USER_AGENT},
        verify=False,
    )
    resp.raise_for_status()
    m = _re.search(
        r'<script type="application/json" data-drupal-selector="drupal-settings-json">(.*?)</script>',
        resp.text,
        _re.DOTALL,
    )
    if not m:
        raise ValueError("Could not find Drupal settings JSON on RCMP news page")
    settings = _json.loads(m.group(1))
    raw = settings["poweb"]["all_news"]["rest_export_all_news"]
    entries = _json.loads(raw)
    results = []
    for entry in entries:
        title = entry.get("title", "").strip()
        url = entry.get("view_node", "").strip()
        date_str = entry.get("created", "")[:10] or None
        if not title or not url or url == "#":
            continue
        results.append({"title": title, "url": url, "date": date_str})
    return results


def fetch_vpd_items(per_page: int = 100) -> list[dict]:
    """Fetch Vancouver Police Department news via WordPress REST API."""
    resp = requests.get(
        VPD_API_URL,
        params={"per_page": per_page, "_fields": "date,link,title"},
        timeout=20,
        headers={"User-Agent": USER_AGENT},
        verify=False,
    )
    resp.raise_for_status()
    results = []
    for entry in resp.json():
        title = entry.get("title", {}).get("rendered", "").strip()
        url = entry.get("link", "").strip()
        date_str = entry.get("date", "")[:10] or None
        if not title or not url:
            continue
        results.append({"title": title, "url": url, "date": date_str})
    return results


def fetch_winnipeg_items(soup: BeautifulSoup | None = None) -> list[dict]:
    """Fetch Winnipeg Police Service news releases from listing page (or a pre-parsed soup)."""
    if soup is None:
        resp = requests.get(
            WINNIPEG_NEWS_URL,
            timeout=20,
            headers={"User-Agent": USER_AGENT},
            verify=False,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    seen_urls = set()
    for row in soup.select("div.views-row"):
        a = row.select_one("h3.field-content a")
        if not a:
            continue
        title = a.get_text(strip=True)
        href = a.get("href", "")
        if not title or not href:
            continue
        url = urljoin(WINNIPEG_NEWS_URL, href)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        # Date is in <time datetime="..."> inside .views-field-field-date-time
        date_str = None
        time_el = row.select_one(".views-field-field-date-time time")
        if time_el:
            date_str = normalize_date(time_el.get("datetime", "") or time_el.get_text(strip=True))
        results.append({"title": title, "url": url, "date": date_str})
    return results


def extract_links_title_from_heading(
    soup: BeautifulSoup,
    base_url: str,
    item_selector: str,
    date_selector: str = "",
) -> list[dict]:
    """
    Extract links from pages where each item is a container with a heading (title)
    and a separate 'Read more' anchor (href). Used for Amherst-style Joomla pages.

    For each element matching item_selector:
    - Title comes from the first <h2> or <h3> inside it
    - URL comes from the first <a href> inside it
    - Date comes from a <time datetime="..."> attribute if present
    """
    results = []
    seen_titles = set()
    for item in soup.select(item_selector):
        heading = item.find(["h2", "h3"])
        title = heading.get_text(strip=True) if heading else ""
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        a = item.find("a", href=True)
        href = a.get("href", "") if a else ""
        if href and not href.startswith("#") and not href.startswith("mailto:"):
            absolute_url = urljoin(base_url, href)
        else:
            # No usable link — fall back to the listing page itself
            absolute_url = base_url
        date_str = None
        time_el = item.find("time")
        if time_el:
            date_str = (time_el.get("datetime", "")[:10] or time_el.get_text(strip=True)[:40]) or None
        results.append({"title": title, "url": absolute_url, "date": date_str})
    return results


def scrape_site(
    service_name: str,
    url: str,
    link_selector: str = "",
    date_selector: str = "",
) -> tuple[list[dict], str | None]:
    """
    Scrape a police service listing page.

    Returns (items, error_message). On success, error_message is None.
    Each item is {title, url, date, service_name}.
    """
    try:
        # Special cases: sites that require custom fetching
        if "opp.ca" in url:
            raw_links = fetch_opp_items()
        elif "rcmp.ca" in url:
            raw_links = fetch_rcmp_items()
        elif "vpd.ca" in url:
            raw_links = fetch_vpd_items()
        elif "winnipeg.ca/police" in url:
            raw_links = fetch_winnipeg_items()
        else:
            resp = requests.get(
                url,
                timeout=15,
                headers={"User-Agent": USER_AGENT},
                verify=False,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            if link_selector.startswith("HEADING:"):
                item_selector = link_selector[len("HEADING:"):]
                raw_links = extract_links_title_from_heading(soup, url, item_selector, date_selector)
            elif link_selector:
                raw_links = extract_links_by_selector(soup, url, link_selector, date_selector)
            else:
                raw_links = extract_links(soup, url)

        items = [
            {
                "title": lnk["title"],
                "url": lnk["url"],
                "date": lnk.get("date"),
                "service_name": service_name,
            }
            for lnk in raw_links[:10]
        ]
        return items, None
    except Exception as e:
        return [], str(e)


# --- Archive persistence ---


def _service_name_to_filename(service_name: str) -> str:
    """Convert a service name to a slug suitable for use as a filename."""
    import re as _re
    slug = service_name.lower().strip()
    slug = _re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug + ".json"


def persist_to_archive(new_items: list[dict], archive_dir: Path) -> None:
    """
    Append newly scraped items to their per-service archive JSON files.

    Each file is named after the service (slugified) and contains a list of
    {title, url, date, service_name} dicts. Existing entries are preserved;
    new ones are prepended so the file stays newest-first.
    Deduplication is by URL.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Group new items by service
    by_service: dict[str, list[dict]] = {}
    for item in new_items:
        by_service.setdefault(item["service_name"], []).append(item)

    for service_name, items in by_service.items():
        filename = _service_name_to_filename(service_name)
        path = archive_dir / filename

        # Load existing entries
        existing: list[dict] = []
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"  [persist_to_archive] WARNING: could not read {filename}: {e}")

        existing_urls = {e.get("url") for e in existing}

        # Prepend new items (skip any already present by URL)
        to_add = []
        for i in items:
            if i["url"] in existing_urls:
                continue
            # Use pre-fetched content (e.g. from OPP API) if available, otherwise scrape
            content = i.get("content") or (fetch_release_content(i["url"]) if i.get("url") else None)
            to_add.append({
                "title": i["title"],
                "url": i["url"],
                "date": i.get("date"),
                "service_name": i["service_name"],
                "content": content,
            })

        if not to_add:
            continue

        merged = to_add + existing
        path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  [persist_to_archive] {filename}: added {len(to_add)} item(s) ({len(merged)} total)")


# --- Feed builder ---


def _load_archive_items(archive_dir: Path, cutoff: date, state: dict | None = None) -> list[dict]:
    """
    Load all press release items from archive/*.json files.
    Normalizes dates to ISO YYYY-MM-DD, filters to on/after cutoff.
    Malformed JSON files are skipped with a warning.
    When state is provided, items missing a scraped date fall back to
    their first-seen timestamp from the state dict.
    """
    items = []
    for path in archive_dir.glob("*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [build_feed] WARNING: skipping malformed archive {path.name}: {e}")
            continue
        for entry in raw:
            scraped_date = normalize_date(entry.get("date"))
            # Determine whether this item falls within the cutoff window.
            # Items with no scraped date are always included (we can't filter them out).
            if scraped_date is not None and scraped_date < cutoff.isoformat():
                continue
            # For display: fall back to first_scraped field, then state, when scraped date is missing.
            display_date = scraped_date
            if display_date is None:
                display_date = entry.get("first_scraped") or None
            if display_date is None and state is not None:
                h = item_hash(entry.get("title", ""), entry.get("url") or "")
                first_seen = state.get(h)
                if first_seen:
                    display_date = first_seen[:10]
            title = (entry.get("title") or "").lower()
            source = (entry.get("service_name") or "").lower()
            content = entry.get("content") or None
            items.append({
                "type": "press_release",
                "title": entry.get("title", ""),
                "url": entry.get("url"),
                "date": display_date,
                "source": entry.get("service_name", ""),
                "content": content,
                "search_text": " ".join(filter(None, [title, source, (content or "").lower()])),
                "_sort_key": display_date or "",
            })
    return items


def _load_tps_items(tps_ndjson: Path, cutoff: date) -> list[dict]:
    """
    Load TPS call records from the NDJSON log.
    Returns empty list with a warning if the file does not exist.
    """
    if not tps_ndjson.exists():
        print(f"  [build_feed] WARNING: TPS NDJSON not found at {tps_ndjson}, skipping TPS data")
        return []
    items = []
    with tps_ndjson.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception as e:
                print(f"  [build_feed] WARNING: skipping malformed TPS line {lineno}: {e}")
                continue
            occurred_at = rec.get("occurred_at")
            if not occurred_at:
                continue
            iso_date = occurred_at[:10]
            if iso_date < cutoff.isoformat():
                continue
            call_type = (rec.get("call_type") or "").lower()
            division = (rec.get("division") or "").lower()
            cross_streets = (rec.get("cross_streets") or "").lower()
            items.append({
                "type": "tps_call",
                "title": rec.get("call_type", ""),
                "call_type": rec.get("call_type", ""),
                "url": None,
                "date": iso_date,
                "occurred_at": occurred_at,
                "source": "Toronto Police Service",
                "division": rec.get("division", ""),
                "cross_streets": rec.get("cross_streets", ""),
                "search_text": " ".join(filter(None, [
                    call_type,
                    "toronto police service",
                    division,
                    cross_streets,
                ])),
                "_sort_key": occurred_at,
            })
    return items


def build_feed(
    archive_dir: Path,
    tps_ndjson: Path,
    output_dir: Path,
    days: int = 7,
) -> None:
    """
    Build the card feed: merge press releases + TPS calls, write docs/data.json
    and render docs/index.html from templates/feed.html.

    Errors (missing files, malformed JSON) are logged and skipped gracefully.
    """
    cutoff = date.today() - timedelta(days=days)
    print(f"  [build_feed] Cutoff: {cutoff} ({days} days)")

    state = load_state()
    press_items = _load_archive_items(archive_dir, cutoff, state=state)
    print(f"  [build_feed] Press releases in window: {len(press_items)}")

    tps_items = _load_tps_items(tps_ndjson, cutoff)
    print(f"  [build_feed] TPS calls in window: {len(tps_items)}")

    all_items = press_items + tps_items
    all_items.sort(key=lambda x: x["_sort_key"], reverse=True)

    for item in all_items:
        item.pop("_sort_key", None)

    output_dir.mkdir(parents=True, exist_ok=True)
    data_path = output_dir / "data.json"
    data_path.write_text(
        json.dumps(all_items, ensure_ascii=False, indent=None),
        encoding="utf-8",
    )
    print(f"  [build_feed] Wrote {data_path} ({len(all_items)} items)")

    generated_at = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")
    sources = sorted({item["source"] for item in press_items if item.get("source")})
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    try:
        template = env.get_template("feed.html")
    except Exception as e:
        print(f"  [build_feed] WARNING: could not load feed.html template: {e}")
        return
    html = template.render(generated_at=generated_at, sources=sources)
    index_path = output_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")
    print(f"  [build_feed] Wrote {index_path}")

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
        items, error = scrape_site(
            source["name"],
            source["url"],
            link_selector=source["link_selector"],
            date_selector=source["date_selector"],
        )
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
            {"title": item["title"], "url": item["url"], "date": item.get("date")}
        )
    services_list = [
        {"name": name, "items": items}
        for name, items in sorted(services_map.items())
    ]

    # Update state: prune first, then merge new hashes
    state = prune_state(state, today_utc)
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for item in all_new_items:
        h = item_hash(item["title"], item["url"])
        if h not in state:
            state[h] = now_ts
    save_state(state)
    print(f"State saved: {len(state)} items")

    # Persist new items to per-service archive files
    persist_to_archive(all_new_items, archive_dir=DATA_DIR / "archive")

    # Backfill content for any archive items that don't have it yet
    print("\nBackfilling content for archive items without content...")
    backfill_content(archive_dir=DATA_DIR / "archive")

    # Build the card feed
    build_feed(
        archive_dir=DATA_DIR / "archive",
        tps_ndjson=DATA_DIR / "tps_calls.ndjson",
        output_dir=DOCS_DIR,
        days=365,
    )
    print("Done.")


if __name__ == "__main__":
    main()
