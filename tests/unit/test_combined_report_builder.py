"""
Unit tests for utils/combined_report_builder — the read-only presentation
layer for the cross-engine Hybrid report.

Locks the trust-critical behaviours the reviewer insisted on:
  * a test the other engine NEVER RAN is "not comparable", never an
    engine-specific defect and never a pass,
  * authoritative scores come from trend/sidecar and domains are ⏳ pending
    (never recomputed) when the sidecar is absent,
  * a partial-scope engine reads "PASSING — EXECUTED SCOPE", not bare READY,
  * overall release = worst engine decision,
  * the combined report is also written as index.html (landing page).
"""
from __future__ import annotations

import utils.combined_report_builder as cb
from utils.snapshot_writer import TestRecord as _TR  # aliased: 'Test*' would be collected


def _rec(clazz: str, method: str, feature: str, status: str) -> _TR:
    return _TR(
        category="FULL", login="Auth", feature=feature, clazz=clazz,
        method=method, status=status, duration_ms=100,
    )


def _engine(name, records=(), auth=None, mobile=None, sidecar=None):
    return cb.EngineData(
        engine=name, present=True, run_ms=1756111948000,
        records=list(records), auth=auth, mobile=mobile, sidecar=sidecar,
    )


# ── Engine comparison: the four states, computed from data ──────────────────

def _comparison_engines():
    chromium = _engine(
        "chromium",
        records=[
            _rec("A", "a1", "F1", "FAIL"),   # shared fail
            _rec("B", "b1", "F2", "FAIL"),   # chromium-only defect (wk ran+passed)
            _rec("C", "c1", "F3", "FAIL"),   # chromium fail, wk never ran → not comparable
            _rec("D", "d1", "F4", "PASS"),
        ],
        auth={"overall": 72, "mobile": 62, "status": "BLOCKED", "total": 312, "failed": 3},
    )
    webkit = _engine(
        "webkit",
        records=[
            _rec("A", "a1", "F1", "FAIL"),   # shared fail
            _rec("B", "b1", "F2", "PASS"),   # makes chromium B a real chromium-only defect
            _rec("E", "e1", "F5", "FAIL"),   # webkit fail, chromium never ran → not comparable
            _rec("D", "d1", "F4", "PASS"),
        ],
        auth={"overall": 94, "mobile": 59, "status": "READY", "total": 182, "failed": 1},
    )
    return chromium, webkit


def test_engine_comparison_four_states():
    chromium, webkit = _comparison_engines()
    html = cb._render_engine_comparison(chromium, webkit)
    assert "SHARED — both ran, both failed (1)" in html
    assert "CHROMIUM-ONLY defect (1)" in html
    assert "WEBKIT-ONLY defect (0)" in html
    assert "NOT COMPARABLE — Chromium failed, WebKit never ran it (1)" in html
    assert "NOT COMPARABLE — WebKit failed, Chromium never ran it (1)" in html


def test_not_run_is_never_an_engine_specific_defect():
    """C::c1 failed on Chromium but WebKit never ran it — it must appear in the
    NOT-COMPARABLE bucket, never in the chromium-only-DEFECT bucket."""
    chromium, webkit = _comparison_engines()
    html = cb._render_engine_comparison(chromium, webkit)
    defect_block = html.split("CHROMIUM-ONLY defect")[1].split("WEBKIT-ONLY")[0]
    assert "C::c1" not in defect_block          # not mislabelled a defect
    notrun_block = html.split("Chromium failed, WebKit never ran it")[1]
    assert "C::c1" in notrun_block              # correctly not-comparable


def test_comparison_summary_counts():
    chromium, webkit = _comparison_engines()
    html = cb._render_engine_comparison(chromium, webkit)
    # comparable = tests run on BOTH: A, B, D → 3
    assert "<span class='scv'>3</span><div class='sl'>Tests comparable (ran on both)</div>" in html


# ── Coverage matrix ─────────────────────────────────────────────────────────

def test_comparison_coverage_matrix():
    chromium = _engine("chromium",
                       mobile={"check_stats": {"attempted": 216, "executed": 216, "na_structural": 0}},
                       auth={"total": 312})
    webkit = _engine("webkit",
                     mobile={"check_stats": {"attempted": 216, "executed": 144, "na_structural": 72}},
                     auth={"total": 182})
    html = cb._render_comparison_coverage(chromium, webkit)
    assert "Comparable mobile (executed on both)" in html
    assert ">144<" in html                       # min(216,144) comparable
    # webkit is not full-suite → its Full-regression cell is a dash, not a number
    assert "Full regression suite" in html


