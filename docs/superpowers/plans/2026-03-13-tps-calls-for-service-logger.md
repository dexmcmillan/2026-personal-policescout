# TPS Calls for Service Logger Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Poll the Toronto Police Service ArcGIS FeatureServer every hour via GitHub Actions, deduplicate by OBJECTID, and append new calls to a rolling NDJSON log at `data/tps_calls.ndjson`.

**Architecture:** A single standalone script `tps_calls.py` fetches the live snapshot from the TPS ArcGIS FeatureServer REST API, converts Unix ms timestamps to ISO 8601, deduplicates against a seen-OBJECTIDs set persisted in `data/tps_calls_seen.json`, and appends only new records to `data/tps_calls.ndjson`. A separate GitHub Actions workflow (`tps_calls.yml`) runs the script hourly and commits any new data.

**Tech Stack:** Python 3.12, `requests`, `uv`, GitHub Actions

---

## Chunk 1: Collector Script and Tests

### Task 1: Tests for the collector

**Files:**
- Create: `tests/test_tps_calls.py`

- [ ] **Step 1: Write failing tests for `parse_feature` and `load_seen` / `save_seen`**

```python
# tests/test_tps_calls.py
import json
import pytest
from pathlib import Path

# Import will fail until Task 2 — that's expected
from tps_calls import parse_feature, load_seen, save_seen


def test_parse_feature_converts_timestamp():
    raw = {
        "OBJECTID": 42,
        "OCCURRENCE_TIME": 1773413150000,   # Unix ms
        "DIVISION": "D51",
        "CALL_TYPE_CODE": "ASS",
        "CALL_TYPE": "ASSAULT",
        "CROSS_STREETS": "GOULD ST - MUTUAL ST",
        "LATITUDE": 43.660,
        "LONGITUDE": -79.377,
        "OCCURRENCE_TIME_AGOL": 1773413150000,
    }
    result = parse_feature(raw)
    assert result["objectid"] == 42
    assert result["occurred_at"].startswith("2026-")   # ISO 8601
    assert result["call_type"] == "ASSAULT"
    assert result["call_type_code"] == "ASS"
    assert result["division"] == "D51"
    assert result["cross_streets"] == "GOULD ST - MUTUAL ST"
    assert result["latitude"] == 43.660
    assert result["longitude"] == -79.377


def test_parse_feature_null_timestamp():
    raw = {
        "OBJECTID": 7,
        "OCCURRENCE_TIME": None,
        "DIVISION": "HP",
        "CALL_TYPE_CODE": "PIACC",
        "CALL_TYPE": "PERSONAL INJURY COLLISION",
        "CROSS_STREETS": "MORNINGSIDE - MCNICOLL",
        "LATITUDE": 43.827,
        "LONGITUDE": -79.234,
        "OCCURRENCE_TIME_AGOL": None,
    }
    result = parse_feature(raw)
    assert result["occurred_at"] is None


def test_load_seen_missing_file(tmp_path):
    p = tmp_path / "seen.json"
    assert load_seen(p) == set()


def test_save_and_load_seen(tmp_path):
    p = tmp_path / "seen.json"
    save_seen({1, 2, 3}, p)
    assert load_seen(p) == {1, 2, 3}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_tps_calls.py -v
```

Expected: `ImportError: cannot import name 'parse_feature' from 'tps_calls'`

---

### Task 2: Implement `tps_calls.py`

**Files:**
- Create: `tps_calls.py`

- [ ] **Step 3: Write the implementation**

