"""
Per-browser snapshot isolation (P0 hygiene, Task 1).

Regression (audit-confirmed): conftest._retarget_report_paths repointed
snapshot_writer/dashboard paths per --browser engine but left
failure_clustering.SNAPSHOT_DIR at the chromium default — AND build_clusters
bound SNAPSHOT_DIR as a default argument (frozen at import), so even a
retargeted global was ignored. A WebKit run archived its snapshots under
reports/trend/webkit/snapshots but CLUSTERED against reports/trend/snapshots —
Chromium's history.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import conftest as root_conftest
import utils.dashboard_builder as db
import utils.failure_clustering as fc
import utils.snapshot_writer as sw


def _fake_config(engine: str):
    return types.SimpleNamespace(getoption=lambda opt: engine)


def _save_globals(monkeypatch):
    """Snapshot the module globals _retarget mutates so this test can't leak
    a per-engine path into sibling tests."""
    monkeypatch.setattr(fc, "SNAPSHOT_DIR", fc.SNAPSHOT_DIR)
    monkeypatch.setattr(sw, "SNAPSHOT_PATH", sw.SNAPSHOT_PATH)
    monkeypatch.setattr(db, "DASHBOARD_PATH", db.DASHBOARD_PATH)
    monkeypatch.setattr(db, "TREND_JSON", db.TREND_JSON)


def test_webkit_retarget_points_clustering_at_engine_archive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_globals(monkeypatch)
    root_conftest._retarget_report_paths(_fake_config("webkit"))
    assert fc.SNAPSHOT_DIR == Path("reports/trend/webkit/snapshots")
    # Reader must match where the teardown archives: <live snapshot>.parent/snapshots
    assert fc.SNAPSHOT_DIR == sw.SNAPSHOT_PATH.parent / "snapshots"


def test_chromium_retarget_keeps_default_archive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _save_globals(monkeypatch)
    root_conftest._retarget_report_paths(_fake_config("chromium"))
    assert fc.SNAPSHOT_DIR == Path("reports/trend/snapshots")


def test_build_clusters_resolves_snapshot_dir_at_call_time(tmp_path, monkeypatch):
    """End-to-end guard against the frozen-default-argument bug: after a webkit
    retarget, build_clusters() with NO snapshot_dir argument (exactly how the
    dashboard calls it) must read the WEBKIT archive."""
    monkeypatch.chdir(tmp_path)
    _save_globals(monkeypatch)
    chromium = tmp_path / "reports" / "trend" / "snapshots"
    webkit = tmp_path / "reports" / "trend" / "webkit" / "snapshots"
    chromium.mkdir(parents=True); webkit.mkdir(parents=True)
    (chromium / "20260101_000000.json").write_text(json.dumps(
        {"tests": [{"status": "FAIL", "method": "test_chromium_only", "feature": "c"}]}))
    (webkit / "20260101_000000.json").write_text(json.dumps(
        {"tests": [{"status": "FAIL", "method": "test_webkit_only", "feature": "w"}]}))

    root_conftest._retarget_report_paths(_fake_config("webkit"))
    methods = {c.sample_method for c in fc.build_clusters(last_n=30)}
    assert methods == {"test_webkit_only"}