# ── Executive summary: authoritative + scope-qualified + eligibility ────────

def test_exec_summary_release_and_scope_and_eligibility():
    chromium, webkit = _comparison_engines()
    html = cb._render_exec_summary([chromium, webkit])
    assert "RELEASE: BLOCKED" in html            # worst engine decision
    assert "PASSING — EXECUTED SCOPE" not in html or "ISSUES — EXECUTED SCOPE" in html
    # webkit here has failed=1 → ISSUES — EXECUTED SCOPE (still scope-qualified, not READY)
    assert "ISSUES — EXECUTED SCOPE" in html
    assert "Comparison eligibility: Partial" in html
    assert "not directly comparable" in html


def test_engine_status_label():
    assert cb._engine_status_label({"status": "BLOCKED", "total": 312}) == ("BLOCKED", "blocked")
    assert cb._engine_status_label({"status": "READY", "total": 182, "failed": 0}) == (
        "PASSING — EXECUTED SCOPE", "scope")
    assert cb._engine_status_label({"status": "READY", "total": 182, "failed": 3}) == (
        "ISSUES — EXECUTED SCOPE", "scope")


def test_overall_release_is_worst():
    a = _engine("chromium", auth={"status": "BLOCKED"})
    b = _engine("webkit", auth={"status": "READY"})
    assert cb._overall_release([a, b]) == "BLOCKED"
    assert cb._overall_release([b]) == "READY"


def test_eligibility_partial_when_populations_differ():
    full = _engine("chromium", auth={"total": 312})
    mobile = _engine("webkit", auth={"total": 182})
    assert cb._eligibility([full, mobile]) == "Partial"
    assert cb._eligibility([full, _engine("firefox", auth={"total": 400})]) == "Full"


# ── Engine block: authoritative-or-pending, never recomputed ────────────────

def test_engine_block_domains_pending_without_sidecar():
    ed = _engine("chromium",
                 records=[_rec("A", "a1", "F1", "PASS")],
                 auth={"overall": 72},
                 mobile={"findings": [], "check_stats": {"attempted": 1, "executed": 1, "na_structural": 0}})
    html = cb._render_engine_block(ed)
    assert "pending sidecar" in html
    # JS errors pending too (no sidecar)
    assert "in-session memory" in html or "not yet persisted" in html


def test_engine_block_domains_from_sidecar():
    ed = _engine("webkit",
                 records=[_rec("A", "a1", "F1", "PASS")],
                 auth={"overall": 94},
                 mobile={"findings": [], "check_stats": {"attempted": 1, "executed": 1, "na_structural": 0}},
                 sidecar={"health": {"product": 100, "infrastructure": 100, "framework": 100},
                          "js_error_clusters": []})
    html = cb._render_engine_block(ed)
    # Domains render as health cards (old-dashboard style), values from the sidecar.
    assert "Product Health" in html and "Infra Health" in html and "Framework Health" in html
    assert html.count(">100<") >= 3   # the three domain values as big numbers
    assert "hcard" in html            # rich card presentation
    assert "No JS error clusters recorded" in html   # empty list = real result, not pending


# ── Landing page: build() also writes index.html ────────────────────────────

def test_build_writes_index_html(tmp_path):
    out = tmp_path / "combined-report.html"
    cb.build(base=str(tmp_path), out=out)
    index = tmp_path / "index.html"
    assert out.exists() and index.exists()
    assert out.read_text() == index.read_text()
    assert "Combined Cross-Engine Report" in index.read_text()


def test_load_engine_skips_empty_snapshot(tmp_path):
    """A unit-run teardown archives an EMPTY snapshot; the newest file on disk
    must not wipe the engine's real data. load_engine picks the latest
    NON-EMPTY snapshot."""
    import json
    import os
    snaps = tmp_path / "snapshots"
    snaps.mkdir()
    (snaps / "20260101_000000.json").write_text(json.dumps({
        "timestamp": 111,
        "tests": [{"class": "A", "method": "a", "feature": "F", "status": "FAIL",
                   "category": "FULL", "login": "Auth", "duration": 1}],
    }), encoding="utf-8")
    empty = snaps / "20260102_000000.json"
    empty.write_text(json.dumps({"timestamp": 222, "tests": []}), encoding="utf-8")
    os.utime(empty, (9_999_999_999, 9_999_999_999))  # force it newest by mtime

    ed = cb.load_engine("chromium", base=str(tmp_path))
    assert ed.present is True
    assert len(ed.records) == 1 and ed.records[0].method == "a"
