"""
Police Scout — Daily police press release digest generator.
Scrapes press release listing pages for Canadian police services,
deduplicates via PostgreSQL, and publishes a static HTML digest.
"""

import csv
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Paths ---

BASE_DIR = Path(__file__).parent
DOCS_DIR = BASE_DIR / "docs"
TEMPLATE_DIR = BASE_DIR / "templates"
SOURCES_FILE = BASE_DIR / "sources.csv"
DATA_DIR = BASE_DIR / "data"
ARCHIVE_DIR = DATA_DIR / "archive"
TPS_NDJSON = DATA_DIR / "tps_calls.ndjson"
SEEN_ITEMS_FILE = DATA_DIR / "seen_items.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Cache for CK daily stats extracted during fetch, keyed by release URL.
# Stored separately so clean_content() cannot accidentally strip them.
_ck_stats_cache: dict[str, str] = {}

# --- Core utilities ---


def item_hash(title: str, url: str) -> str:
    """Return MD5 hex digest of 'title|url' (both stripped)."""
    raw = title.strip() + "|" + url.strip()
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def prune_state(state: dict, today: date) -> dict:
    """Remove entries older than 30 days from a {key: ISO-timestamp} dict."""
    cutoff = (today - timedelta(days=30)).isoformat()
    return {k: v for k, v in state.items() if v[:10] >= cutoff}


