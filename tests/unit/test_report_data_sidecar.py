"""
Unit tests for utils/report_data_sidecar — the versioned, authoritative
per-engine run record that backs the combined cross-engine report.

Locks the contract that matters for trustworthiness:
  * schema is versioned and shaped as documented,
  * authoritative-only: None scores stay None (never invented),
  * EMPTY vs COMPLETED execution status,
  * in-session-only structures (login routes, JS clusters) serialize safely,
  * git/env provenance is always present (values may be None off a repo/CI).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import utils.report_data_sidecar as sc


@dataclass
class _Scores:
    overall: int = 72
    product_health: int = 59
    infra_health: int = 78
    framework_health: int = 100
    mobile_health: int = 62
    scoring_version: int = 3


def test_classify_execution_empty_vs_completed():
    assert sc.classify_execution(0) == "EMPTY"
    assert sc.classify_execution(1) == "COMPLETED"
    assert sc.classify_execution(312) == "COMPLETED"


def test_build_sidecar_shape_and_mapping():
    p = sc.build_sidecar(
        engine="chromium", run_started_ms=1756111948000, scores=_Scores(),
        decision="BLOCKED", stats={"total": 312, "passed": 249, "failed": 63},
    )
    assert p["schema_version"] == sc.SCHEMA_VERSION == 1
    assert p["engine"] == "chromium"
    assert p["run_id"] == "chromium-1756111948000"
    assert p["run_started_at"] == 1756111948000
    assert p["execution_status"] == "COMPLETED"
    assert p["population"] == {"suite": "full", "total": 312, "executed": 312}
    # LayeredScores field names are mapped to the schema's health keys.
    assert p["health"] == {
        "overall": 72, "product": 59, "infrastructure": 78,
        "framework": 100, "mobile": 62, "scoring_version": 3,
    }
    assert p["release"] == {"decision": "BLOCKED"}
    assert p["stats"] == {"total": 312, "passed": 249, "failed": 63}
    # Provenance blocks always present (keys, not values).
    assert set(p["git"]) == {"commit", "branch", "dirty"}
    assert set(p["environment"]) == {"os", "python", "playwright", "browser", "browser_version"}
    assert p["environment"]["browser"] == "chromium"


def test_authoritative_only_none_scores_stay_none():
    """No scores object → health values are None, NEVER fabricated."""
    p = sc.build_sidecar(
        engine="webkit", run_started_ms=1, scores=None,
        decision=None, stats={"total": 0, "passed": 0, "failed": 0},
    )
    assert p["execution_status"] == "EMPTY"
    assert p["population"]["suite"] == "empty"
    assert all(v is None for v in p["health"].values())
    assert p["release"] == {"decision": None}


def test_suite_inference():
    full = sc.build_sidecar(engine="chromium", run_started_ms=1, scores=None,
                            decision=None, stats={"total": 312})
    mobile = sc.build_sidecar(engine="webkit", run_started_ms=1, scores=None,
                              decision=None, stats={"total": 182})
    assert full["population"]["suite"] == "full"
    assert mobile["population"]["suite"] == "mobile"


def test_decision_object_with_status_value():
    class _Status:
        value = "READY"

    class _Decision:
        status = _Status()

    p = sc.build_sidecar(engine="webkit", run_started_ms=1, scores=None,
                         decision=_Decision(), stats={"total": 5})
    assert p["release"]["decision"] == "READY"


def test_serialize_login_routes_tuples_and_malformed():
    routes = [("test_a", "flozic-authv2", "https://x"), ("bad",), None]
    out = sc._serialize_login_routes(routes)
    assert out == [{"test": "test_a", "route": "flozic-authv2", "url": "https://x"}]


def test_serialize_clusters_dataclass_and_plain():
    @dataclass
    class _Cluster:
        title: str = "boom"
        domain: str = "PRODUCT"
        count: int = 3

    class _Plain:
        title = "plain"
        severity = "HIGH"
        count = 1

    out = sc._serialize_clusters([_Cluster(), _Plain()])
    assert out[0]["title"] == "boom" and out[0]["domain"] == "PRODUCT"
    assert out[1]["title"] == "plain" and out[1]["severity"] == "HIGH"
    assert sc._serialize_clusters([]) == []
    assert sc._serialize_clusters(None) == []


def test_write_and_read_roundtrip(tmp_path):
    p = sc.build_sidecar(engine="chromium", run_started_ms=1, scores=_Scores(),
                         decision="BLOCKED", stats={"total": 312})
    path = sc.write_sidecar(tmp_path, p)
    assert path is not None and path.name == "report-data.json"
    back = sc.read_sidecar(tmp_path)
    assert back == p
    # File is valid JSON on disk.
    assert json.loads(path.read_text())["schema_version"] == 1


def test_read_sidecar_missing_is_none(tmp_path):
    assert sc.read_sidecar(tmp_path) is None
