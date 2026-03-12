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


def extract_links(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """
    Extract press release links from a parsed page.

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
            verify=False,
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
