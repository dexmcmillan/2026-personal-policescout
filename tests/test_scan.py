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


# --- build_feed ---

import json as _json


def _make_archive(tmp_path, slug, items):
    """Write a fake archive JSON file."""
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir(exist_ok=True)
    (archive_dir / f"{slug}.json").write_text(
        _json.dumps(items, ensure_ascii=False), encoding="utf-8"
    )
    return archive_dir


def _make_tps_ndjson(tmp_path, records):
    """Write a fake tps_calls.ndjson file."""
    p = tmp_path / "tps_calls.ndjson"
    p.write_text(
        "\n".join(_json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return p


def test_build_feed_creates_data_json(tmp_path):
    """build_feed() writes docs/data.json with merged items."""
    today = date.today().isoformat()
    archive_dir = _make_archive(tmp_path, "ottawa-police", [
        {"title": "Arrest made", "url": "https://example.com/1", "date": today, "service_name": "Ottawa Police"},
    ])
    tps = _make_tps_ndjson(tmp_path, [
        {
            "objectid": 1, "occurred_at": f"{today}T12:00:00+00:00",
            "division": "D11", "call_type": "ASSAULT", "call_type_code": "ASS",
            "cross_streets": "MAIN - FIRST", "latitude": 43.6, "longitude": -79.4,
            "collected_at": f"{today}T12:01:00+00:00",
        }
    ])
    output_dir = tmp_path / "docs"
    scan.build_feed(archive_dir=archive_dir, tps_ndjson=tps, output_dir=output_dir, days=7)

    data_file = output_dir / "data.json"
    assert data_file.exists()
    items = _json.loads(data_file.read_text())
    assert len(items) == 2

    types = {i["type"] for i in items}
    assert types == {"press_release", "tps_call"}


def test_build_feed_filters_old_items(tmp_path):
    """Items older than `days` are excluded."""
    old_date = (date.today() - timedelta(days=8)).isoformat()
    today = date.today().isoformat()
    archive_dir = _make_archive(tmp_path, "ottawa-police", [
        {"title": "Old release", "url": "https://example.com/old", "date": old_date, "service_name": "Ottawa"},
        {"title": "New release", "url": "https://example.com/new", "date": today, "service_name": "Ottawa"},
    ])
    output_dir = tmp_path / "docs"
    scan.build_feed(archive_dir=archive_dir, tps_ndjson=tmp_path / "missing.ndjson", output_dir=output_dir, days=7)

    items = _json.loads((output_dir / "data.json").read_text())
    assert len(items) == 1
    assert items[0]["title"] == "New release"


def test_build_feed_missing_tps_file_produces_press_release_only_feed(tmp_path):
    """Missing tps_calls.ndjson -> feed contains only press releases, no error."""
    today = date.today().isoformat()
    archive_dir = _make_archive(tmp_path, "ottawa-police", [
        {"title": "Arrest", "url": "https://example.com/1", "date": today, "service_name": "Ottawa"},
    ])
    output_dir = tmp_path / "docs"
    scan.build_feed(archive_dir=archive_dir, tps_ndjson=tmp_path / "no_tps.ndjson", output_dir=output_dir, days=7)

    items = _json.loads((output_dir / "data.json").read_text())
    assert all(i["type"] == "press_release" for i in items)


def test_build_feed_search_text_content(tmp_path):
    """search_text is a lowercase concat of relevant fields."""
    today = date.today().isoformat()
    archive_dir = _make_archive(tmp_path, "ottawa-police", [
        {"title": "Arrest in Vanier", "url": "https://example.com/1", "date": today, "service_name": "Ottawa Police Service"},
    ])
    output_dir = tmp_path / "docs"
    scan.build_feed(archive_dir=archive_dir, tps_ndjson=tmp_path / "missing.ndjson", output_dir=output_dir, days=7)

    items = _json.loads((output_dir / "data.json").read_text())
    pr = next(i for i in items if i["type"] == "press_release")
    assert "arrest in vanier" in pr["search_text"]
    assert "ottawa police service" in pr["search_text"]


def test_build_feed_tps_search_text_includes_location(tmp_path):
    """TPS search_text includes division and cross_streets."""
    today = date.today().isoformat()
    archive_dir = _make_archive(tmp_path, "dummy", [])
    archive_dir.mkdir(exist_ok=True)
    tps = _make_tps_ndjson(tmp_path, [
        {
            "objectid": 99, "occurred_at": f"{today}T10:00:00+00:00",
            "division": "D51", "call_type": "ROBBERY", "call_type_code": "ROB",
            "cross_streets": "GOULD ST - MUTUAL ST", "latitude": 43.66, "longitude": -79.38,
            "collected_at": f"{today}T10:01:00+00:00",
        }
    ])
    output_dir = tmp_path / "docs"
    scan.build_feed(archive_dir=archive_dir, tps_ndjson=tps, output_dir=output_dir, days=7)

    items = _json.loads((output_dir / "data.json").read_text())
    tps_item = next(i for i in items if i["type"] == "tps_call")
    assert "d51" in tps_item["search_text"]
    assert "gould st" in tps_item["search_text"]


def test_build_feed_sorts_newest_first(tmp_path):
    """Items are sorted newest-first by sort key."""
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    archive_dir = _make_archive(tmp_path, "ottawa-police", [
        {"title": "Old PR", "url": "https://example.com/old", "date": yesterday, "service_name": "Ottawa"},
        {"title": "New PR", "url": "https://example.com/new", "date": today, "service_name": "Ottawa"},
    ])
    output_dir = tmp_path / "docs"
    scan.build_feed(archive_dir=archive_dir, tps_ndjson=tmp_path / "missing.ndjson", output_dir=output_dir, days=7)

    items = _json.loads((output_dir / "data.json").read_text())
    assert items[0]["date"] >= items[-1]["date"]


def test_build_feed_null_dates_sort_to_end(tmp_path):
    """Items with date=null sort after dated items."""
    today = date.today().isoformat()
    archive_dir = _make_archive(tmp_path, "ottawa-police", [
        {"title": "No Date", "url": "https://example.com/nodate", "date": None, "service_name": "Ottawa"},
        {"title": "Has Date", "url": "https://example.com/dated", "date": today, "service_name": "Ottawa"},
    ])
    output_dir = tmp_path / "docs"
    scan.build_feed(archive_dir=archive_dir, tps_ndjson=tmp_path / "missing.ndjson", output_dir=output_dir, days=7)

    items = _json.loads((output_dir / "data.json").read_text())
    assert items[0]["title"] == "Has Date"
    assert items[-1]["title"] == "No Date"


def test_build_feed_malformed_archive_skipped(tmp_path):
    """A malformed archive JSON file is skipped; valid archives still processed."""
    today = date.today().isoformat()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "bad-service.json").write_text("NOT JSON", encoding="utf-8")
    (archive_dir / "ottawa-police.json").write_text(
        _json.dumps([{"title": "Good", "url": "https://example.com/good", "date": today, "service_name": "Ottawa"}]),
        encoding="utf-8",
    )
    output_dir = tmp_path / "docs"
    scan.build_feed(archive_dir=archive_dir, tps_ndjson=tmp_path / "missing.ndjson", output_dir=output_dir, days=7)

    items = _json.loads((output_dir / "data.json").read_text())
    assert len(items) == 1
    assert items[0]["title"] == "Good"


def test_build_feed_malformed_tps_line_skipped(tmp_path):
    """A malformed JSON line in tps_calls.ndjson is skipped; valid lines still processed."""
    today = date.today().isoformat()
    archive_dir = _make_archive(tmp_path, "dummy", [])
    tps = tmp_path / "tps_calls.ndjson"
    tps.write_text(
        "NOT JSON\n" +
        _json.dumps({
            "objectid": 5, "occurred_at": f"{today}T09:00:00+00:00",
            "division": "D12", "call_type": "THEFT", "call_type_code": "THE",
            "cross_streets": "KING ST - QUEEN ST", "latitude": 43.65, "longitude": -79.38,
            "collected_at": f"{today}T09:01:00+00:00",
        }) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "docs"
    scan.build_feed(archive_dir=archive_dir, tps_ndjson=tps, output_dir=output_dir, days=7)

    items = _json.loads((output_dir / "data.json").read_text())
    assert len(items) == 1
    assert items[0]["call_type"] == "THEFT"


def test_build_feed_tps_missing_occurred_at_skipped(tmp_path):
    """TPS records without occurred_at are skipped."""
    today = date.today().isoformat()
    archive_dir = _make_archive(tmp_path, "dummy", [])
    tps = _make_tps_ndjson(tmp_path, [
        {
            "objectid": 10, "occurred_at": None,
            "division": "D41", "call_type": "DISTURBANCE", "call_type_code": "DIS",
            "cross_streets": "YONGE - BLOOR", "latitude": 43.67, "longitude": -79.39,
            "collected_at": f"{today}T08:00:00+00:00",
        },
        {
            "objectid": 11, "occurred_at": f"{today}T08:30:00+00:00",
            "division": "D41", "call_type": "ALARM", "call_type_code": "ALM",
            "cross_streets": "YONGE - EGLINTON", "latitude": 43.70, "longitude": -79.40,
            "collected_at": f"{today}T08:31:00+00:00",
        },
    ])
    output_dir = tmp_path / "docs"
    scan.build_feed(archive_dir=archive_dir, tps_ndjson=tps, output_dir=output_dir, days=7)

    items = _json.loads((output_dir / "data.json").read_text())
    assert len(items) == 1
    assert items[0]["title"] == "ALARM"


def test_build_feed_creates_index_html(tmp_path):
    """build_feed() renders docs/index.html from the template."""
    today = date.today().isoformat()
    archive_dir = _make_archive(tmp_path, "ottawa-police", [
        {"title": "Test PR", "url": "https://example.com/1", "date": today, "service_name": "Ottawa"},
    ])
    output_dir = tmp_path / "docs"
    scan.build_feed(archive_dir=archive_dir, tps_ndjson=tmp_path / "missing.ndjson", output_dir=output_dir, days=7)

    index_file = output_dir / "index.html"
    assert index_file.exists()
    content = index_file.read_text()
    assert "Police Scout" in content
    assert "data.json" in content
