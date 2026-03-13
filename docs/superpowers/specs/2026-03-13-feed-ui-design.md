# Police Scout Feed UI Design

## Goal

Replace the static daily digest with a searchable, chronological card feed combining press releases (from all 36 archived police services, last 7 days) and TPS calls for service (last 7 days). Hosted on GitHub Pages as a static site; no server required.

---

## Architecture

The build step (`scan.py`) gains a new `build_feed()` function that:

1. Reads all `data/archive/*.json` files, normalizes dates, filters to last 7 days
2. Reads `data/tps_calls.ndjson`, filters to last 7 days
3. Merges into one sorted list (newest-first), writes `docs/data.json`
4. Renders `docs/index.html` from `templates/feed.html`

`docs/index.html` is a static shell that loads `docs/data.json` via `fetch()` on page load, then renders cards and wires up live search with vanilla JS. No frameworks, no bundler.

The existing `templates/digest.html` is retired (deleted). `scan.py`'s `main()` is updated to call `build_feed()` at the end, after the existing scraping and state-saving logic (which is preserved unchanged):

```python
def main():
    # ... existing scraping, deduplication, state-saving logic (unchanged) ...
    build_feed(
        archive_dir=Path("data/archive"),
        tps_ndjson=Path("data/tps_calls.ndjson"),
        output_dir=Path("docs"),
        days=7,
    )
```

`build_feed()` signature:
```python
def build_feed(archive_dir: Path, tps_ndjson: Path, output_dir: Path, days: int = 7) -> None
```
It raises no exceptions — errors (missing files, malformed JSON) are logged and skipped gracefully.

---

## Data Pipeline: `build_feed()` in `scan.py`

### Input

- `data/archive/*.json` — each file is a list of `{title, url, date, service_name}` dicts
- `data/tps_calls.ndjson` — one JSON object per line: `{objectid, occurred_at, division, call_type, call_type_code, cross_streets, latitude, longitude, collected_at}`

### Date normalization

Press release dates arrive in many formats ("March 13, 2026", "2026-03-13", "Mar 12, 2026", etc). The existing `is_within_cutoff()` logic in `backfill.py` already handles these — reuse that parsing approach to convert all dates to ISO `YYYY-MM-DD`. Items that cannot be parsed keep `date: null` and sort to the end.

For TPS calls, `date` is extracted from `occurred_at` (always present):
```python
date = occurred_at[:10]  # "2026-03-13T15:18:56+00:00" → "2026-03-13"
```

Cutoff: `date.today() - timedelta(days=7)`.

**Missing file handling:**
- If `tps_ndjson` does not exist, skip TPS data silently (log a warning, produce a press-releases-only feed).
- If an archive file contains malformed JSON, skip it and log a warning.
- Archive files with zero items in the 7-day window are simply excluded from output (no error).

### Unified item schema (`docs/data.json`)

Each element is one of two shapes:

**Press release:**
```json
{
  "type": "press_release",
  "title": "Man charged in Vanier assault",
  "url": "https://...",
  "date": "2026-03-13",
  "source": "Ottawa Police Service",
  "search_text": "man charged in vanier assault ottawa police service"
}
```

**TPS call:**
```json
{
  "type": "tps_call",
  "title": "ASSAULT",
  "url": null,
  "date": "2026-03-13",
  "occurred_at": "2026-03-13T15:18:56+00:00",
  "source": "Toronto Police Service",
  "division": "D11",
  "cross_streets": "PERTH AVE - FRANKLIN AVE",
  "search_text": "assault toronto police service d11 perth ave franklin ave"
}
```

`search_text` is a pre-built lowercase string concatenating: `title`, `source`, and (for TPS calls) `division` + `cross_streets`. Built at data-merge time in Python; the JS does nothing but `item.search_text.includes(query)`.

### Sort order

All items sorted by a sort key: for TPS calls, use `occurred_at` (full ISO timestamp); for press releases, use `date` (ISO date string, e.g. `"2026-03-13"`). Since `occurred_at` is always a full datetime and press release dates are date-only, TPS calls will sort precisely within a day while press releases on the same day cluster together. This is acceptable — no guarantee of exact interleaving between the two types within a day.