def _slugify(s: str) -> str:
    """Convert a string to a URL-safe slug (lowercase, hyphens only)."""
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def load_sources() -> list[dict]:
    """Read sources.csv and return list of source dicts."""
    sources = []
    with open(SOURCES_FILE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row["Name of police service"].strip()
            url = row["url"].strip()
            status = row.get("status", "active").strip() or "active"
            if name and url and status == "active":
                sources.append({
                    "name": name,
                    "url": url,
                    "link_selector": row.get("link_selector", "").strip(),
                    "date_selector": row.get("date_selector", "").strip(),
                    "province": row.get("province", "").strip(),
                })
    return sources


_PROVINCE_ORDER = [
    "National", "British Columbia", "Alberta", "Saskatchewan", "Manitoba",
    "Ontario", "Quebec", "New Brunswick", "Nova Scotia", "Prince Edward Island",
    "Newfoundland and Labrador",
]

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
    from urllib.parse import urlparse
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
    # Strip leading day-of-week: "Thursday June 11 2026, 8am" -> "June 11 2026, 8am"
    date_str = _re.sub(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+", "", date_str)
    # Strip trailing time: "June 11 2026, 8am" -> "June 11 2026"
    date_str = _re.sub(r",?\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM)$", "", date_str.strip())
    # Human-readable formats — try original case and title-cased (handles "05 march 2026")
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %B %Y", "%B %d %Y", "%b %d %Y", "%b-%d-%Y", "%b-%m-%Y"):
        for candidate in (date_str.strip()[:20], date_str.strip()[:20].title()):
            try:
                return datetime.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                continue
    # DD.MM.YY format (e.g. "09.06.26" from MHPS)
    m = _re.match(r"^(\d{2})\.(\d{2})\.(\d{2})$", date_str.strip())
    if m:
        try:
            return datetime.strptime(date_str.strip(), "%d.%m.%y").date().isoformat()
        except ValueError:
            pass
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
    from urllib.parse import urljoin
    results = []
    seen_urls = set()
    for a in soup.select(link_selector):
        href = a.get("href", "")
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        # PressPoint CMS: prefer .pp_newsreel_title child over full anchor text
        # (full text concatenates date/time/timezone before the real title)
        pp_title = a.select_one(".pp_newsreel_title")
        if pp_title:
            title = pp_title.get_text(strip=True)
        else:
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
    from urllib.parse import urljoin

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
_JS_RENDERED_HOSTS = {
    "www.opp.ca",
    "saintjohnpolice.ca",
}


def _fetch_edmonton_content(url: str) -> str | None:
    """
    Fetch content for an Edmonton Police Service release page.
    EPS uses a flat XHTML layout with no <main>/<article>; the release body
    lives in div.leftColumn. Strip the breadcrumb header line before returning.
    """
    import re as _re
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
    el = soup.select_one("div.leftColumn")
    if not el:
        return None
    # Remove sidebar nav that sometimes bleeds in
    for tag in el.select("div.noindex, nav, ul.menu"):
        tag.decompose()
    text = el.get_text(separator="\n", strip=True)
    # Strip leading breadcrumb line (e.g. "Edmonton Police Service>Newsroom>Media Releases>Title")
    text = _re.sub(r"^[^\n]*>[^\n]*\n", "", text)
    text = _re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() if len(text) > 50 else None


def split_ck_daily_release(item: dict) -> list[dict]:
    """
    Split a Chatham-Kent Police daily omnibus release into per-incident items.

    Daily releases contain multiple incidents separated by headers like
    "Theft – CK26024691" or (split across lines) "Warrants/IPV, Arrest\n–\nCK26024398".
    Each incident block is extracted as a separate item with a title like
    "Theft (CK26024691)" and the date extracted from the block's "Date:" line.
    If no incident headers are found, returns [item] unchanged.
    """
    import re as _re

    content = item.get("content") or ""
    url = item.get("url", "")
    date = item.get("date")
    service_name = item.get("service_name", "Chatham-Kent Police Service")

    # Normalize split-line em-dash pattern: "Type\n–\nCKxxxx" -> "Type – CKxxxx"
    normalized = _re.sub(r"\n[–\-]\n(CK\d+)", r" – \1", content)
    # Also handle "Type –\nCKxxxx"
    normalized = _re.sub(r"[–\-]\n(CK\d+)", r"– \1", normalized)

    # Incident header — two formats seen in the wild:
    #   Old: "Incident Type – CKxxxxxxxx"   (groups 1, 2)
    #   New: "CKxxxxxxxx – Incident Type"   (groups 3, 4)
    pattern = _re.compile(
        r"^(?:"
        r"(.{3,80}?)\s*[–\-]\s*(CK\d+(?:/CK\d+)*)"   # old: type – CK
        r"|"
        r"(CK\d+(?:/CK\d+)*)\s*[–\-]\s*(.{3,80})"    # new: CK – type
        r")",
        _re.MULTILINE,
    )
    matches = list(pattern.finditer(normalized))

    if not matches:
        return [item]

    # Retrieve the daily stats line cached by fetch_release_content() for this URL.
    # clean_content() strips it from the body text, so we preserve it here separately.
    stats_line = _ck_stats_cache.get(url)

    results = []
    for i, m in enumerate(matches):
        # Support both old (type–CK) and new (CK–type) formats
        incident_type = (m.group(1) or m.group(4) or "").strip()
        case_num = (m.group(2) or m.group(3) or "").strip()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(normalized)
        block = normalized[start:end].strip()
        if stats_line:
            block = stats_line + "\n\n" + block

        # Try to extract the actual incident date from "Date: Month D, YYYY".
        # Clamp to the release date if the parsed date is more than 30 days in
        # the future — CK sometimes publishes typos like "July 11" for "June 11".
        date_match = _re.search(r"Date:\s+([A-Za-z]+ \d{1,2},?\s*\d{4})", block)
        incident_date = normalize_date(date_match.group(1)) if date_match else date
        if incident_date and date:
            from datetime import date as _date_type, timedelta
            try:
                parsed = _date_type.fromisoformat(incident_date)
                release = _date_type.fromisoformat(date)
                if parsed > release + timedelta(days=30):
                    incident_date = date
            except ValueError:
                pass

        results.append({
            "title": f"{incident_type} ({case_num})",
            "url": url,
            "date": incident_date,
            "service_name": service_name,
            "content": block,
        })

    return results


def _clean_hamilton_content(text: str, title: str) -> str:
    """
    Strip PressPoint boilerplate from Hamilton Police release content.

    Each release page includes:
    - A header: city name, date parts, timezone, and the release title repeated
    - A footer: "Related Stories" sidebar listing other releases in the same
      date/time/timezone format
    """
    import re as _re

    # Strip header: everything up to and including the repeated title line.
    if title:
        escaped = _re.escape(title.strip())
        text = _re.sub(r"^.*?" + escaped + r"\n", "", text, count=1, flags=_re.DOTALL)

    # Strip footer at "Related Stories" (PressPoint sidebar listing other releases)
    for marker in ("\nRelated Stories", "\nDownload Media Kit"):
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]

    text = _re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_NOISE_LINES_EXACT = frozenset({
    # Social media nav
    "x", "twitter", "facebook", "instagram", "youtube", "linkedin", "tiktok", "snapchat",
    # UI buttons
    "menu", "search", "print this page", "subscribe", "email", "more", "share",
    # TPS/website link qualifiers (appear as text from accessibility labels)
    "(opens in new window)", "(opens a print window)",
    # Alert/compat banners
    "close alert banner", "close old browser notification", "browser compatibility notification",
    "skip to content",
    # Emergency nav (not content)
    "emergency:", "report online", "contact us", "i want to",
    # Release labels
    "official news release download", "download media kit",
    "back to news search", "back to search",
    "subscribe to news", "subscribe to this page",
    # CMS category/section labels (standalone)
    "media release", "media releases", "news release", "news releases",
    "general releases", "public advisories", "police media releases",
    # Archive nav
    "see more",
    # Phone header labels
    "non emergency phone:", "non emergencies",
    # CMS artifacts
    "defaultinterior", "testing xsl",
    # Text size controls
    "decrease text size", "default text size", "increase text size",
    # Language toggles
    "en", "fr",
})

_NOISE_LINE_PATTERNS = [
    re.compile(r'^\d{3}[-.\s]\d{3}[-.\s]\d{4}$'),      # 10-digit phone
    re.compile(r'^\d{1,2}$'),                            # single/double digit (9-1-1 split)
    re.compile(r'^Non-?emergency:\s*\d'),                # non-emergency phone lines
    re.compile(r'^(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}$'),
    re.compile(r'^\d{4}$'),                              # bare years (archive nav)
    re.compile(r'^file://'),                             # CMS file path errors
    re.compile(r'^It appears you are trying to access'),
    re.compile(r'^As a result, parts of the site may not function'),
    re.compile(r'^We recommend updating your browser'),
    re.compile(r'^Published on:\s'),                     # Barrie/WordPress "Published on: date | Categories:"
    re.compile(r'^Unit:\s*$'),                           # TPS article header label
    re.compile(r'^Case #:'),                             # TPS case number line
    re.compile(r'^Published:\s+\w+day,'),                # TPS "Published: Thursday, ..." line
]

_CONTENT_FOOTER_MARKERS = (
    "\n-30-",
    "\nRelated Stories",
    "\nRelated News\n",
    "\nDownload Media Kit",
    "\nSubscribe to News\n",
    "\nSubscribe to this page",
    "\nShow More\nABOUT US",
    "\nABOUT US\nUNITS",
    "\nShare Story\n",
    "\nPreparing your download...\n",
    "\nAbout Us\nOur People\n",
    "\nView this post on Instagram\n",
    "\nReport A Crime\nThe work we do",
    "\nMedia Officer\n",
    "\nEmail. Media Officer\n",
    # Regina/Elementor WordPress pagination nav
    "\nPrev\nPrevious\n",
    "\nMore News\n",
    "\n← Back\n",
    # SPVM site-wide footer
    "\ncommunicate\nwith the SPVM\n",
    "\ncommunicate with the SPVM\n",
)


def clean_content(text: str, title: str = "") -> str:
    """
    Strip common CMS navigation boilerplate from scraped press release text.

    Removes standalone social media links, phone numbers, month/year archive
    nav, browser compatibility warnings, CMS error artifacts, and common UI
    labels. If title is provided and found within the first 1000 chars, strips
    everything up to and including the title line (it is shown separately in
    the card UI). Also strips common footer sections.
    """
    import re as _re

    # Strip TPS article header block up to "Share / (opens in new window)" marker.
    # Must run FIRST before noise-line removal strips parts of the marker string.
    _tps_marker = "\nShare\n(opens in new window)\n"
    _idx = text.find(_tps_marker)
    if 0 <= _idx < 600:
        text = text[_idx + len(_tps_marker):]

    # Strip SPVM full-site navigation block that precedes press release content.
    # The nav always ends with the "Vidéos" menu item before the article begins.
    _spvm_marker = "\nVidéos\n"
    _idx = text.find(_spvm_marker)
    if 0 <= _idx < 2000:
        text = text[_idx + len(_spvm_marker):]

    lines = text.split("\n")
    cleaned = []
    for line in lines:
        s = line.strip()
        if s.lower() in _NOISE_LINES_EXACT:
            continue
        if any(p.search(s) for p in _NOISE_LINE_PATTERNS):
            continue
        cleaned.append(line)

    text = "\n".join(cleaned)

    # Strip leading boilerplate up to and including the repeated title
    if title:
        idx = text.find(title.strip())
        if 0 <= idx < 1000:
            newline_after = text.find("\n", idx + len(title.strip()))
            text = text[newline_after + 1:] if newline_after != -1 else text[idx + len(title.strip()):]
            # Strip "Official News Release Download" immediately after the title
            text = _re.sub(r"^Official News Release Download\s*\n", "", text)

    # Second title-strip pass: some CMS templates (e.g. ALERT) repeat the title after the date
    if title:
        idx2 = text.find(title.strip())
        if 0 <= idx2 < 200:
            newline_after2 = text.find("\n", idx2 + len(title.strip()))
            text = text[newline_after2 + 1:] if newline_after2 != -1 else text[idx2 + len(title.strip()):]

    # Strip leading category/city header fragments from Elementor "read more" widgets
    # Handles: "City\nCity…\n" and "City| Category\nCity…\n"
    text = _re.sub(r"^([A-Za-z][A-Za-z ]{0,29})(?:\| [^\n]*)?\n\1[…\.]+\n", "", text)

    # Strip RCMP leading "- \n\t\t\t..." artefact (CMS list-item prefix with tab indent)
    text = _re.sub(r"^-[ \t]*\n[\t]+", "", text)

    # Strip PEI/Charlottetown WordPress leading "|\nNews Releases\n" nav artefact
    text = _re.sub(r"^\|\n(?:News Releases\n)?", "", text)

    # Strip RCMP "On this page" nav block (e.g. "On this page\nContent\nContent\n"
    # or with Image gallery / Contacts / Downloads labels in any order)
    text = _re.sub(
        r"On this page\n(?:(?:Content|Image gallery|Contacts|Downloads?|Videos?)\n)+",
        "",
        text,
        count=1,
    )

    # Strip VPD-style byline: Author\nISO-timestamp\nDate\n|\nCategory\n|
    text = _re.sub(
        r"^[A-Za-z ]+\n\d{4}-\d{2}-\d{2}T[^\n]+\n[^\n]+\n\|\n[^\n]+\n\|\n",
        "",
        text,
    )

    # Strip GovDelivery/CivicPlus byline block that follows the title:
    #   "By\n[Service Name]\n-\n[Date]\n[optional short category lines]"
    # Also handles the no-"By" variant: "-\n[Date]\n[category]"
    # Category lines are short (≤60 chars); real content sentences are much longer.
    text = _re.sub(
        r"^(?:By\n[^\n]+\n)?-\n[^\n]+\n(?:[^\n]{1,60}\n){0,2}",
        "",
        text,
    )
    # Strip any orphaned date or CMS file-number lines left after byline removal
    text = _re.sub(
        r"^(?:[A-Z][a-z]+ \d{1,2},?\s+\d{4}\n|\d{1,2} [A-Za-z]+ \d{4}\n|\d{2}-\d{5,}\n)+",
        "",
        text,
    )

    # Strip common footer sections
    for marker in _CONTENT_FOOTER_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]

    # Join name+comma breaks: "LASTNAME\n, age 38" → "LASTNAME, age 38"
    text = _re.sub(r"\n, ", ", ", text)

    # Collapse space/newline before ordinal suffixes from <sup> extraction: "26 th" / "26\nth" → "26th"
    text = _re.sub(r"(\d)[ \n](st|nd|rd|th)\b", r"\1\2", text)

    # Strip Bridgewater/GovDelivery release header block
    # "News Room\nFor Immediate Release\nBridgewater Police Service – Media Release\nDate: ...\nSubject: ..."
    text = _re.sub(
        r"^News Room\nFor Immediate Release\n[^\n]+\nDate: [^\n]+\n(?:Subject: [^\n]+\n)?",
        "",
        text,
    )

    # Join orphaned bullet characters (·\nText) onto the same line as their content
    text = _re.sub(r"·\n", "· ", text)

    # Strip Calgary PressPoint attachment filename lines (e.g. "CA26249031_\nCA26249031.JPG_")
    text = _re.sub(r"\n[A-Za-z0-9 ._-]+_(\n|$)", "\n", text)

    # Normalize non-breaking spaces (HTML &nbsp; artifacts from CMS table layouts)
    text = text.replace("\xa0", " ")

    text = _re.sub(r"\n{3,}", "\n\n", text)
    # Collapse runs of spaces/tabs (CMS layout artefacts from table/float-based HTML)
    text = _re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def fetch_release_content(url: str) -> str | None:
    """
    Fetch an individual press release page and return its plain-text body.

    Tries to extract text from the most specific content container available
    (<article>, <main>, .content, .entry-content, etc.). Falls back to <body>.
    Returns None on network error, JS-rendered sites, or if no usable text is found.
    """
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    if host in _JS_RENDERED_HOSTS:
        return None
    if host == "www.edmontonpolice.ca":
        return _fetch_edmonton_content(url)
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

    # CK-specific: extract and format the daily statistics table. Store in the
    # module-level cache keyed by URL so split_ck_daily_release() can retrieve
    # it after clean_content() has run (which would otherwise strip the stats).
    if host == "ckpolice.com":
        stats_table = soup.select_one("table.has-fixed-layout")
        if stats_table:
            cells = [" ".join(td.get_text(separator=" ", strip=True).split())
                     for td in stats_table.find_all("td")]
            cells = [c for c in cells if c]
            if cells:
                _ck_stats_cache[url] = " | ".join(cells)
            stats_table.decompose()

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


