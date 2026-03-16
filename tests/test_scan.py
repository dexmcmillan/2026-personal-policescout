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


# --- is_press_release_url ---

def test_is_press_release_url_matches_news():
    assert scan.is_press_release_url("https://police.ca/news/some-release") is True

def test_is_press_release_url_matches_release():
    assert scan.is_press_release_url("https://police.ca/media-releases/2026-01") is True

def test_is_press_release_url_matches_newsroom():
    assert scan.is_press_release_url("https://police.ca/newsroom/") is True

def test_is_press_release_url_rejects_homepage():
    assert scan.is_press_release_url("https://police.ca/") is False

def test_is_press_release_url_rejects_about():
    assert scan.is_press_release_url("https://police.ca/about-us") is False

def test_is_press_release_url_rejects_contact():
    assert scan.is_press_release_url("https://police.ca/contact") is False

def test_is_press_release_url_case_insensitive():
    assert scan.is_press_release_url("https://police.ca/News/Release-1") is True


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
        <li><a href="https://example.com/news/valid">Valid Release</a></li>
      </ul>
    </main>
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    links = scan.extract_links(soup, "https://example.com/")
    assert len(links) == 1
    assert links[0]["url"] == "https://example.com/news/valid"

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

def test_extract_links_filters_non_press_release_urls():
    html = """
    <main><ul>
      <li><a href="https://example.com/about-us">About Us</a></li>
      <li><a href="https://example.com/contact">Contact</a></li>
      <li><a href="https://example.com/news/2026-arrest">Arrest Notice</a></li>
      <li><a href="https://example.com/recruitment">Join Us</a></li>
    </ul></main>
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    links = scan.extract_links(soup, "https://example.com/")
    urls = [l["url"] for l in links]
    # Only the news link should survive the keyword filter
    assert urls == ["https://example.com/news/2026-arrest"]

def test_extract_links_uses_stripped_title():
    html = """
    <main><ul>
      <li><a href="https://example.com/news/1">  Spaced Title  </a></li>
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


# --- normalize_date ---

def test_normalize_date_iso():
    assert scan.normalize_date("2026-03-13") == "2026-03-13"

def test_normalize_date_long_month():
    assert scan.normalize_date("March 13, 2026") == "2026-03-13"

def test_normalize_date_short_month():
    assert scan.normalize_date("Mar 13, 2026") == "2026-03-13"

def test_normalize_date_day_month_year():
    assert scan.normalize_date("13 March 2026") == "2026-03-13"

def test_normalize_date_none():
    assert scan.normalize_date(None) is None

def test_normalize_date_unparseable():
    assert scan.normalize_date("not a date") is None