Items with `date: null` sort to the end (use `""` as sort key fallback).

### Output

`docs/data.json` — a JSON array of all normalized items, newest-first. Approximate size: ~500–800 KB for 7 days of data.

---

## Frontend: `templates/feed.html` → `docs/index.html`

### Structure

```
<head>
  <title>Police Scout</title>
  <meta viewport>
  <style>  <!-- all CSS embedded -->  </style>
</head>
<body>
  <header>
    <h1>Police Scout</h1>
    <p class="updated">Updated: {{ generated_at }}</p>
    <!-- generated_at example: "March 13, 2026 at 15:45 UTC"
         Python: datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC") -->
  </header>
  <div class="controls">
    <input type="search" id="search" placeholder="Search releases and calls…" autofocus>
    <span id="count"></span>
  </div>
  <div id="feed"></div>
  <script>  <!-- all JS inline -->  </script>
</body>
```

The Jinja2 template injects only `{{ generated_at }}` (a timestamp string). All card rendering is done in JS after `fetch('data.json')`.

### Card designs

**Press release card:**
```
┌─────────────────────────────────────────────────┐
│ Ottawa Police Service              Mar 13, 2026  │
│                                                  │
│ Man charged in Vanier assault investigation →    │
└─────────────────────────────────────────────────┘
```
- Source name (small, muted) + date (small, muted, right-aligned) on top row
- Title as a link (`<a href="...">`) on the second line
- White background, subtle border

**TPS call card** — visually distinct to make source clear at a glance:
```
┌─────────────────────────────────────────────────┐
│ TPS · D11                         3:18 PM today  │
│                                                  │
│ ASSAULT                                          │
│ PERTH AVE / FRANKLIN AVE                         │
└─────────────────────────────────────────────────┘
```
- "TPS · {division}" + relative time on top row. Format (all times UTC): same calendar day → "3:18 PM today"; previous calendar day → "yesterday 11:42 AM"; older → "Mar 12 at 11:42 AM"
- Call type (bold) on second line
- Cross streets (muted) on third line
- Light blue-grey background to distinguish from press releases
- No link (TPS calls have no external URL)

### Live search

Single `<input type="search" id="search">` above the feed. On `input` event:
- Lowercased query compared against each item's `search_text`
- Cards with no match get `display: none`
- A result count ("Showing 42 of 318") updates next to the search box
- Empty query shows all cards

No debounce needed — the comparison is O(n) string inclusion, fast enough for ~1000 items.

### Styling

- System font stack (matching current site)
- Max-width 800px, centred
- ~150 lines of embedded CSS
- No external dependencies (no CDN, no framework)
- Cards: `border-radius: 6px`, `padding: 12px 16px`, `margin-bottom: 8px`
- Press release: white background, `border: 1px solid #e0e0e0`
- TPS call: `background: #eef3f8`, `border: 1px solid #c8d8e8`

---

## Files Changed

| File | Change |
|------|--------|
| `scan.py` | Add `build_feed(archive_dir, tps_ndjson, output_dir, days=7)` function; update `main()` to call it |
| `templates/feed.html` | New Jinja2 template (replaces `digest.html`) |
| `templates/digest.html` | Deleted |
| `docs/index.html` | Generated by `build_feed()` |
| `docs/data.json` | Generated by `build_feed()` — committed by GitHub Actions |
| `.github/workflows/scan.yml` | No change needed — `git add docs/` already covers `docs/data.json` |

---

## GitHub Actions

The existing `scan.yml` workflow runs `scan.py` daily. The `git add` step currently stages `docs/` and `data/seen_items.json`. It needs `docs/data.json` added (this is already covered by `git add docs/`).

No changes to workflow schedule or triggers needed.

---

## Out of Scope

- Pagination (all 7 days render at once; manageable at ~1000 items)
- Map view for TPS coordinates
- Dark mode
- Filtering by service/type (search handles this)
- The old `docs/archive/YYYY-MM-DD.html` daily archives (left in place, not linked from new UI)