TPS_RSS_URL = "https://www.tps.ca/media-centre/news-releases/rss/"


def fetch_tps_items() -> list[dict]:
    """
    Fetch Toronto Police Service news releases via their Atom feed.

    tps.ca is behind Cloudflare bot protection that blocks plain requests and
    even Playwright stealth. curl_cffi impersonates Chrome's TLS fingerprint
    at the network layer, which bypasses the challenge.
    """
    import xml.etree.ElementTree as ET
    from curl_cffi import requests as cffi_requests

    resp = cffi_requests.get(TPS_RSS_URL, impersonate="chrome124", timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    results = []
    for entry in root.findall("atom:entry", ns):
        title = (entry.findtext("atom:title", "", ns) or "").strip()
        link_el = entry.find("atom:link", ns)
        url = link_el.get("href", "").strip() if link_el is not None else ""
        published = (entry.findtext("atom:published", "", ns) or "")[:10] or None
        summary_html = entry.findtext("atom:summary", "", ns) or ""
        content = None
        if summary_html:
            import html as _html
            # Atom summary is HTML-entity-encoded HTML (double-encoded from feed) — decode once
            summary_html = _html.unescape(summary_html)
            content = BeautifulSoup(summary_html, "html.parser").get_text(separator="\n", strip=True)
            if len(content) < 20:
                content = None
        if title and url:
            results.append({"title": title, "url": url, "date": published, "content": content})
    return results


OPP_API_URL = "https://www.opp.ca/protonapi/entry/list/"
OPP_NEWS_BASE = "https://www.opp.ca/news/viewnews/"

OPPNEWS_API_BASE = "https://oppnews.ca/api/articles/search/1/200"
OPPNEWS_ARTICLE_BASE = "https://oppnews.ca/#!/article/"
OPPNEWS_SESSION_FILE = Path("opp_session_id.txt")

RCMP_NEWS_URL = "https://rcmp.ca/en/news"

VPD_API_URL = "https://vpd.ca/wp-json/wp/v2/posts"
WINNIPEG_NEWS_URL = "https://www.winnipeg.ca/police/community/news-releases"
NELSON_NEWS_URL = "https://www.nelson.ca/CivicAlerts.aspx?CID=7"


def fetch_nelson_items() -> list[dict]:
    """
    Fetch Nelson Police Department news from the CivicAlert CMS.

    The listing page uses generic 'Media release' link text. This fetcher
    resolves each article's real title and date from the individual article
    page, so those fields are available without a second fetch later.
    """
    resp = requests.get(
        NELSON_NEWS_URL,
        timeout=15,
        headers={"User-Agent": USER_AGENT},
        verify=False,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    results = []
    seen_urls = set()

    for li in soup.select("li.list-group-item"):
        a = li.select_one("a.article-title-link")
        if not a:
            continue
        href = a.get("href", "")
        m = re.search(r"/Detail/(\d+)$", href)
        if not m:
            continue
        article_id = m.group(1)
        url = f"https://www.nelson.ca/CivicAlerts.aspx?AID={article_id}"
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Fetch the article page for date, title, and content
        try:
            ar = requests.get(
                url,
                timeout=15,
                headers={"User-Agent": USER_AGENT},
                verify=False,
            )
            ar.raise_for_status()
        except Exception:
            results.append({"title": a.get_text(strip=True), "url": url, "date": None, "content": None})
            continue

        asoup = BeautifulSoup(ar.text, "html.parser")
        for tag in asoup(["script", "style", "nav", "header", "footer", "form", "noscript", "aside"]):
            tag.decompose()

        raw = (asoup.find("main") or asoup.find("body") or asoup).get_text(separator="\n", strip=True)
        raw = re.sub(r"\n{3,}", "\n\n", raw)

        # Date: "Posted on June 03, 2026"
        date_str = None
        dm = re.search(r"Posted on ([A-Za-z]+ \d{1,2}, \d{4})", raw)
        if dm:
            date_str = normalize_date(dm.group(1))

        # Real title: first short, non-generic line after the date stamp
        title = a.get_text(strip=True)
        after_date = re.search(r"Posted on [A-Za-z]+ \d{1,2}, \d{4}\n+", raw)
        if after_date:
            for line in raw[after_date.end():].split("\n"):
                s = line.strip()
                if not s:
                    continue
                # Skip known boilerplate lines
                if s.upper() in ("FOR IMMEDIATE RELEASE", "MEDIA RELEASE", "MEDIA RELEASES"):
                    continue
                if re.match(r"^Nelson,?\s+B\.?C\.?\.?$", s, re.IGNORECASE):
                    continue
                if re.match(r"^Media Release\s*[–\-]", s, re.IGNORECASE):
                    continue
                # Accept as title only if it fits on one readable line
                if 10 <= len(s) <= 120:
                    title = s
                break  # stop at the first real content line regardless

        # Content: everything after the date stamp line
        content = None
        after = re.split(r"Posted on [A-Za-z]+ \d{1,2}, \d{4}\n*", raw, maxsplit=1)
        if len(after) > 1:
            body = after[1].strip()
            if len(body) > 50:
                content = body

        results.append({"title": title, "url": url, "date": date_str, "content": content})

    return results


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


def fetch_oppnews_items() -> list[dict]:
    """Fetch OPP press releases from oppnews.ca authenticated API.

    Requires opp_session_id.txt in the project root. Returns an empty list
    silently if the file is missing or if the session has expired.
    """
    import json as _json
    import re as _re
    from datetime import datetime as _dt

    if not OPPNEWS_SESSION_FILE.exists():
        return []
    session_id = OPPNEWS_SESSION_FILE.read_text().strip()
    if not session_id:
        return []

    payload = {
        "displaydate": _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "regionid": 0,
        "subscriptions": 1,
        "categoryid": 0,
    }
    try:
        resp = requests.post(
            OPPNEWS_API_BASE,
            json=payload,
            timeout=20,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://oppnews.ca",
                "Referer": "https://oppnews.ca/search",
            },
            cookies={"session_id": session_id},
            verify=False,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    if isinstance(data, dict):
        return []

    results = []
    for article in data:
        news_id = article.get("newsid")
        title = (article.get("title") or "").strip()
        if not news_id or not title:
            continue
        date_str = article.get("displaydate") or None
        raw_html = article.get("body") or ""
        content = None
        if raw_html:
            text = BeautifulSoup(raw_html, "html.parser").get_text(separator="\n", strip=True)
            text = _re.sub(r"\n{3,}", "\n\n", text)
            if len(text) > 50:
                content = text
        results.append({
            "title": title,
            "url": OPPNEWS_ARTICLE_BASE + str(news_id),
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


def fetch_stlatlimx_items(cutoff_days: int = 730) -> list[dict]:
    """Fetch St'át'l̓imx Tribal Police news, paginating through all pages.

    The listing shows 2 items per page (?paged=N). Dates are embedded in
    the title text as "Month DD, YYYY: Real title".
    """
    import re as _re
    from datetime import date as _date, timedelta as _timedelta
    from urllib.parse import urljoin

    base = "https://stlatlimxpolice.ca/?page_id=504"
    cutoff = (_date.today() - _timedelta(days=cutoff_days)).isoformat()
    results = []
    seen_urls: set[str] = set()

    for page in range(1, 50):
        url = base if page == 1 else f"{base}&paged={page}"
        try:
            resp = requests.get(url, timeout=15, headers={"User-Agent": USER_AGENT}, verify=False)
            resp.raise_for_status()
        except Exception:
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        items_on_page = soup.select("h4.uagb-post__title a")
        if not items_on_page:
            break

        page_had_recent = False
        for a in items_on_page:
            href = a.get("href", "")
            if not href or href in seen_urls:
                continue
            seen_urls.add(href)
            raw_title = a.get_text(strip=True)
            # Extract "Month DD, YYYY: Real title"
            m = _re.match(r"^([A-Z][a-z]+ \d{1,2},\s*\d{4}):\s*(.*)", raw_title)
            if m:
                date_str = m.group(1)
                title = m.group(2).strip()
            else:
                date_str = None
                title = raw_title
            # Normalize date to check cutoff
            norm = normalize_date(date_str) if date_str else None
            if norm and norm < cutoff:
                continue
            page_had_recent = True
            results.append({"title": title, "url": href, "date": date_str})

        if not page_had_recent:
            break  # all items on this page are older than cutoff

    return results


def fetch_cbrps_items() -> list[dict]:
    """Fetch Cape Breton Regional Police media releases.

    Some items have a 'Read more' link (URL set, content fetched later).
    Others are fully inline on the listing page (content extracted here,
    URL set to the listing page so the title-based dedup still works).
    """
    from urllib.parse import urljoin
    base = "https://www.cbrps.ca/media-releases/"
    resp = requests.get(base, timeout=15, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    seen_titles: set[str] = set()
    for container in soup.select("div.item-content"):
        heading = container.find(["h2", "h3"])
        if not heading:
            continue
        title = heading.get_text(strip=True)
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        # Date is in <p><em>...</em></p>
        date_str = None
        em = container.find("em")
        if em:
            date_str = em.get_text(strip=True)
        # Check for a "Read more" link
        readmore = container.select_one("p.readmore a[href]")
        if readmore:
            url = urljoin(base, readmore["href"])
            content = None  # fetched later by fetch_release_content
        else:
            url = base
            # Extract inline content (skip heading and readmore paragraphs)
            paras = []
            for p in container.find_all("p"):
                if p.find_parent(class_="readmore"):
                    continue
                if p.find("em") and p.get_text(strip=True) == (date_str or ""):
                    continue  # skip the date paragraph
                text = p.get_text(separator=" ", strip=True)
                if text:
                    paras.append(text)
            content = "\n".join(paras) if paras else None
        results.append({"title": title, "url": url, "date": date_str, "content": content})
    return results


def fetch_vicpd_items() -> list[dict]:
    """Fetch Victoria Police Department news from Ghost CMS tag feed.

    The post-link anchor wraps the whole card, so title comes from img alt.
    """
    from urllib.parse import urljoin
    resp = requests.get(
        "https://engagement.vicpd.ca/tag/news-releases/",
        timeout=15,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    seen_urls = set()
    for a in soup.select("a.post-link"):
        href = a.get("href", "")
        if not href:
            continue
        url = urljoin("https://engagement.vicpd.ca", href)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        img = a.find("img")
        title = (img.get("alt", "").strip() if img else "") or a.get_text(strip=True)
        if not title:
            continue
        results.append({"title": title, "url": url, "date": None})
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
    from urllib.parse import urljoin
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
    from urllib.parse import urljoin
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
    from urllib.parse import urljoin, urlparse
    try:
        # Special cases: sites that require custom fetching
        if "tps.ca" in url:
            raw_links = fetch_tps_items()
        elif "opp.ca" in url:
            raw_links = fetch_opp_items()
            raw_links.extend(fetch_oppnews_items())
        elif "rcmp.ca" in url:
            raw_links = fetch_rcmp_items()
        elif "vpd.ca" in url:
            raw_links = fetch_vpd_items()
        elif "winnipeg.ca/police" in url:
            raw_links = fetch_winnipeg_items()
        elif "nelson.ca" in url:
            raw_links = fetch_nelson_items()
        elif "cbrps.ca" in url:
            raw_links = fetch_cbrps_items()
        elif "engagement.vicpd.ca" in url:
            raw_links = fetch_vicpd_items()
        elif "stlatlimxpolice.ca" in url:
            raw_links = fetch_stlatlimx_items()
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
                "content": lnk.get("content"),
            }
            for lnk in raw_links[:10]
        ]
        return items, None
    except Exception as e:
        return [], str(e)


# --- Feed builder ---


def build_feed(
    archive_dir: Path,
    tps_ndjson: Path,
    output_dir: Path,
    days: int = 365,
) -> None:
    """
    Build the card feed from JSON archive files and TPS NDJSON.
    Writes docs/data.json and docs/index.html.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    print(f"  [build_feed] Cutoff: {cutoff} ({days} days)")

    # Load press releases from per-service archive files
    press_items = []
    if archive_dir.exists():
        for f in sorted(archive_dir.glob("*.json")):
            try:
                releases = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for item in releases:
                item_date = normalize_date(item.get("date"))
                if item_date is not None and item_date < cutoff:
                    continue
                sort_key = item_date or ""
                press_items.append({
                    "type": "press_release",
                    "title": item.get("title", ""),
                    "url": item.get("url"),
                    "date": item_date,
                    "source": item.get("service_name", ""),
                    "content": item.get("content"),
                    "search_text": " ".join(
                        s.lower()
                        for s in [
                            item.get("title") or "",
                            item.get("service_name") or "",
                            item.get("content") or "",
                        ]
                        if s
                    ),
                    "_sort_key": sort_key,
                })
    print(f"  [build_feed] Press releases in window: {len(press_items)}")

    # Load TPS calls from NDJSON
    tps_items = []
    if tps_ndjson.exists():
        for line in tps_ndjson.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            occurred_at = rec.get("occurred_at")
            if not occurred_at:
                continue
            rec_date = occurred_at[:10]
            if rec_date < cutoff:
                continue
            tps_items.append({
                "type": "tps_call",
                "title": rec.get("call_type") or "",
                "call_type": rec.get("call_type") or "",
                "url": None,
                "date": rec_date,
                "occurred_at": occurred_at,
                "source": "Toronto Police Service",
                "division": rec.get("division") or "",
                "cross_streets": rec.get("cross_streets") or "",
                "search_text": " ".join(
                    s.lower()
                    for s in [
                        rec.get("call_type") or "",
                        rec.get("division") or "",
                        rec.get("cross_streets") or "",
                    ]
                    if s
                ),
                "_sort_key": occurred_at,
            })
    print(f"  [build_feed] TPS calls in window: {len(tps_items)}")

    all_items = sorted(press_items + tps_items, key=lambda x: x["_sort_key"], reverse=True)
    for item in all_items:
        item.pop("_sort_key", None)

    output_dir.mkdir(parents=True, exist_ok=True)
    data_path = output_dir / "data.json"
    data_path.write_text(json.dumps(all_items, ensure_ascii=False), encoding="utf-8")
    print(f"  [build_feed] Wrote {data_path} ({len(all_items)} items)")

    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/Toronto"))
    et_label = now_et.strftime("%Z")
    generated_at = now_et.strftime(f"%B %d, %Y at %H:%M {et_label}")

    raw_provinces: dict[str, set[str]] = {}
    for src in load_sources():
        prov = src.get("province") or "Other"
        raw_provinces.setdefault(prov, set()).add(src["name"])

    provinces: dict[str, list[str]] = {}
    for prov in _PROVINCE_ORDER:
        if prov in raw_provinces:
            provinces[prov] = sorted(raw_provinces[prov])
    for prov in sorted(raw_provinces):
        if prov not in provinces:
            provinces[prov] = sorted(raw_provinces[prov])

    service_paths: dict[str, str] = {}
    for prov, svcs in provinces.items():
        for svc in svcs:
            service_paths[svc] = f"{_slugify(prov)}/{_slugify(svc)}"

    total_services = sum(len(svcs) for svcs in provinces.values())

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    try:
        template = env.get_template("feed.html")
    except Exception as e:
        print(f"  [build_feed] WARNING: could not load feed.html template: {e}")
        return
    html = template.render(
        generated_at=generated_at,
        provinces=provinces,
        service_paths=service_paths,
        total_services=total_services,
    )
    index_path = output_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")
    print(f"  [build_feed] Wrote {index_path}")


# --- JSON storage helpers ---


def _load_seen_items() -> dict:
    """Load {hash: ISO-timestamp} from seen_items.json. Returns {} if missing."""
    try:
        return json.loads(SEEN_ITEMS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_seen_items(seen: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_ITEMS_FILE.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")


def _service_slug(service_name: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "-", service_name.lower()).strip("-")


def _append_to_archive(service_name: str, items: list[dict]) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"{_service_slug(service_name)}.json"
    existing: list = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []
    # Dedup by title: skip incoming items whose title already exists in the archive
    existing_titles = {i.get("title", "") for i in existing}
    new_items = [i for i in items if i.get("title", "") not in existing_titles]
    existing.extend(new_items)
    path.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")


# --- Main ---


def main():
    from collections import defaultdict

    today_utc = datetime.now(timezone.utc).date()
    print(f"Police Scout — {today_utc}")

    seen = _load_seen_items()
    known = set(seen.keys())

    sources = load_sources()
    print(f"Loaded {len(sources)} sources")

    # Scrape all sources
    all_scraped: list[dict] = []
    failed_services: list[str] = []

    scrape_counts: dict[str, int] = {}
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
            scrape_counts[source["name"]] = 0
            continue
        n = len(items)
        print(f"{n} links found" + (" [ZERO — possible scrape issue]" if n == 0 else ""))
        scrape_counts[source["name"]] = n
        all_scraped.extend(items)

    # Dedup against seen hashes
    new_items = [i for i in all_scraped if item_hash(i["title"], i["url"] or "") not in known]
    print(f"\nScraped: {len(all_scraped)}, new: {len(new_items)}, failed services: {len(failed_services)}")

    # Fetch content and expand CK daily releases into per-incident items
    to_insert: list[dict] = []
    for item in new_items:
        url = item.get("url")
        content = item.get("content") or (fetch_release_content(url) if url else None)
        if content and item["service_name"] == "Hamilton Police Service":
            content = _clean_hamilton_content(content, item["title"])
        if content:
            content = clean_content(content, item["title"])

        base = {
            "item_hash": item_hash(item["title"], url or ""),
            "title": item["title"],
            "url": url,
            "date": normalize_date(item.get("date")),
            "service_name": item["service_name"],
            "content": content,
            "first_scraped_at": today_utc.isoformat(),
        }

        if item["service_name"] == "Chatham-Kent Police Service" and content:
            for incident in split_ck_daily_release({**base, "content": content}):
                to_insert.append({
                    "item_hash": item_hash(incident["title"], incident.get("url") or ""),
                    "title": incident["title"],
                    "url": incident.get("url"),
                    "date": incident.get("date"),
                    "service_name": incident["service_name"],
                    "content": incident.get("content"),
                    "first_scraped_at": today_utc.isoformat(),
                })
        else:
            to_insert.append(base)

    # Write to archive files and update seen dict
    by_service: defaultdict[str, list] = defaultdict(list)
    for item in to_insert:
        by_service[item["service_name"]].append(item)
    for service_name, service_items in by_service.items():
        _append_to_archive(service_name, service_items)
    for item in to_insert:
        seen[item["item_hash"]] = datetime.now(timezone.utc).isoformat()
    seen = prune_state(seen, today_utc)
    _save_seen_items(seen)
    print(f"Inserted {len(to_insert)} new items")

    # Per-service new-item summary
    print("\n── New releases by service ──")
    all_service_names = sorted(scrape_counts.keys())
    for name in all_service_names:
        n_new = len(by_service.get(name, []))
        n_scraped = scrape_counts.get(name, 0)
        if n_new > 0:
            status = f"+{n_new}"
        elif name in failed_services:
            status = "FAILED"
        elif n_scraped == 0:
            status = "0 links"
        else:
            status = "—"
        print(f"  {name}: {status}")
    if failed_services:
        print(f"\nFailed ({len(failed_services)}): {', '.join(failed_services)}")

    # Build the static feed
    build_feed(archive_dir=ARCHIVE_DIR, tps_ndjson=TPS_NDJSON, output_dir=DOCS_DIR, days=365)
    print("Done.")


if __name__ == "__main__":
    main()
