"""
FlowGuard MCP v1 — the LOGIC layer (read-only).

These are plain Python functions that answer questions from FlowGuard's
persisted run data (sidecars, snapshots, mobile summaries, trend history).
They know NOTHING about the MCP protocol — flowguard_mcp.py wraps them as MCP
tools. Keeping the layers separate means the logic is unit-testable without a
running server, and the protocol wrapper stays a page long.

Design rules (v1):
  * READ-ONLY. No tool triggers runs or mutates state.
  * Every answer carries PROVENANCE (engine + run timestamp) — data is only as
    fresh as the last run, and the caller must be able to see that.
  * Serve Python's persisted verdicts (release decision, scores, clusters) —
    never recompute or invent. Same "AI observes, Python decides" rule as the
    report.
  * Never expose credentials or .env content.
"""
from __future__ import annotations

from typing import Any

from utils.combined_report_builder import (
    EngineData,
    _collect_fail_records,
    _engine_status_label,
    _eligibility,
    _failure_type,
    _load_failure_evidence,
    _overall_release,
    _release_reason,
    load_engine,
)

_ENGINES = ("chromium", "webkit")


def _engines(base: str) -> list[EngineData]:
    return [load_engine(e, base) for e in _ENGINES]


def _prov(ed: EngineData) -> dict[str, Any]:
    return {"engine": ed.engine, "run": ed.run_label}


def get_release_status(base: str = "reports/trend") -> dict[str, Any]:
    """Release decision + health scores per engine, with provenance."""
    engines = _engines(base)
    overall = _overall_release(engines)
    per_engine = []
    for ed in engines:
        auth = ed.auth or {}
        label, _ = _engine_status_label(auth) if auth else ("no completed run", "")
        per_engine.append({
            **_prov(ed),
            "status": label,
            "suite": (ed.sidecar or {}).get("population", {}).get("suite"),
            "total": auth.get("total"), "failed": auth.get("failed"),
            "health": (ed.sidecar or {}).get("health"),
        })
    return {
        "release": overall,
        "reason": _release_reason(engines, overall),
        "comparison_eligibility": _eligibility(engines),
        "engines": per_engine,
    }


def get_failures(base: str = "reports/trend", engine: str | None = None) -> dict[str, Any]:
    """Failing tests from each engine's latest completed run: test, feature,
    normalized error signature, harness-fault flag, evidence folder."""
    out = []
    for ed in _engines(base):
        if engine and ed.engine != engine:
            continue
        for r in ed.records:
            if r.status != "FAIL":
                continue
            out.append({
                **_prov(ed),
                "test": f"{r.clazz}::{r.method}",
                "feature": r.feature,
                "error_signature": r.error_signature or None,
                "failure_type": _failure_type(r.error_signature) if r.error_signature else None,
                "harness_fault": r.harness_fault,
                "evidence_folder": (f"reports/failures/{r.artifact_folder}"
                                    if r.artifact_folder else None),
            })
    return {"failed_count": len(out), "failures": out}


def get_systemic_clusters(base: str = "reports/trend") -> dict[str, Any]:
    """Signature-matched systemic failure clusters (>=3 independent tests
    sharing one normalized error signature — evidence of a common cause).
    Distinct from mere feature concentration."""
    from utils.failure_clustering import detect_systemic
    fail_records, signed = _collect_fail_records(_engines(base))
    clusters = detect_systemic(fail_records, min_tests=3) if signed else []
    return {
        "signed_failures": signed,
        "clusters": [{
            "confirmed": (f"{s.affected_tests} independent tests share this "
                          f"normalized signature"),
            "failure_type": _failure_type(s.sample_message),
            "signature": s.sample_message[:200],
            "affected_tests": s.affected_tests,
            "features": s.sample_features,
            "engines": s.engines,
            "confidence": s.confidence,
            "test_methods": s.test_methods,
        } for s in clusters],
    }


def get_failure_evidence(test_name: str, base: str = "reports/trend") -> dict[str, Any]:
    """Evidence for one failing test (substring match on the test name): error
    signature, AI diagnosis + suggested fix, page URL, network digest, and the
    on-disk evidence paths (screenshot, DOM, video, network bodies)."""
    matches = []
    for ed in _engines(base):
        for r in ed.records:
            if r.status != "FAIL" or test_name.lower() not in f"{r.clazz}::{r.method}".lower():
                continue
            ev = _load_failure_evidence(r.artifact_folder, embed_shot=False)
            folder = f"reports/failures/{r.artifact_folder}" if r.artifact_folder else None
            matches.append({
                **_prov(ed),
                "test": f"{r.clazz}::{r.method}",
                "feature": r.feature,
                "error_signature": r.error_signature or None,
                "diagnosis": ev.get("diagnosis"),
                "suggested_fix": ev.get("suggested_fix"),
                "triage_category": ev.get("triage_category"),
                "triage_confidence": ev.get("confidence"),
                "page_url": ev.get("url"),
                "network": ev.get("net"),
                "evidence_folder": folder,
                "evidence_files": ({
                    "screenshot": f"{folder}/screenshot.png",
                    "dom": f"{folder}/dom.html",
                    "video": f"{folder}/recording.webm",
                    "network_events": f"{folder}/network-events.json",
                } if folder else None),
            })
    return {"matches": len(matches), "evidence": matches}


