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
    try:
        resp = requests.get(
            FEATURE_URL,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.Timeout:
        print("ERROR: Request timed out")
        return []
    except requests.RequestException as e:
        print(f"ERROR: Failed to fetch data: {e}")
        return []
    try:
        data = resp.json()
    except Exception as e:
        print(f"ERROR: Invalid JSON response: {e}")
        return []
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
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except Exception as e:
        print(f"WARNING: Could not read seen file ({e}), starting fresh")
        return set()


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
