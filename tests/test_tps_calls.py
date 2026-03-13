# tests/test_tps_calls.py
import json
import pytest
from pathlib import Path

from tps_calls import parse_feature, load_seen, save_seen, append_records


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


def test_append_records_creates_valid_ndjson(tmp_path):
    from tps_calls import append_records
    log = tmp_path / "test.ndjson"
    records = [{"id": 1, "call_type": "ASSAULT"}, {"id": 2, "call_type": "FRAUD"}]
    append_records(records, log)
    lines = log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == 1
    assert json.loads(lines[1])["id"] == 2


def test_load_seen_handles_corrupt_file(tmp_path):
    p = tmp_path / "seen.json"
    p.write_text("not valid json", encoding="utf-8")
    # Should not raise, should return empty set
    result = load_seen(p)
    assert result == set()