def get_mobile_findings(base: str = "reports/trend", engine: str = "chromium",
                        severity: str | None = None, check: str | None = None,
                        limit: int = 50) -> dict[str, Any]:
    """Mobile findings for one engine, filterable by severity (blocker/major/
    minor/info) and check (tap_target, font_size, input_zoom, ...). Each
    finding carries element message, device, page, CSS selector and the
    recommended fix."""
    from utils.combined_report_builder import _recommendation_for
    ed = load_engine(engine, base)
    findings = (ed.mobile or {}).get("findings") or []
    sel = [f for f in findings
           if (not severity or str(f.get("severity", "")).lower() == severity.lower())
           and (not check or str(f.get("category", "")).lower() == check.lower())]
    return {
        **_prov(ed),
        "total_matching": len(sel),
        "returned": min(len(sel), limit),
        "findings": [{
            "severity": f.get("severity"), "check": f.get("category"),
            "issue": f.get("message"), "device": f.get("device"),
            "page": f.get("page"), "selector": f.get("selector"),
            "recommended": _recommendation_for(f.get("category", "")),
        } for f in sel[:limit]],
    }


def get_failure_history(base: str = "reports/trend") -> dict[str, Any]:
    """Cross-run failure classification: NEW (1 fail), OBSERVED (2),
    RECURRING (3+), FLAKY (passes and fails), CONSISTENT_FAILURE (>=90%)."""
    from pathlib import Path
    from utils.failure_clustering import build_clusters
    clusters = build_clusters(snapshot_dir=Path(base) / "snapshots", last_n=30)
    return {"clusters": [{
        "test": c.sample_method, "feature": c.sample_feature,
        "label": c.label,
        "fails": c.occurrences_in_history, "passes": c.passes_in_history,
        "runs_seen": c.runs_seen, "window": c.window_size,
        "last_seen": c.last_seen,
    } for c in clusters]}


def compare_engines(base: str = "reports/trend") -> dict[str, Any]:
    """4-state cross-engine comparison over co-executed tests: shared failures,
    engine-only defects (ran on both, failed on one), and not-comparable (never
    ran on the other engine — NOT a pass, NOT a defect)."""
    engines = _engines(base)
    by = {e.engine: e for e in engines}
    c, w = by["chromium"], by["webkit"]
    fp = lambda r: (f"{r.clazz}::{r.method}", r.feature)  # noqa: E731
    c_run, w_run = {fp(r) for r in c.records}, {fp(r) for r in w.records}
    c_fail = {fp(r) for r in c.records if r.status == "FAIL"}
    w_fail = {fp(r) for r in w.records if r.status == "FAIL"}
    shared = sorted(m for m, _ in (c_fail & w_fail))
    return {
        "chromium": _prov(c), "webkit": _prov(w),
        "eligibility": _eligibility(engines),
        "co_executed": len(c_run & w_run),
        "shared_failures": len(c_fail & w_fail),
        "chromium_only_defects": len({f for f in (c_fail - w_fail) if f in w_run}),
        "webkit_only_defects": len({f for f in (w_fail - c_fail) if f in c_run}),
        "not_comparable": len({f for f in c_fail if f not in w_run}
                              | {f for f in w_fail if f not in c_run}),
        "shared_failure_tests": shared[:60],
    }


def get_run_provenance(base: str = "reports/trend") -> dict[str, Any]:
    """When each engine last completed a run, what suite it was, and where the
    combined report lives — so a caller always knows how fresh the data is."""
    import os
    out = {"engines": [], "combined_report": f"{base}/combined-report.html"}
    for ed in _engines(base):
        sc = ed.sidecar or {}
        out["engines"].append({
            **_prov(ed),
            "suite": sc.get("population", {}).get("suite"),
            "total": sc.get("population", {}).get("total"),
            "run_finished_at_ms": sc.get("run_finished_at"),
            "git_commit": (sc.get("git") or {}).get("commit"),
            "browser": (sc.get("environment") or {}).get("browser"),
        })
    if os.path.isfile(out["combined_report"]):
        out["combined_report_mtime"] = int(os.path.getmtime(out["combined_report"]))
    return out
