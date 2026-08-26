"""
Unit tests for the minimum-observation failure classification in
utils/failure_clustering — the fix for "1/30 labelled Recurring".

Rule: 1 fail = NEW, 2 = OBSERVED, 3+ = RECURRING; 3+ with any passes = FLAKY;
3+ failing >=90% of runs seen = CONSISTENT_FAILURE.
"""
from __future__ import annotations

import json

import utils.failure_clustering as fc


def test_classify_failure_boundaries():
    assert fc.classify_failure(1, 30, 0) == "NEW"
    assert fc.classify_failure(2, 30, 0) == "OBSERVED"
    assert fc.classify_failure(3, 30, 0) == "RECURRING"       # 3 fails, no pass, 10% rate
    assert fc.classify_failure(3, 3, 2) == "FLAKY"            # mixed pass/fail
    assert fc.classify_failure(10, 10, 0) == "CONSISTENT_FAILURE"
    assert fc.classify_failure(9, 10, 0) == "CONSISTENT_FAILURE"   # exactly 90%
    assert fc.classify_failure(0, 0, 0) == "NEW"              # degenerate → NEW, never recurring


def test_flaky_beats_consistent_when_passes_exist():
    # 5 fails out of 6 runs would be >=90%-ish, but a PASS observed → FLAKY, not consistent.
    assert fc.classify_failure(5, 6, 1) == "FLAKY"


def _write_snapshot(dirpath, name, tests):
    (dirpath / f"{name}.json").write_text(json.dumps({"timestamp": 1, "tests": tests}), encoding="utf-8")


def test_build_clusters_labels_new_vs_flaky(tmp_path):
    snaps = tmp_path / "snapshots"
    snaps.mkdir()
    # test_new: fails once → NEW
    # test_flaky: fails in 3 runs, passes in 2 → FLAKY
    _write_snapshot(snaps, "20260101_000001", [
        {"method": "test_new", "feature": "F", "status": "FAIL", "category": "FULL"},
        {"method": "test_flaky", "feature": "G", "status": "FAIL", "category": "FULL"},
    ])
    _write_snapshot(snaps, "20260101_000002", [
        {"method": "test_flaky", "feature": "G", "status": "FAIL", "category": "FULL"},
    ])
    _write_snapshot(snaps, "20260101_000003", [
        {"method": "test_flaky", "feature": "G", "status": "FAIL", "category": "FULL"},
    ])
    _write_snapshot(snaps, "20260101_000004", [
        {"method": "test_flaky", "feature": "G", "status": "PASS", "category": "FULL"},
    ])
    _write_snapshot(snaps, "20260101_000005", [
        {"method": "test_flaky", "feature": "G", "status": "PASS", "category": "FULL"},
    ])
    clusters = {c.sample_method: c for c in fc.build_clusters(snapshot_dir=snaps, last_n=30)}
    assert clusters["test_new"].label == "NEW"
    assert clusters["test_new"].occurrences_in_history == 1
    assert clusters["test_flaky"].label == "FLAKY"
    assert clusters["test_flaky"].occurrences_in_history == 3
    assert clusters["test_flaky"].passes_in_history == 2