```python
"""
tps_calls.py — Hourly collector for TPS Calls for Service.

Fetches the live ArcGIS FeatureServer snapshot (last ~4 hours, ~70 records),
deduplicates by OBJECTID against data/tps_calls_seen.json, and appends new
records as NDJSON lines to data/tps_calls.ndjson.

ArcGIS endpoint (public, no auth required):
  https://services.arcgis.com/S9th0jAJ7bqgIRjw/arcgis/rest/services/C4S_Public_NoGO/FeatureServer/0
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

FEATURE_URL = (
    "https://services.arcgis.com/S9th0jAJ7bqgIRjw/arcgis/rest/services"
    "/C4S_Public_NoGO/FeatureServer/0/query"
)
SEEN_FILE = Path(__file__).parent / "data" / "tps_calls_seen.json"
LOG_FILE = Path(__file__).parent / "data" / "tps_calls.ndjson"

USER_AGENT = (
    "Mozilla/5.0 (compatible; PolicePressScout/1.0; "
    "+https://github.com/globeandmail)"
)


def fetch_features() -> list[dict]:
    """Fetch all current calls for service from the TPS FeatureServer."""
    params = {
        "where": "1=1",
        "outFields": (
            "OBJECTID,OCCURRENCE_TIME,DIVISION,"
            "CALL_TYPE_CODE,CALL_TYPE,CROSS_STREETS,"
            "LATITUDE,LONGITUDE"
        ),
        "orderByFields": "OCCURRENCE_TIME DESC",
        "resultRecordCount": 2000,
        "f": "json",
    }
    resp = requests.get(
        FEATURE_URL,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=20,
        verify=False,
    )
    resp.raise_for_status()
    data = resp.json()
    return [feat["attributes"] for feat in data.get("features", [])]


def parse_feature(attrs: dict) -> dict:
    """Convert raw ArcGIS attributes to a clean record."""
    ts = attrs.get("OCCURRENCE_TIME")
    if ts is not None:
        occurred_at = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
    else:
        occurred_at = None

    return {
        "objectid": attrs.get("OBJECTID"),
        "occurred_at": occurred_at,
        "division": attrs.get("DIVISION"),
        "call_type_code": attrs.get("CALL_TYPE_CODE"),
        "call_type": attrs.get("CALL_TYPE"),
        "cross_streets": attrs.get("CROSS_STREETS"),
        "latitude": attrs.get("LATITUDE"),
        "longitude": attrs.get("LONGITUDE"),
        "collected_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def load_seen(path: Path) -> set[int]:
    """Load the set of already-logged OBJECTIDs from disk."""
    if not path.exists():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")))


def save_seen(seen: set[int], path: Path) -> None:
    """Persist the seen OBJECTIDs set to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(seen)), encoding="utf-8")


def append_records(records: list[dict], log_path: Path) -> None:
    """Append records as NDJSON lines to the log file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> None:
    seen = load_seen(SEEN_FILE)
    print(f"Known OBJECTIDs: {len(seen)}")

    raw_features = fetch_features()
    print(f"Fetched: {len(raw_features)} features from API")

    new_records = []
    for attrs in raw_features:
        oid = attrs.get("OBJECTID")
        if oid is None or oid in seen:
            continue
        seen.add(oid)
        new_records.append(parse_feature(attrs))

    if new_records:
        append_records(new_records, LOG_FILE)
        save_seen(seen, SEEN_FILE)
        print(f"Appended: {len(new_records)} new records → {LOG_FILE}")
    else:
        print("No new records.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_tps_calls.py -v
```

Expected: 4 tests PASS

- [ ] **Step 5: Smoke-test against the live API**

```bash
uv run python tps_calls.py
```

Expected output like:
```
Known OBJECTIDs: 0
Fetched: 71 features from API
Appended: 71 new records → data/tps_calls.ndjson
```

Verify the file was created:
```bash
head -2 data/tps_calls.ndjson | python -m json.tool
```

Each line should be valid JSON with keys: `objectid`, `occurred_at`, `division`, `call_type`, `cross_streets`, `latitude`, `longitude`, `collected_at`.

- [ ] **Step 6: Commit**

```bash
git add tps_calls.py tests/test_tps_calls.py data/tps_calls.ndjson data/tps_calls_seen.json
git commit -m "Add TPS calls for service hourly collector"
```

---

## Chunk 2: GitHub Actions Workflow

### Task 3: Hourly workflow

**Files:**
- Create: `.github/workflows/tps_calls.yml`

- [ ] **Step 7: Write the workflow**

```yaml
# .github/workflows/tps_calls.yml
name: TPS Calls for Service

on:
  schedule:
    # Every hour, every day
    - cron: '0 * * * *'
  workflow_dispatch:

jobs:
  collect:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Set up Python
        run: uv python install

      - name: Install dependencies
        run: uv sync

      - name: Collect TPS calls
        run: uv run python tps_calls.py

      - name: Commit new data
        run: |
          git config --local user.email "github-actions[bot]@users.noreply.github.com"
          git config --local user.name "github-actions[bot]"
          git add data/tps_calls.ndjson data/tps_calls_seen.json
          git diff --staged --quiet || git commit -m "TPS calls snapshot $(date -u +%Y-%m-%dT%H:%MZ)"
          git push
```

- [ ] **Step 8: Add `data/tps_calls.ndjson` to git tracking**

The log file needs to be committed each run. Add it explicitly if it's in `.gitignore`:

```bash
git check-ignore data/tps_calls.ndjson && echo "IGNORED — fix .gitignore" || echo "OK"
```

If ignored, add an exception to `.gitignore`:
```
!data/tps_calls.ndjson
!data/tps_calls_seen.json
```

- [ ] **Step 9: Commit and push the workflow**

```bash
git add .github/workflows/tps_calls.yml
git commit -m "Add hourly GitHub Actions workflow for TPS calls collector"
git push
```

- [ ] **Step 10: Verify via manual trigger**

In GitHub Actions UI, run "TPS Calls for Service" manually (workflow_dispatch). Confirm:
- Job completes successfully
- A new commit appears with updated `data/tps_calls.ndjson`

---

## Notes

**NDJSON format** (newline-delimited JSON) is chosen over a single JSON array because:
- New records can be appended with a simple file open/append — no need to parse the entire file
- Works well with `jq`, `pandas.read_json(..., lines=True)`, and command-line tools
- File grows incrementally without rewriting

**Seen OBJECTIDs pruning:** OBJECTIDs are not globally unique across time — TPS reuses them as the live dataset rotates. The seen set therefore only needs to track recent OBJECTIDs to avoid duplicates within overlapping poll windows. Currently the seen set grows unboundedly (safe for years at ~70 IDs/hr), but could be pruned to a rolling window if needed.

**Rate limiting:** The ArcGIS public FeatureServer has no documented rate limit. One request per hour is well within any reasonable threshold.
