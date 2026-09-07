"""
Unit tests for utils/mcp_tools — the MCP v1 logic layer.

The MCP protocol wrapper (flowguard_mcp.py) is a thin adapter; what needs
testing is the logic layer it exposes. Fixtures build a fake reports/trend
tree (sidecar + snapshot + mobile-summary per engine) and every tool reads
from it via the `base` override — no server, no live data.
"""
from __future__ import annotations

import json

import utils.mcp_tools as mt


def _mk_base(tmp_path):
    """Minimal two-engine reports tree: chromium full run with 2 fails
    (shared signature) + webkit run failing the same tests."""
    for eng, root in (("chromium", tmp_path), ("webkit", tmp_path / "webkit")):
        (root / "snapshots").mkdir(parents=True)
        tests = [
            {"class": "TestA", "method": "test_a", "feature": "Connect", "status": "FAIL",
             "category": "FULL", "login": "Auth", "duration": 5,
             "errorSignature": "TimeoutError: Timeout 180000ms exceeded waiting for locator('.copilot')"},
            {"class": "TestB", "method": "test_b", "feature": "Connect", "status": "FAIL",
             "category": "FULL", "login": "Auth", "duration": 5,
             "errorSignature": "TimeoutError: Timeout 90000ms exceeded waiting for locator('.copilot')"},
            {"class": "TestC", "method": "test_c", "feature": "Marketing", "status": "PASS",
             "category": "FULL", "login": "Guest", "duration": 1},
        ]
        (root / "snapshots" / "20260901_100000.json").write_text(
            json.dumps({"timestamp": 1, "tests": tests}))
        (root / "report-data.json").write_text(json.dumps({
            "schema_version": 1, "engine": eng, "run_finished_at": 123,
            "population": {"suite": "full", "total": 3, "executed": 3},
            "health": {"overall": 80, "product": 67, "infrastructure": 96,
                       "framework": 100, "mobile": 62, "scoring_version": 3},
            "release": {"decision": "AT_RISK"},
            "git": {"commit": "abc123", "branch": "x", "dirty": False},
            "environment": {"browser": eng},
            "login_routes": [], "js_error_clusters": [],
        }))
        (root / "mobile-summary.json").write_text(json.dumps({
            "engine": eng, "generated_at": "2026-09-01",
            "check_stats": {"attempted": 10, "executed": 10, "na_structural": 0},
            "findings": [
                {"severity": "minor", "category": "tap_target", "device": "iPhone SE",
                 "page": "pricing", "message": "BUTTON 'X' is 20x20px", "selector": "#buy"},
                {"severity": "info", "category": "font_size", "device": "Pixel 5",
                 "page": "home", "message": "small text", "selector": None},
            ],
        }))
        # total>200 so _is_full() reads these as FULL-suite runs (eligibility Full).
        (root / "trend-history.json").write_text(json.dumps([
            {"ts": 1, "label": "run", "total": 312, "passed": 310, "failed": 2,
             "overall": 80, "mobile": 62, "status": "AT_RISK", "scoring_version": 3},
        ]))
    return str(tmp_path)


def test_release_status(tmp_path):
    out = mt.get_release_status(_mk_base(tmp_path))
    assert out["release"] == "AT_RISK"
    assert out["comparison_eligibility"] == "Full"
    assert len(out["engines"]) == 2
    assert out["engines"][0]["health"]["product"] == 67


def test_failures_and_engine_filter(tmp_path):
    base = _mk_base(tmp_path)
    out = mt.get_failures(base)
    assert out["failed_count"] == 4                       # 2 per engine
    only = mt.get_failures(base, engine="webkit")
    assert only["failed_count"] == 2
    assert all(f["engine"] == "webkit" for f in only["failures"])
    assert only["failures"][0]["failure_type"].startswith("Timeout")


def test_systemic_clusters_group_by_signature(tmp_path):
    out = mt.get_systemic_clusters(_mk_base(tmp_path))
    # test_a + test_b share the normalized signature (durations stripped) →
    # one cluster spanning both engines, 2 independent tests... but min_tests=3
    # counts DISTINCT methods: TestA::test_a, TestB::test_b = 2 < 3 → none? No:
    # methods are distinct per class, 2 methods only → below threshold.
    assert out["signed_failures"] == 4
    # 2 independent methods < min_tests=3 → honestly no systemic cluster.
    assert out["clusters"] == []


def test_failure_evidence_lookup(tmp_path):
    out = mt.get_failure_evidence("test_a", base=_mk_base(tmp_path))
    assert out["matches"] == 2                            # one per engine
    ev = out["evidence"][0]
    assert ev["test"] == "TestA::test_a"
    assert ev["error_signature"].startswith("TimeoutError")


def test_mobile_findings_filters(tmp_path):
    base = _mk_base(tmp_path)
    out = mt.get_mobile_findings(base, engine="chromium", severity="minor")
    assert out["total_matching"] == 1
    f = out["findings"][0]
    assert f["check"] == "tap_target" and f["selector"] == "#buy"
    assert "44" in f["recommended"]
    assert mt.get_mobile_findings(base, engine="chromium", check="font_size")["total_matching"] == 1


def test_compare_engines_shared(tmp_path):
    out = mt.compare_engines(_mk_base(tmp_path))
    assert out["eligibility"] == "Full"
    assert out["co_executed"] == 3
    assert out["shared_failures"] == 2
    assert out["chromium_only_defects"] == 0 and out["not_comparable"] == 0


def test_run_provenance(tmp_path):
    out = mt.get_run_provenance(_mk_base(tmp_path))
    assert len(out["engines"]) == 2
    assert out["engines"][0]["git_commit"] == "abc123"
    assert out["engines"][0]["suite"] == "full"
