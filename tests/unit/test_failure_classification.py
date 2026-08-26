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


# ── Systemic detection: signature must MATCH, not just share a feature ──────

def _fr(method, sig, feature="F", engine="chromium"):
    return {"method": method, "feature": feature, "errorSignature": sig,
            "engine": engine, "status": "FAIL"}


def test_systemic_groups_by_normalized_signature():
    # Three independent tests, signatures differ only in the duration number →
    # same normalized signature → one systemic failure.
    recs = [
        _fr("t1", "TimeoutError: waiting for 'Connect created' after 180000ms"),
        _fr("t2", "TimeoutError: waiting for 'Connect created' after 45000ms"),
        _fr("t3", "TimeoutError: waiting for 'Connect created' after 90000ms"),
    ]
    out = fc.detect_systemic(recs, min_tests=3)
    assert len(out) == 1
    assert out[0].affected_tests == 3


def test_systemic_excludes_feature_fallback_signatures():
    # 'feature=' is the pre-signature fallback — NEVER systemic (this is the
    # rule that stops systemic from decaying into feature concentration).
    recs = [_fr(f"t{i}", "feature=Flozic Entry Point") for i in range(5)]
    assert fc.detect_systemic(recs, min_tests=3) == []


def test_systemic_requires_independent_tests_not_repeats_of_one():
    recs = [_fr("same_test", "AssertionError: dashboard did not load in time") for _ in range(5)]
    assert fc.detect_systemic(recs, min_tests=3) == []   # 5 records, 1 method


def test_systemic_below_threshold_not_flagged():
    recs = [_fr("t1", "SomeError: a specific and long failure message"),
            _fr("t2", "SomeError: a specific and long failure message")]
    assert fc.detect_systemic(recs, min_tests=3) == []   # only 2 independent


def test_systemic_confidence_scales_with_independent_tests():
    recs = [_fr(f"t{i}", "AssertionError: dashboard did not load in time") for i in range(8)]
    out = fc.detect_systemic(recs, min_tests=3)
    assert out and out[0].confidence == "High" and out[0].affected_tests == 8
