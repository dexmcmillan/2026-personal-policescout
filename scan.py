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
                # Strip author prefixes like "By Brandon Police Service-Mar 12, 2026"
                if "-" in text:
                    text = text.split("-")[-1].strip()
                # Strip time/timezone noise like "12 March 2026 | 11:47 America/Denver"
                if "|" in text:
                    text = text.split("|")[0].strip()
                # Strip verbose prefix like "Posted on Friday, March 13, 2026 09:35 AM"
                import re as _re
                m = _re.search(r"(\w+ \d{1,2},\s*\d{4})", text)
                if m:
                    text = m.group(1)
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


OPP_API_URL = "https://www.opp.ca/protonapi/entry/list/"
OPP_NEWS_BASE = "https://www.opp.ca/news/viewnews/"

RCMP_NEWS_URL = "https://rcmp.ca/en/news"


def fetch_opp_items(limit: int = 200) -> list[dict]:
    """Fetch press releases from the OPP Proton API."""
    import json as _json

    payload = {
        "returnData": _json.dumps({
            "data.title": "1",
            "data.displaydate": "1",
            "data.category": "1",
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
        results.append({
            "title": title,
            "url": OPP_NEWS_BASE + entry_id,
            "date": date_str,
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
