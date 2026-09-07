"""
Run-status model + EMPTY-run exclusion (P0 hygiene, Tasks 2+3).

A session that executed 0 tests (unit-only, aborted, collection-only) is an
audit-trail entry, NOT a test result. It must be labelled (runStatus=EMPTY),
excluded from the trend display, and excluded from the flake/recurring window —
the audit found 28 of 30 trend entries were 0/0 junk inflating the '/30'.
"""
from __future__ import annotations

import json
import types

import utils.failure_clustering as fc
from utils.dashboard_builder import _render_trend_rows


def _snap(dirpath, name, tests, run_status=None):
    payload = {"timestamp": 1, "tests": tests}
    if run_status:
        payload["runStatus"] = run_status
    (dirpath / f"{name}.json").write_text(json.dumps(payload))


def test_load_history_skips_empty_snapshots(tmp_path):
    _snap(tmp_path, "20260103_000000", [], run_status="EMPTY")           # newest: empty
    _snap(tmp_path, "20260102_000000", [])                                # legacy empty (no runStatus)
    _snap(tmp_path, "20260101_000000",
          [{"status": "FAIL", "method": "test_x", "feature": "F"}])
    hist = fc.load_history(snapshot_dir=tmp_path, last_n=30)
    assert len(hist) == 1                       # only the COMPLETED run counts
    assert hist[0][0] == "20260101_000000"


def test_window_denominator_counts_completed_only(tmp_path):
    # 1 real failing run + 5 empty sessions: the badge maths must see window=1,
    # not 6 — a failure seen in its only real run is 1/1, not 1/6.
    _snap(tmp_path, "20260106_000000",
          [{"status": "FAIL", "method": "test_x", "feature": "F"}])
    for i in range(5):
        _snap(tmp_path, f"2026010{i}_000000", [], run_status="EMPTY")
    clusters = fc.build_clusters(snapshot_dir=tmp_path, last_n=30)
    assert clusters[0].window_size == 1
    assert clusters[0].occurrences_in_history == 1


def test_method_stats_ignore_empty_sessions(tmp_path):
    _snap(tmp_path, "20260102_000000", [], run_status="EMPTY")
    _snap(tmp_path, "20260101_000000",
          [{"status": "PASS", "method": "test_x", "feature": "F"}])
    stats = fc._method_run_stats(tmp_path, last_n=30)
    assert stats["test_x"] == {"seen": 1, "passed": 1, "failed": 0}


def test_trend_rows_hide_empty_runs():
    trend = [
        {"label": "real", "run_status": "COMPLETED", "total": 312, "passed": 268,
         "failed": 44, "pass_rate": 85.9, "status": "AT_RISK"},
        {"label": "junk", "run_status": "EMPTY", "total": 0, "passed": 0,
         "failed": 0, "pass_rate": 0, "status": "WARNING"},
        {"label": "legacy-junk", "total": 0, "passed": 0, "failed": 0,
         "pass_rate": 0, "status": "WARNING"},          # pre-run_status row
    ]
    html = _render_trend_rows(trend)
    assert "real" in html
    assert "junk" not in html and "legacy-junk" not in html


def test_append_trend_and_snapshot_stamp_run_status(tmp_path, monkeypatch):
    import utils.dashboard_builder as db
    import utils.snapshot_writer as sw
    monkeypatch.setattr(db, "TREND_JSON", tmp_path / "trend-history.json")
    decision = types.SimpleNamespace(status=types.SimpleNamespace(value="WARNING"))
    db._append_trend({"total": 0, "passed": 0, "failed": 0, "pass_rate": 0.0},
                     decision, 1_700_000_000_000)
    row = json.loads((tmp_path / "trend-history.json").read_text())[0]
    assert row["run_status"] == "EMPTY"

    monkeypatch.setattr(sw, "SNAPSHOT_PATH", tmp_path / "snap.json")
    sw.write_snapshot(tmp_path / "snap.json")   # session has 0 records here
    assert json.loads((tmp_path / "snap.json").read_text())["runStatus"] == "EMPTY"
