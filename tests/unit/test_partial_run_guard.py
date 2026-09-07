"""
Partial-run clobber hardening.

Three guards that make the combined report's engine view permanently coherent:
  1. Sidecar routing — only FULL-suite runs write report-data.json; partial
     runs write report-data-partial.json, so a subset run can never displace
     the authoritative full-run record.
  2. Run anchoring — load_engine picks the snapshot AND trend row belonging to
     the full sidecar's run, so tiles/failures/scores describe ONE run even
     when a later subset run archived a newer snapshot and trend row.
  3. Mobile-summary guard — an uninstrumented (layout-only subset) session
     never overwrites an instrumented full-mobile summary.
"""
from __future__ import annotations

import json
from datetime import datetime

import utils.combined_report_builder as cb
import utils.mobile_report_builder as mrb
from utils.report_data_sidecar import build_sidecar, write_sidecar


class _Scores:
    overall = 80; product_health = 77; infra_health = 92
    framework_health = 100; mobile_health = 62; scoring_version = 3


def _payload(total, started_ms):
    return build_sidecar(engine="chromium", run_started_ms=started_ms,
                         scores=_Scores(), decision="AT_RISK",
                         stats={"total": total, "passed": total - 2, "failed": 2})


# ── Guard 1: sidecar routing ────────────────────────────────────────────────

def test_full_run_owns_report_data_json(tmp_path):
    p = write_sidecar(tmp_path, _payload(312, 1000))
    assert p.name == "report-data.json"


def test_partial_run_writes_partial_file_and_never_displaces_full(tmp_path):
    write_sidecar(tmp_path, _payload(312, 1000))            # full run first
    p2 = write_sidecar(tmp_path, _payload(7, 2000))         # then a subset run
    assert p2.name == "report-data-partial.json"
    full = json.loads((tmp_path / "report-data.json").read_text())
    assert full["run_started_at"] == 1000                   # untouched


# ── Guard 2: run anchoring in load_engine ───────────────────────────────────

def _snap(dirpath, started_ms, tests):
    stem = datetime.fromtimestamp(started_ms / 1000).strftime("%Y%m%d_%H%M%S")
    (dirpath / f"{stem}.json").write_text(json.dumps({"timestamp": started_ms, "tests": tests}))


def test_load_engine_anchors_to_full_run_not_latest_subset(tmp_path):
    snaps = tmp_path / "snapshots"; snaps.mkdir()
    t_full, t_subset = 1_700_000_000_000, 1_700_009_999_000
    full_tests = [{"class": "A", "method": f"t{i}", "feature": "F", "status": "PASS",
                   "category": "FULL", "login": "Auth", "duration": 1} for i in range(300)]
    full_tests += [{"class": "A", "method": "tf", "feature": "F", "status": "FAIL",
                    "category": "FULL", "login": "Auth", "duration": 1}]
    _snap(snaps, t_full, full_tests)
    _snap(snaps, t_subset, [{"class": "B", "method": "sub", "feature": "M", "status": "PASS",
                             "category": "REGRESSION", "login": "Guest", "duration": 1}])
    write_sidecar(tmp_path, _payload(301, t_full))          # full sidecar anchors t_full
    (tmp_path / "trend-history.json").write_text(json.dumps([
        {"ts": t_full, "label": "full", "run_status": "COMPLETED", "total": 301,
         "passed": 300, "failed": 1, "pass_rate": 99.7, "status": "AT_RISK",
         "overall": 80, "mobile": 62, "scoring_version": 3},
        {"ts": t_subset, "label": "subset", "run_status": "COMPLETED", "total": 1,
         "passed": 1, "failed": 0, "pass_rate": 100.0, "status": "READY",
         "overall": 100, "mobile": None, "scoring_version": 3},
    ]))
    ed = cb.load_engine("chromium", base=str(tmp_path))
    # Records, auth row and sidecar all belong to the FULL run — not the newer subset.
    assert len(ed.records) == 301
    assert ed.auth["ts"] == t_full and ed.auth["overall"] == 80
    assert ed.sidecar["run_started_at"] == t_full


def test_load_engine_falls_back_without_full_sidecar(tmp_path):
    snaps = tmp_path / "snapshots"; snaps.mkdir()
    _snap(snaps, 1_700_000_000_000, [{"class": "B", "method": "sub", "feature": "M",
                                      "status": "PASS", "category": "REGRESSION",
                                      "login": "Guest", "duration": 1}])
    write_sidecar(tmp_path, _payload(7, 1_700_000_000_000))  # only a partial exists
    ed = cb.load_engine("chromium", base=str(tmp_path))
    assert ed.present and ed.sidecar["population"]["total"] == 7   # partial fallback


# ── Guard 3: mobile-summary clobber guard ───────────────────────────────────

def test_uninstrumented_session_keeps_instrumented_summary(tmp_path, monkeypatch):
    c = mrb.MobileReportCollector(engine="chromium")
    path = tmp_path / "mobile-summary.json"
    path.write_text(json.dumps({"schema_version": 1, "engine": "chromium",
                                "check_stats": {"attempted": 216, "executed": 216,
                                                "na_structural": 0},
                                "findings": [{"severity": "minor"}] * 5}))
    monkeypatch.setattr(c, "summary_path", lambda base="x": str(path))
    monkeypatch.setattr(mrb, "session_check_stats", lambda: {})   # uninstrumented session
    c.write_summary()
    kept = json.loads(path.read_text())
    assert kept["check_stats"] is not None and len(kept["findings"]) == 5


def test_instrumented_session_refreshes_summary(tmp_path, monkeypatch):
    c = mrb.MobileReportCollector(engine="chromium")
    path = tmp_path / "mobile-summary.json"
    path.write_text(json.dumps({"schema_version": 1, "check_stats": {"attempted": 216},
                                "findings": [{"severity": "minor"}] * 5}))
    monkeypatch.setattr(c, "summary_path", lambda base="x": str(path))
    monkeypatch.setattr(mrb, "session_check_stats",
                        lambda: {"chromium": {"attempted": 10, "executed": 10,
                                              "na_structural": 0}})
    c.write_summary()
    fresh = json.loads(path.read_text())
    assert fresh["check_stats"]["attempted"] == 10               # instrumented run wins
    assert fresh["findings"] == []                                # this session's findings
