"""
Python-side HTML dashboard builder.
Produces reports/trend/dashboard.html from the in-memory test records.
No Java dependency — fully self-contained.
"""
from __future__ import annotations

import html
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.snapshot_writer import TestRecord
from utils.risk_interpreter import interpret, ReleaseStatus
import utils.health_tracker as _ht
from utils.layered_health_scores import compute as _compute_layered, score_band
from utils.mobile_report_builder import (
    mobile_was_exercised as _mobile_ran,
    session_findings as _mobile_findings,
    session_check_stats as _mobile_check_stats,
)
from utils.error_clusterer import ErrorCluster

logger = logging.getLogger(__name__)

DASHBOARD_PATH = Path("reports/trend/dashboard.html")
TREND_JSON     = Path("reports/trend/trend-history.json")

# The per-engine dashboards are consolidated into the Combined Cross-Engine
# Report. build() emits a redirect stub instead of the full dashboard; set this
# True to restore the legacy per-engine page.
EMIT_FULL_DASHBOARD = False


def _redirect_stub_html() -> str:
    """A tiny page that forwards the (now-retired) per-engine dashboard to the
    combined report. Computes a relative href from THIS engine's location
    (chromium: sibling; webkit: ../)."""
    import os as _os
    try:
        href = _os.path.relpath(Path("reports/trend/combined-report.html"), DASHBOARD_PATH.parent)
    except Exception:
        href = "combined-report.html"
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\">"
        f"<meta http-equiv=\"refresh\" content=\"0; url={href}\">"
        "<title>Flozic FlowGuard — Combined Report</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
        "background:#f1f5f9;color:#1e293b;padding:56px 24px;text-align:center}"
        "a{color:#3730a3;font-weight:700;font-size:18px;text-decoration:none}"
        ".sub{color:#64748b;font-size:13px;margin-top:10px}</style></head><body>"
        "<h2>The per-engine dashboards have been consolidated.</h2>"
        f"<p>Everything now lives in one place — the <a href=\"{href}\">Combined Cross-Engine Report &rarr;</a></p>"
        f"<p class=\"sub\">Redirecting… if nothing happens, click the link above.</p>"
        "</body></html>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def build(records: list[TestRecord], started_at_ms: int) -> Path:
    """
    Build the HTML dashboard from this session's records.
    Also appends a row to trend-history.json for future trend display.
    Returns the path to the written HTML file.
    """
    stats    = _compute_stats(records)
    decision = interpret(
        total        = stats["total"],
        passed       = stats["passed"],
        failed       = stats["failed"],
        smoke_total  = stats["smoke_total"],
        smoke_passed = stats["smoke_passed"],
        harness_faults = stats["harness_faults"],
    )

    # Health score + layered domain scores
    health_score = _ht.compute_score(stats["total"], stats["passed"], stats["failed"])
    clusters     = _ht.get_clusters()
    # Mobile findings must be passed HERE too, not only in conftest. build()
    # computes its own layered scores, so omitting them would print
    # "Mobile: not measured" on the dashboard while the session log reported a
    # real Mobile score for the same run. check_stats must be resolved the
    # same way conftest does it -- otherwise the trend-history entry appended
    # below (from THIS score) records the undiscounted/ungated number while
    # conftest logs the correct, coverage-gated one for the same run.
    _cs = _mobile_check_stats()
    _engine_cs = next(iter(_cs.values()), None) if _cs else None
    layered      = _compute_layered(records, clusters,
                                    mobile_findings=_mobile_findings(),
                                    mobile_tested=_mobile_ran(),
                                    check_stats=_engine_cs)

    _append_trend(stats, decision, started_at_ms, layered)

    # Consolidated reporting: the per-engine dashboards are RETIRED in favour of
    # the single Combined Cross-Engine Report, which now carries the per-engine
    # detail + dev-actionable evidence. We still run the full pipeline above so
    # trend-history and the persisted data the combined report reads stay
    # correct — we just emit a redirect STUB here instead of a duplicate
    # dashboard. Flip EMIT_FULL_DASHBOARD to restore the legacy per-engine page.
    DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    if EMIT_FULL_DASHBOARD:
        html = _render(records, stats, decision, _load_trend(), started_at_ms,
                       health_score=health_score, layered=layered, clusters=clusters,
                       mobile_findings=_mobile_findings())
    else:
        html = _redirect_stub_html()
    DASHBOARD_PATH.write_text(html, encoding="utf-8")
    logger.info("[DashboardBuilder] Written → %s", DASHBOARD_PATH)
    return DASHBOARD_PATH


# ─────────────────────────────────────────────────────────────────────────────
# Stats
# ─────────────────────────────────────────────────────────────────────────────

def _compute_stats(records: list[TestRecord]) -> dict[str, Any]:
    total   = len(records)
    passed  = sum(1 for r in records if r.status == "PASS")
    failed  = sum(1 for r in records if r.status == "FAIL")
    skipped = sum(1 for r in records if r.status == "SKIP")

    smoke   = [r for r in records if r.category.upper() == "SMOKE"]
    smoke_total  = len(smoke)
    smoke_passed = sum(1 for r in smoke if r.status == "PASS")

    by_feature: dict[str, dict] = {}
    for r in records:
        entry = by_feature.setdefault(r.feature, {"pass": 0, "fail": 0, "skip": 0})
        entry[r.status.lower() if r.status.lower() in ("pass","fail","skip") else "skip"] += 1

    total_ms = sum(r.duration_ms for r in records)
    # Failures attributed to the harness/infrastructure (connectivity loss,
    # missing env) — the release gate excludes them from the product fail-rate.
    harness_faults = sum(1 for r in records
                         if r.status == "FAIL" and getattr(r, "harness_fault", False))
    return dict(
        total=total, passed=passed, failed=failed, skipped=skipped,
        smoke_total=smoke_total, smoke_passed=smoke_passed,
        harness_faults=harness_faults,
        pass_rate=round(passed / total * 100, 1) if total else 0,
        by_feature=by_feature,
        total_ms=total_ms,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Trend persistence
# ─────────────────────────────────────────────────────────────────────────────

def _append_trend(stats: dict, decision: Any, started_at_ms: int,
                  scores: Any = None) -> None:
    """Append one run to the rolling 30-run history.

    Records `overall` and `scoring_version` from this run onward. Neither was
    persisted before -- which is why adding Mobile to the formula needed no
    backfill, but also meant a future formula change would be undetectable.
    Entries lacking `scoring_version` predate v2; do not plot them on the same
    axis as versioned ones.
    """
    history: list[dict] = []
    if TREND_JSON.exists():
        try:
            history = json.loads(TREND_JSON.read_text(encoding="utf-8"))
        except Exception:
            history = []

    # Run-execution status (COMPLETED / EMPTY): a unit-only or aborted session
    # that executed 0 tests is NOT a test result — trend charts and the
    # flake window must be able to exclude it instead of averaging in 0/0 junk.
    from utils.report_data_sidecar import classify_execution
    history.append({
        "ts":        started_at_ms,
        "label":     datetime.fromtimestamp(started_at_ms / 1000, tz=timezone.utc)
                              .strftime("%Y-%m-%d %H:%M UTC"),
        "run_status": classify_execution(stats["total"]),
        "total":     stats["total"],
        "passed":    stats["passed"],
        "failed":    stats["failed"],
        "pass_rate": stats["pass_rate"],
        "status":    decision.status.value,
        # Absent on entries written before scoring was versioned.
        **({"overall": scores.overall,
            "mobile": scores.mobile_health,
            "scoring_version": scores.scoring_version} if scores is not None else {}),
    })

    # Keep last 30 runs
    history = history[-30:]
    # Atomic: the other engine's parallel rebuild reads this trend file for the
    # authoritative overall/mobile scores — never let it see a partial write.
    from utils.atomic_io import atomic_write_json
    atomic_write_json(TREND_JSON, history)


def _load_trend() -> list[dict]:
    if not TREND_JSON.exists():
        return []
    try:
        return json.loads(TREND_JSON.read_text(encoding="utf-8"))
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# HTML rendering
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_ms(ms: int) -> str:
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms/1000:.1f} s"
    return f"{ms/60_000:.1f} min"


def _status_badge(status: str) -> str:
    colour = {"PASS": "#16a34a", "FAIL": "#dc2626", "SKIP": "#94a3b8"}.get(status, "#6b7280")
    bg     = {"PASS": "#dcfce7", "FAIL": "#fee2e2", "SKIP": "#f1f5f9"}.get(status, "#f3f4f6")
    return (
        f"<span style='display:inline-block;padding:2px 8px;border-radius:9999px;"
        f"font-size:11px;font-weight:700;background:{bg};color:{colour}'>{status}</span>"
    )


# Icon + colour per failure-history label (min-observation classification).
_LABEL_STYLE = {
    "NEW":                ("🆕", "#0369a1", "#dbeafe"),
    "OBSERVED":           ("👁", "#0369a1", "#dbeafe"),
    "RECURRING":          ("🔁", "#92400e", "#fef3c7"),
    "FLAKY":              ("⚠️", "#7c3aed", "#ede9fe"),
    "CONSISTENT_FAILURE": ("🔁", "#b91c1c", "#fee2e2"),
}


def _recurring_badge(occurrences: int, window: int, score: float,
                     label: str = "NEW") -> str:
    """Failure-history badge, e.g. '🆕 NEW 1/30' or '🔁 RECURRING 4/30'.

    Classification (not score) decides the word: a failure seen once is NEW,
    never 'recurring'. Shown for every classified failure so a one-off reads
    honestly as new rather than as an established pattern.
    """
    icon, color, bg = _LABEL_STYLE.get(label, _LABEL_STYLE["NEW"])
    return (
        f"<span style='display:inline-block;padding:2px 8px;border-radius:9999px;"
        f"font-size:11px;font-weight:700;background:{bg};color:{color};"
        f"margin-left:8px;vertical-align:middle;' "
        f"title='{label}: failed in {occurrences}/{window} runs (score {score:.2f})'>"
        f"{icon} {label} {occurrences}/{window}</span>"
    )


def _href_from_dashboard(target: str | Path) -> str:
    """
    Build an href that resolves from the dashboard's location
    (reports/trend/dashboard.html).

    Handles both absolute paths (e.g. C:/.../reports/recordings/foo/x.webm)
    and project-relative paths (e.g. reports/recordings/foo/x.webm). The
    earlier code naively prefixed '../../' to whatever it got, which
    produced URLs like  '../../C:/Users/.../py/reports/...'  when the
    input was absolute — Chrome then resolved that to a doubled file://
    path that didn't exist on disk.
    """
    p = Path(target)
    # Reduce to a path relative to the project's working directory.
    if p.is_absolute():
        try:
            p = p.relative_to(Path.cwd())
        except ValueError:
            # Outside cwd: fall back to a bare file:// URL so the link at
            # least works even if it leaves the project root.
            return p.resolve().as_uri()
    # Dashboard lives at reports/trend/dashboard.html — two levels under cwd.
    return "../../" + p.as_posix()


# ─────────────────────────────────────────────────────────────────────────────
# AI integration helpers — surface artifacts written by utils/ai_*.py modules
# on the per-test dashboard rows. All helpers are NO-OP-safe: if the JSON
# files don't exist (test ran without OPENAI_API_KEY, or with AI disabled),
# they return '' and the dashboard renders exactly as if AI weren't wired.
# ─────────────────────────────────────────────────────────────────────────────

def _recording_dir_for(r: TestRecord) -> Path | None:
    """
    Locate the directory where AI artifacts (ai_verdict.json, canvas.png,
    generated_prompt.json, visual_diff.json) live for this test.

    Conventions:
      - Tests write to reports/recordings/<test_method>/ during the test.
      - On FAIL, conftest MOVES the video into reports/failures/<folder>/
        but leaves the AI artifacts in reports/recordings/<test_method>/.
      - Parametrized diversified tests use a derived path that drops the
        square brackets, e.g.
            method=test_flozic_diversified_connect[chatgpt-v0]
            dir   = reports/recordings/test_flozic_diversified_chatgpt_v0/

    Strategy: check the canonical recordings/<method>/ path FIRST (so we
    still find AI artifacts after a failure moved the video). Then try the
    diversified-derived path. Last-resort fall back to video_path's parent.
    """
    # Parametrized diversified test? method like
    # 'test_flozic_diversified_connect[<slug>-v<n>]' where <slug> may itself
    # contain hyphens (housecall-pro, microsoft-excel, etc.).
    # IMPORTANT: check this BEFORE the canonical path. Playwright's video
    # recorder auto-creates reports/recordings/<method-with-brackets>/ for
    # the .webm file, but our test code writes AI artifacts to a separate
    # bracket-less dir derived from slug + variant. So the bracketed path
    # exists but is empty of AI artifacts — we want the derived path.
    import re as _re
    m = _re.match(
        r"^test_flozic_diversified_connect\[(.+)-v(\d+)\]$", r.method,
    )
    if m:
        slug    = m.group(1).replace("-", "_")
        variant = m.group(2)
        derived = Path("reports/recordings") / (
            f"test_flozic_diversified_{slug}_v{variant}"
        )
        if derived.exists():
            return derived

    canonical = Path("reports/recordings") / r.method
    if canonical.exists():
        return canonical

    if r.video_path:
        try:
            parent = Path(r.video_path).parent
            if parent.exists():
                return parent
        except Exception:
            pass
    return None


def _failure_folder_for(r: TestRecord) -> Path | None:
    """Return reports/failures/<artifact_folder>/ for FAIL rows, else None."""
    if r.artifact_folder:
        path = Path("reports/failures") / r.artifact_folder
        return path if path.exists() else None
    return None


def _read_json_safe(path: Path) -> dict | None:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("Could not read %s: %s", path, e)
    return None


def _truncate(text: str, n: int = 110) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[:n - 1] + "…"


def _ai_status_badge(status: str) -> str:
    """Canvas-validation verdict from utils/ai_validator.py."""
    color, bg, label = {
        "VALID":   ("#15803d", "#dcfce7", "🤖 AI: VALID"),
        "INVALID": ("#b91c1c", "#fee2e2", "🤖 AI: INVALID"),
        "SKIPPED": ("#64748b", "#f1f5f9", "🤖 AI: SKIPPED"),
        "ERROR":   ("#92400e", "#fef3c7", "🤖 AI: ERROR"),
    }.get(status, ("#64748b", "#f1f5f9", f"🤖 AI: {status}"))
    return (
        f"<span style='display:inline-block;padding:2px 8px;border-radius:9999px;"
        f"font-size:11px;font-weight:700;background:{bg};color:{color};"
        f"margin-right:8px;vertical-align:middle;'>{label}</span>"
    )


def _triage_badge(category: str, severity: str) -> str:
    """Failure-triage classification from utils/ai_triage.py."""
    color, bg = {
        "PRODUCT_BUG":   ("#b91c1c", "#fee2e2"),
        "LOCATOR_DRIFT": ("#92400e", "#fef3c7"),
        "FLAKE":         ("#0369a1", "#dbeafe"),
        "INFRA":         ("#7c3aed", "#ede9fe"),
        "TEST_BUG":      ("#9f1239", "#fce7f3"),
        "UNKNOWN":       ("#64748b", "#f1f5f9"),
    }.get(category, ("#64748b", "#f1f5f9"))
    sev_icon = {"blocker": "🔥", "major": "⚠️", "minor": "·"}.get(severity, "·")
    return (
        f"<span style='display:inline-block;padding:2px 8px;border-radius:9999px;"
        f"font-size:11px;font-weight:700;background:{bg};color:{color};"
        f"margin-right:8px;vertical-align:middle;' title='{severity} severity'>"
        f"{sev_icon} {category}</span>"
    )


def _visual_diff_badge(status: str) -> str:
    """Visual-regression verdict from utils/ai_visual_diff.py."""
    color, bg, label = {
        "IDENTICAL":  ("#15803d", "#dcfce7", "📸 IDENTICAL"),
        "COSMETIC":   ("#ca8a04", "#fef3c7", "🎨 COSMETIC"),
        "REGRESSION": ("#b91c1c", "#fee2e2", "🔴 REGRESSION"),
        "UNKNOWN":    ("#64748b", "#f1f5f9", "❓ UNKNOWN"),
        "FIRST_RUN":  ("#0284c7", "#dbeafe", "📷 FIRST RUN"),
        "SKIPPED":    ("#64748b", "#f1f5f9", "📸 SKIPPED"),
        "ERROR":      ("#92400e", "#fef3c7", "📸 ERROR"),
    }.get(status, ("#64748b", "#f1f5f9", f"📸 {status}"))
    return (
        f"<span style='display:inline-block;padding:2px 8px;border-radius:9999px;"
        f"font-size:11px;font-weight:700;background:{bg};color:{color};"
        f"margin-right:8px;vertical-align:middle;'>{label}</span>"
    )


def _ai_artifact_links(r: TestRecord) -> str:
    """AI badges + links to canvas/prompt/verdict/triage/visual JSONs."""
    rec_dir  = _recording_dir_for(r)
    fail_dir = _failure_folder_for(r)
    verdict     = _read_json_safe(rec_dir / "ai_verdict.json") if rec_dir else None
    generated   = _read_json_safe(rec_dir / "generated_prompt.json") if rec_dir else None
    canvas      = (rec_dir / "canvas.png") if rec_dir else None
    visual_diff = _read_json_safe(rec_dir / "visual_diff.json") if rec_dir else None
    triage      = _read_json_safe(fail_dir / "triage.json") if fail_dir else None
    if (verdict is None and generated is None and triage is None
            and visual_diff is None and (canvas is None or not canvas.exists())):
        return ""

    parts = []
    if triage and triage.get("status") == "TRIAGED":
        parts.append(_triage_badge(
            str(triage.get("category", "UNKNOWN")),
            str(triage.get("severity", "minor")),
        ))
    if visual_diff:
        parts.append(_visual_diff_badge(str(visual_diff.get("status", "UNKNOWN"))))
    if verdict:
        parts.append(_ai_status_badge(str(verdict.get("status", "UNKNOWN"))))
    if canvas and canvas.exists():
        href = _href_from_dashboard(canvas)
        parts.append(
            f"<a href='{href}' target='_blank' "
            f"style='color:#0891b2;margin-right:8px;font-size:11px;font-weight:600;'>"
            f"🖼️ Canvas</a>"
        )
    if generated:
        href = _href_from_dashboard(rec_dir / "generated_prompt.json")
        parts.append(
            f"<a href='{href}' target='_blank' "
            f"style='color:#4f46e5;margin-right:8px;font-size:11px;'>📝 Prompt</a>"
        )
    if verdict:
        href = _href_from_dashboard(rec_dir / "ai_verdict.json")
        parts.append(
            f"<a href='{href}' target='_blank' "
            f"style='color:#4f46e5;margin-right:8px;font-size:11px;'>🧪 Verdict</a>"
        )
    if triage:
        href = _href_from_dashboard(fail_dir / "triage.json")
        parts.append(
            f"<a href='{href}' target='_blank' "
            f"style='color:#dc2626;margin-right:8px;font-size:11px;'>🩺 Triage</a>"
        )
    if visual_diff:
        href = _href_from_dashboard(rec_dir / "visual_diff.json")
        parts.append(
            f"<a href='{href}' target='_blank' "
            f"style='color:#0891b2;margin-right:8px;font-size:11px;'>📐 Visual</a>"
        )
    return "".join(parts)


def _ai_details_block(r: TestRecord) -> str:
    """Inline subtext under the test name: prompt + AI reasoning + diagnosis +
    suggested fix. Lets QA eyeball failures without opening any JSON."""
    rec_dir   = _recording_dir_for(r)
    fail_dir  = _failure_folder_for(r)
    verdict   = _read_json_safe(rec_dir / "ai_verdict.json") if rec_dir else None
    generated = _read_json_safe(rec_dir / "generated_prompt.json") if rec_dir else None
    triage    = _read_json_safe(fail_dir / "triage.json") if fail_dir else None
    if verdict is None and generated is None and triage is None:
        return ""

    lines = []
    if generated and generated.get("prompt"):
        full = generated["prompt"]
        lines.append(
            f"<div style='font-size:11px;color:#475569;margin-top:4px;' "
            f"title='{full}'><span style='color:#94a3b8;'>↪ prompt:</span> "
            f"<span style='font-style:italic;'>{_truncate(full)}</span></div>"
        )
    if verdict and verdict.get("reasoning"):
        full   = verdict["reasoning"]
        status = verdict.get("status", "")
        color  = "#b91c1c" if status == "INVALID" else "#475569"
        lines.append(
            f"<div style='font-size:11px;color:{color};margin-top:2px;' "
            f"title='{full}'><span style='color:#94a3b8;'>↪ AI says:</span> "
            f"{_truncate(full)}</div>"
        )
    if triage and triage.get("status") == "TRIAGED":
        diagnosis     = triage.get("diagnosis", "")
        suggested_fix = triage.get("suggested_fix", "")
        if diagnosis:
            lines.append(
                f"<div style='font-size:11px;color:#b91c1c;margin-top:2px;' "
                f"title='{diagnosis}'><span style='color:#94a3b8;'>↪ diagnosis:</span> "
                f"{_truncate(diagnosis)}</div>"
            )
        if suggested_fix:
            lines.append(
                f"<div style='font-size:11px;color:#15803d;margin-top:2px;' "
                f"title='{suggested_fix}'><span style='color:#94a3b8;'>↪ suggested fix:</span> "
                f"{_truncate(suggested_fix)}</div>"
            )
    return "".join(lines)


def _artifact_links(r: TestRecord) -> str:
    parts = []
    if r.artifact_folder:
        # artifact_folder is just the folder name, e.g.
        # 'test_xyz_20260609_115400_123' — under reports/failures/.
        base = _href_from_dashboard(Path("reports/failures") / r.artifact_folder)
        parts += [
            f"<a href='{base}/screenshot.png' target='_blank' style='color:#4f46e5;margin-right:8px;font-size:11px;'>📷 Screenshot</a>",
            f"<a href='{base}/dom.html' target='_blank' style='color:#4f46e5;margin-right:8px;font-size:11px;'>🔍 DOM</a>",
            f"<a href='{base}/url.txt' target='_blank' style='color:#4f46e5;margin-right:8px;font-size:11px;'>🔗 URL</a>",
        ]
        webm = f"{base}/recording.webm"
        parts.append(
            f"<a href='{webm}' target='_blank' "
            f"style='color:#7c3aed;margin-right:8px;font-size:11px;font-weight:600;'>▶ Replay</a>"
        )
    elif r.video_path:
        # Passed test with recording — video_path may be absolute or relative.
        href = _href_from_dashboard(r.video_path)
        parts.append(
            f"<a href='{href}' target='_blank' "
            f"style='color:#7c3aed;font-size:11px;font-weight:600;'>▶ Replay</a>"
        )
    # Append AI badges/links — silently no-op when no AI artifacts exist.
    parts.append(_ai_artifact_links(r))
    return "".join(parts)


def _render_test_rows(
    records: list[TestRecord],
    recurring_by_method: dict | None = None,
) -> str:
    """
    Render the per-test rows. If `recurring_by_method` is provided
    (built from FailureCluster.sample_method), failing rows whose method
    matches will get a 🔁 N/M recurring-failure badge next to the status.
    """
    recurring_by_method = recurring_by_method or {}
    rows = []
    for r in records:
        bg = "#fef2f2" if r.status == "FAIL" else ("" if r.status == "PASS" else "#f8fafc")
        cluster = recurring_by_method.get(r.method) if r.status == "FAIL" else None
        recurring_html = ""
        if cluster:
            recurring_html = _recurring_badge(
                cluster.occurrences_in_history,
                cluster.window_size,
                cluster.recurring_score,
                getattr(cluster, "label", "NEW"),
            )
        ai_details = _ai_details_block(r)
        # data-* attributes drive the client-side filter / search / sort.
        # data-search is a lowercased haystack of every searchable field.
        search_blob = " ".join([
            r.method, r.feature, r.category, r.status,
        ]).lower().replace("'", " ")
        rows.append(
            f"<tr class='tr-row' "
            f"data-status='{r.status}' "
            f"data-feature='{_html_attr(r.feature)}' "
            f"data-category='{_html_attr(r.category)}' "
            f"data-duration='{r.duration_ms}' "
            f"data-method='{_html_attr(r.method)}' "
            f"data-search='{_html_attr(search_blob)}' "
            f"style='background:{bg}'>"
            f"<td style='padding:8px 12px;font-size:13px;font-family:monospace;vertical-align:top;'>"
            f"<div>{r.method}</div>{ai_details}</td>"
            f"<td style='padding:8px 12px;font-size:12px;color:#64748b;vertical-align:top;'>{r.feature}</td>"
            f"<td style='padding:8px 12px;font-size:12px;color:#64748b;vertical-align:top;'>{r.category}</td>"
            f"<td style='padding:8px 12px;vertical-align:top;'>{_status_badge(r.status)}{recurring_html}</td>"
            f"<td style='padding:8px 12px;font-size:12px;color:#64748b;text-align:right;vertical-align:top;'>{_fmt_ms(r.duration_ms)}</td>"
            f"<td style='padding:8px 12px;vertical-align:top;'>{_artifact_links(r)}</td>"
            f"</tr>"
        )
    return "\n".join(rows)


def _html_attr(s: str) -> str:
    """Escape a string for safe use inside a single-quoted HTML attribute."""
    return (str(s or "")
            .replace("&", "&amp;").replace("'", "&#39;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _render_test_results_toolbar(records: list[TestRecord]) -> str:
    """
    Toolbar above the Test Results table: status filter, feature filter,
    category filter, and a search box. All client-side (see _filter_sort_script).
    """
    features   = sorted({r.feature for r in records if r.feature})
    categories = sorted({r.category for r in records if r.category})
    statuses   = ["PASS", "FAIL", "SKIP"]

    def _opts(values: list[str], label: str) -> str:
        opts = f"<option value=''>{label}</option>"
        opts += "".join(f"<option value='{_html_attr(v)}'>{v}</option>" for v in values)
        return opts

    sel_style = (
        "font-size:12px;padding:5px 8px;border-radius:6px;border:1px solid #cbd5e1;"
        "background:#fff;color:#1e293b;cursor:pointer;"
    )
    return f"""
    <div class="no-print" style="display:flex;gap:8px;align-items:center;
                flex-wrap:wrap;margin-bottom:12px;">
      <input id="tr-search" type="text" placeholder="🔎 Search tests…"
             oninput="filterTestRows()"
             style="font-size:12px;padding:6px 10px;border-radius:6px;
                    border:1px solid #cbd5e1;min-width:220px;flex:1;max-width:340px;">
      <select id="tr-status" onchange="filterTestRows()" style="{sel_style}">
        {_opts(statuses, 'All statuses')}
      </select>
      <select id="tr-feature" onchange="filterTestRows()" style="{sel_style}">
        {_opts(features, 'All features')}
      </select>
      <select id="tr-category" onchange="filterTestRows()" style="{sel_style}">
        {_opts(categories, 'All categories')}
      </select>
      <button onclick="resetTestFilters()" style="{sel_style}font-weight:600;">
        ✕ Clear
      </button>
      <span id="tr-count" style="font-size:11px;color:#64748b;margin-left:auto;"></span>
    </div>
    """


def _filter_sort_script() -> str:
    """
    Client-side filter / search / sort for the Test Results table, plus the
    auto-refresh toggle. Pure vanilla JS, no deps. Returned as a plain string
    (NOT an f-string) so the JS braces don't need escaping.
    """
    return """
    <script>
    (function () {
      // ── Filter + search ──────────────────────────────────────────────
      window.filterTestRows = function () {
        var q    = (document.getElementById('tr-search')   || {}).value || '';
        var st   = (document.getElementById('tr-status')   || {}).value || '';
        var feat = (document.getElementById('tr-feature')  || {}).value || '';
        var cat  = (document.getElementById('tr-category') || {}).value || '';
        q = q.trim().toLowerCase();
        var rows = document.querySelectorAll('#test-results-body .tr-row');
        var shown = 0;
        rows.forEach(function (row) {
          var okSearch = !q || (row.getAttribute('data-search') || '').indexOf(q) !== -1;
          var okStatus = !st || row.getAttribute('data-status') === st;
          var okFeat   = !feat || row.getAttribute('data-feature') === feat;
          var okCat    = !cat || row.getAttribute('data-category') === cat;
          var show = okSearch && okStatus && okFeat && okCat;
          row.style.display = show ? '' : 'none';
          if (show) shown++;
        });
        var count = document.getElementById('tr-count');
        if (count) count.textContent = shown + ' of ' + rows.length + ' shown';
        var empty = document.getElementById('tr-empty');
        if (empty) empty.style.display = (shown === 0 && rows.length > 0) ? 'block' : 'none';
      };

      window.resetTestFilters = function () {
        ['tr-search','tr-status','tr-feature','tr-category'].forEach(function (id) {
          var el = document.getElementById(id);
          if (el) el.value = '';
        });
        window.filterTestRows();
      };

      // ── Sort ─────────────────────────────────────────────────────────
      var sortState = { key: null, dir: 1 };
      window.sortTestTable = function (key, th) {
        var body = document.getElementById('test-results-body');
        if (!body) return;
        // Toggle direction if same column clicked again.
        if (sortState.key === key) { sortState.dir *= -1; }
        else { sortState.key = key; sortState.dir = 1; }
        var rows = Array.prototype.slice.call(body.querySelectorAll('.tr-row'));
        rows.sort(function (a, b) {
          var av = a.getAttribute('data-' + key) || '';
          var bv = b.getAttribute('data-' + key) || '';
          if (key === 'duration') {
            av = parseFloat(av) || 0; bv = parseFloat(bv) || 0;
            return (av - bv) * sortState.dir;
          }
          return av.toLowerCase().localeCompare(bv.toLowerCase()) * sortState.dir;
        });
        rows.forEach(function (r) { body.appendChild(r); });
        // Update sort indicators
        document.querySelectorAll('#test-results-table .sort-ind').forEach(function (s) {
          s.textContent = '';
        });
        if (th) {
          var ind = th.querySelector('.sort-ind');
          if (ind) ind.textContent = sortState.dir === 1 ? ' ▲' : ' ▼';
        }
      };

      // ── Auto-refresh ─────────────────────────────────────────────────
      var autoTimer = null;
      window.toggleAutoReload = function () {
        var cb = document.getElementById('auto-reload-toggle');
        try {
          localStorage.setItem('flowguard-autoreload', cb && cb.checked ? '1' : '0');
        } catch (e) {}
        if (cb && cb.checked) {
          autoTimer = setInterval(function () { location.reload(); }, 10000);
        } else if (autoTimer) {
          clearInterval(autoTimer); autoTimer = null;
        }
      };

      // ── Root-cause drill-down (Issues table) ─────────────────────────
      window.filterRootCause = function (rc, chip) {
        var rows = document.querySelectorAll(
          '#issues-table .issue-row, #issues-table .issue-row-details');
        rows.forEach(function (row) {
          var match = !rc || row.getAttribute('data-rootcause') === rc;
          row.style.display = match ? '' : 'none';
        });
        // Highlight the active chip.
        document.querySelectorAll('.rc-chip').forEach(function (c) {
          c.style.outline = (c.getAttribute('data-rc') === rc) ? '2px solid #0f172a' : 'none';
        });
      };

      // ── Collapsible sections (.card with a leading <h2>) ─────────────
      function initCollapsibles() {
        var cards = document.querySelectorAll('.card');
        cards.forEach(function (card) {
          var h2 = card.querySelector('h2');
          if (!h2) return;
          var key = 'flowguard-collapse-' + (h2.textContent || '').trim().slice(0, 40);
          // Wrap everything after the h2 in a .collapse-body container.
          var body = document.createElement('div');
          body.className = 'collapse-body';
          var sib = h2.nextSibling;
          while (sib) { var next = sib.nextSibling; body.appendChild(sib); sib = next; }
          card.appendChild(body);
          // Caret + click handler.
          var caret = document.createElement('span');
          caret.className = 'collapse-caret';
          caret.textContent = '▾ ';
          h2.insertBefore(caret, h2.firstChild);
          h2.classList.add('collapse-toggle');
          h2.addEventListener('click', function () {
            card.classList.toggle('section-collapsed');
            try {
              localStorage.setItem(key,
                card.classList.contains('section-collapsed') ? '1' : '0');
            } catch (e) {}
          });
          // Restore persisted state.
          try {
            if (localStorage.getItem(key) === '1') card.classList.add('section-collapsed');
          } catch (e) {}
        });
      }

      // ── Dark mode ────────────────────────────────────────────────────
      window.toggleDarkMode = function () {
        var on = document.body.classList.toggle('dark');
        var label = document.getElementById('dark-mode-label');
        if (label) label.textContent = on ? '☀️ Light' : '🌙 Dark';
        try { localStorage.setItem('flowguard-dark', on ? '1' : '0'); } catch (e) {}
      };

      // ── Screenshot hover preview (any link to a .png) ────────────────
      function initHoverPreview() {
        var pop = document.createElement('div');
        pop.id = 'hover-preview';
        var img = document.createElement('img');
        pop.appendChild(img);
        document.body.appendChild(pop);
        function isImg(a) {
          var h = (a.getAttribute('href') || '').toLowerCase();
          return h.indexOf('.png') !== -1 || h.indexOf('.jpg') !== -1
              || h.indexOf('.jpeg') !== -1 || h.indexOf('.webp') !== -1;
        }
        document.addEventListener('mouseover', function (e) {
          var a = e.target.closest ? e.target.closest('a') : null;
          if (!a || !isImg(a)) return;
          img.src = a.getAttribute('href');
          pop.style.display = 'block';
        });
        document.addEventListener('mousemove', function (e) {
          if (pop.style.display !== 'block') return;
          var x = e.clientX + 18, y = e.clientY + 18;
          // Keep within viewport.
          if (x + 470 > window.innerWidth) x = e.clientX - 470;
          if (y + 330 > window.innerHeight) y = window.innerHeight - 330;
          pop.style.left = Math.max(4, x) + 'px';
          pop.style.top  = Math.max(4, y) + 'px';
        });
        document.addEventListener('mouseout', function (e) {
          var a = e.target.closest ? e.target.closest('a') : null;
          if (a && isImg(a)) { pop.style.display = 'none'; img.src = ''; }
        });
      }

      // Restore auto-refresh preference + init the count on load.
      document.addEventListener('DOMContentLoaded', function () {
        try {
          if (localStorage.getItem('flowguard-autoreload') === '1') {
            var cb = document.getElementById('auto-reload-toggle');
            if (cb) { cb.checked = true; window.toggleAutoReload(); }
          }
          if (localStorage.getItem('flowguard-dark') === '1') {
            document.body.classList.add('dark');
            var label = document.getElementById('dark-mode-label');
            if (label) label.textContent = '☀️ Light';
          }
        } catch (e) {}
        initCollapsibles();
        initHoverPreview();
        if (window.filterTestRows) window.filterTestRows();
      });
    })();
    </script>
    """


def _tsv_escape(s: str) -> str:
    """Escape a value so it survives a TSV paste into Excel/Sheets."""
    if s is None:
        return ""
    # Newlines + tabs break TSV — collapse to spaces.
    return str(s).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _md_escape(s: str) -> str:
    """Escape a value for a Markdown table cell."""
    if s is None:
        return ""
    # Pipes break MD tables; newlines do too. Backslashes are not interpreted.
    return (str(s).replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip())


def _render_issues_section(records: list[TestRecord]) -> str:
    """
    Focused 'Issues to Triage' table — one row per FAIL or AI-INVALID test,
    pulling together every diagnostic signal we have: the original prompt,
    what flozic copilot actually built, the AI verdict, plus quick links to
    screenshot / triage / URL / replay. Diagnosis + suggested fix appear as
    a second row spanning the table width.

    PASS rows are intentionally hidden — the existing 'Test Results' table
    below still shows them. This section is the 'what needs my attention
    right now' view.

    Also embeds hidden <pre> blocks with TSV + Markdown serialisations of
    every issue row so the "📋 Copy as TSV" / "📋 Copy as Markdown" buttons
    can write a clean tabular paste to the clipboard.
    """
    # Pick rows that need triage: either pytest FAIL, or AI-INVALID verdict.
    issue_rows: list[tuple[TestRecord, dict, dict, dict, str]] = []
    for r in records:
        if r.status == "SKIP":
            continue
        rec_dir  = _recording_dir_for(r)
        fail_dir = _failure_folder_for(r)
        verdict  = _read_json_safe(rec_dir / "ai_verdict.json") if rec_dir else None
        generated = _read_json_safe(rec_dir / "generated_prompt.json") if rec_dir else None
        triage   = _read_json_safe(fail_dir / "triage.json") if fail_dir else None
        ai_invalid = (verdict or {}).get("status") == "INVALID"
        if r.status != "FAIL" and not ai_invalid:
            continue
        # Read URL captured at failure for FAIL rows
        url_text = ""
        if fail_dir is not None:
            url_path = fail_dir / "url.txt"
            try:
                if url_path.exists():
                    url_text = url_path.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        issue_rows.append((r, verdict or {}, generated or {}, triage or {}, url_text))

    if not issue_rows:
        return ""

    # TSV header — tab-separated for clean Excel/Sheets paste.
    tsv_cols = [
        "Test", "Category", "Status", "Duration",
        "Prompt asked", "Copilot built", "Verdict",
        "Triage", "Diagnosis", "Suggested fix", "URL",
    ]
    tsv_rows: list[str] = ["\t".join(tsv_cols)]
    md_rows:  list[str] = [
        "| " + " | ".join(tsv_cols) + " |",
        "| " + " | ".join(["---"] * len(tsv_cols)) + " |",
    ]
    # Rich-text (HTML) table accumulator. This is what makes the paste
    # render as a real table in Word / Outlook / Notion / Gmail / Confluence
    # / most chat apps — anywhere that accepts the clipboard's text/html
    # mime type. Inline styles are required (no external CSS reaches the
    # destination's renderer).
    import html as _html_mod
    def _h(s: str) -> str:
        return _html_mod.escape(str(s or ""))
    html_rows: list[str] = [
        "<tr style='background:#fee2e2;'>"
        + "".join(
            f"<th style='border:1px solid #cbd5e1;padding:6px 10px;"
            f"font-family:Arial,sans-serif;font-size:12px;color:#7f1d1d;"
            f"text-align:left;'>{_h(c)}</th>"
            for c in tsv_cols
        )
        + "</tr>"
    ]

    # Root-cause counts for the drill-down summary chips.
    rootcause_counts: dict[str, int] = {}

    # Build rows
    body_html: list[str] = []
    for r, verdict, generated, triage, url_text in issue_rows:
        # Prompt — from generated_prompt.json if available; else look up in
        # the hardcoded flozic_prompts.json by inferring app_key from method
        # name (test_flozic_<app>_connect).
        prompt_text = generated.get("prompt") or ""
        if not prompt_text:
            # Best-effort: parse 'test_flozic_<app>_connect' for the slug
            m = r.method
            if m.startswith("test_flozic_") and "_connect" in m:
                app_key = m[len("test_flozic_"):].split("_connect")[0].replace("_", "-")
                prompts_file = Path("tests/flozic_prompts.json")
                pdata = _read_json_safe(prompts_file) or {}
                prompt_text = (pdata.get(app_key) or {}).get("prompt", "")
        prompt_short = _truncate(prompt_text, 90) if prompt_text else "—"

        # What copilot built — pulled from ai_verdict.json's parsed raw payload
        raw = verdict.get("raw") if isinstance(verdict, dict) else None
        trigger_seen = ""
        actions_seen: list[str] = []
        if isinstance(raw, dict):
            trigger_seen = str(raw.get("trigger_app", "") or "")
            actions_seen = [str(a) for a in (raw.get("action_apps") or []) if a]
        if trigger_seen or actions_seen:
            built_text = (
                (f"<b>Trigger:</b> {trigger_seen}" if trigger_seen else "")
                + (f"<br><b>Actions:</b> {', '.join(actions_seen)}" if actions_seen else "")
            )
        else:
            built_text = "<span style='color:#94a3b8;'>(canvas not captured)</span>"

        # Verdict badge
        v_status = verdict.get("status") if verdict else ""
        verdict_html = _ai_status_badge(v_status) if v_status else (
            "<span style='color:#94a3b8;font-size:11px;'>—</span>"
        )

        # Triage badge + root-cause bucket for the drill-down summary.
        triage_html = ""
        rootcause = "UNCLASSIFIED"
        if triage.get("status") == "TRIAGED":
            rootcause = str(triage.get("category", "UNKNOWN"))
            triage_html = _triage_badge(
                rootcause,
                str(triage.get("severity", "minor")),
            )
        rootcause_counts[rootcause] = rootcause_counts.get(rootcause, 0) + 1

        # Links: 📷 Screenshot, 🩺 Triage, 🔗 URL, ▶ Replay
        rec_dir  = _recording_dir_for(r)
        fail_dir = _failure_folder_for(r)
        link_html_parts: list[str] = []
        canvas_png = (rec_dir / "canvas.png") if rec_dir else None
        screenshot_png = (fail_dir / "screenshot.png") if fail_dir else None
        # Prefer the FAIL-time screenshot when present; fall back to the
        # canvas screenshot. Both are linked when both exist.
        if screenshot_png and screenshot_png.exists():
            link_html_parts.append(
                f"<a href='{_href_from_dashboard(screenshot_png)}' target='_blank' "
                f"style='color:#4f46e5;font-size:11px;margin-right:6px;' "
                f"title='Failure screenshot'>📷</a>"
            )
        if canvas_png and canvas_png.exists():
            link_html_parts.append(
                f"<a href='{_href_from_dashboard(canvas_png)}' target='_blank' "
                f"style='color:#0891b2;font-size:11px;margin-right:6px;' "
                f"title='Canvas screenshot'>🖼️</a>"
            )
        if fail_dir is not None:
            triage_json = fail_dir / "triage.json"
            if triage_json.exists():
                link_html_parts.append(
                    f"<a href='{_href_from_dashboard(triage_json)}' target='_blank' "
                    f"style='color:#dc2626;font-size:11px;margin-right:6px;' "
                    f"title='AI triage JSON'>🩺</a>"
                )
        if url_text:
            link_html_parts.append(
                f"<a href='{url_text}' target='_blank' "
                f"style='color:#4f46e5;font-size:11px;margin-right:6px;' "
                f"title='URL at failure: {url_text}'>🔗</a>"
            )
        replay_target = None
        if r.video_path:
            replay_target = r.video_path
        elif r.artifact_folder:
            cand = Path("reports/failures") / r.artifact_folder / "recording.webm"
            if cand.exists():
                replay_target = cand
        if replay_target:
            link_html_parts.append(
                f"<a href='{_href_from_dashboard(replay_target)}' target='_blank' "
                f"style='color:#7c3aed;font-size:11px;margin-right:6px;font-weight:600;' "
                f"title='Replay video'>▶</a>"
            )
        links_html = "".join(link_html_parts) or "<span style='color:#94a3b8;'>—</span>"

        # Diagnosis + suggested fix subtext
        details_parts: list[str] = []
        if verdict and verdict.get("reasoning"):
            r_text = verdict["reasoning"]
            details_parts.append(
                f"<div style='font-size:11px;color:#475569;margin-top:2px;' title='{r_text}'>"
                f"<span style='color:#94a3b8;font-weight:600;'>↪ AI says:</span> "
                f"<span style='color:#b91c1c;'>{_truncate(r_text, 200)}</span></div>"
            )
        if triage.get("status") == "TRIAGED":
            d_text = triage.get("diagnosis", "")
            f_text = triage.get("suggested_fix", "")
            if d_text:
                details_parts.append(
                    f"<div style='font-size:11px;color:#475569;margin-top:2px;' title='{d_text}'>"
                    f"<span style='color:#94a3b8;font-weight:600;'>↪ Diagnosis:</span> "
                    f"<span style='color:#7f1d1d;'>{_truncate(d_text, 200)}</span></div>"
                )
            if f_text:
                details_parts.append(
                    f"<div style='font-size:11px;color:#475569;margin-top:2px;' title='{f_text}'>"
                    f"<span style='color:#94a3b8;font-weight:600;'>↪ Suggested fix:</span> "
                    f"<span style='color:#14532d;'>{_truncate(f_text, 200)}</span></div>"
                )
        # Show the full prompt as a third line so the user can see the
        # actual request without hovering.
        if prompt_text:
            details_parts.append(
                f"<div style='font-size:11px;color:#475569;margin-top:2px;' title='{prompt_text}'>"
                f"<span style='color:#94a3b8;font-weight:600;'>↪ Full prompt:</span> "
                f"<span style='font-style:italic;'>{_truncate(prompt_text, 240)}</span></div>"
            )
        details_html = "".join(details_parts)

        # Column separator style — light border on the right edge of every
        # column except the last gives the "|" delimiter look the user asked
        # for. Inline so it inherits cleanly with the existing styling.
        # Excel-style cell border on ALL four sides, in the section's red
        # theme so it stays visible against the pink row background.
        # (The global table th/td rule paints a light-grey grid by default,
        # but that washes out on #fef2f2 — explicit override here.)
        SEP = "border:1px solid #fecaca;"
        # Two rows per issue: data row + details row spanning all columns.
        # data-rootcause lets the drill-down chips filter; the matching
        # details row carries the same attribute so the pair hides together.
        body_html.append(
            f"<tr class='issue-row' data-rootcause='{_html_attr(rootcause)}' "
            "style='background:#fef2f2;border-top:1px solid #fecaca;'>"
            f"<td style='padding:8px 10px;font-size:12px;font-family:monospace;"
            f"vertical-align:top;word-break:break-word;{SEP}'>{r.method}</td>"
            f"<td style='padding:8px 10px;font-size:11px;color:#64748b;vertical-align:top;{SEP}'>{r.category}</td>"
            f"<td style='padding:8px 10px;vertical-align:top;{SEP}'>{_status_badge(r.status)}</td>"
            f"<td style='padding:8px 10px;font-size:11px;color:#64748b;text-align:right;vertical-align:top;{SEP}'>{_fmt_ms(r.duration_ms)}</td>"
            f"<td style='padding:8px 10px;font-size:11px;color:#1e293b;vertical-align:top;"
            f"max-width:280px;font-style:italic;{SEP}' title='{prompt_text}'>{prompt_short or '—'}</td>"
            f"<td style='padding:8px 10px;font-size:11px;color:#1e293b;vertical-align:top;"
            f"max-width:240px;{SEP}'>{built_text}</td>"
            f"<td style='padding:8px 10px;vertical-align:top;white-space:nowrap;{SEP}'>"
            f"{triage_html}{verdict_html}</td>"
            f"<td style='padding:8px 10px;vertical-align:top;white-space:nowrap;font-size:13px;{SEP}'>{links_html}</td>"
            "</tr>"
        )
        if details_html:
            body_html.append(
                f"<tr class='issue-row-details' data-rootcause='{_html_attr(rootcause)}' "
                f"style='background:#fff5f5;'>"
                f"<td colspan='8' style='padding:6px 14px 12px 14px;"
                f"border:1px solid #fecaca;border-top:1px dashed #fecaca;'>"
                f"{details_html}</td></tr>"
            )

        # Accumulate TSV / Markdown rows for the copy buttons.
        built_flat = ""
        if trigger_seen:
            built_flat = f"Trigger: {trigger_seen}"
        if actions_seen:
            built_flat += (" | " if built_flat else "") + f"Actions: {', '.join(actions_seen)}"
        verdict_flat = ""
        v_stat = verdict.get("status", "") if verdict else ""
        if v_stat:
            verdict_flat = f"AI:{v_stat}"
        triage_flat = ""
        if triage.get("status") == "TRIAGED":
            triage_flat = (
                f"{triage.get('category','UNKNOWN')} ({triage.get('severity','minor')})"
            )
        diagnosis_flat = triage.get("diagnosis", "") if triage else ""
        suggest_flat   = triage.get("suggested_fix", "") if triage else ""
        if not diagnosis_flat and verdict and verdict.get("reasoning"):
            diagnosis_flat = verdict["reasoning"]

        tsv_cells = [
            r.method, r.category, r.status, _fmt_ms(r.duration_ms),
            prompt_text or "",
            built_flat,
            verdict_flat,
            triage_flat,
            diagnosis_flat,
            suggest_flat,
            url_text or "",
        ]
        tsv_rows.append("\t".join(_tsv_escape(c) for c in tsv_cells))
        md_rows.append("| " + " | ".join(_md_escape(c) for c in tsv_cells) + " |")
        # HTML row — inline styles so the table survives the clipboard
        # journey and renders as a real table in the destination app.
        html_rows.append(
            "<tr>"
            + "".join(
                f"<td style='border:1px solid #cbd5e1;padding:6px 10px;"
                f"font-family:Arial,sans-serif;font-size:11px;vertical-align:top;'>"
                f"{_h(c)}</td>"
                for c in tsv_cells
            )
            + "</tr>"
        )

    header_cols = [
        ("Test",          "left"),
        ("Category",      "left"),
        ("Status",        "left"),
        ("Duration",      "right"),
        ("Prompt asked",  "left"),
        ("Copilot built", "left"),
        ("Verdict",       "left"),
        ("Links",         "left"),
    ]
    # Every header cell gets a full Excel-style border in the section's red
    # theme. Background is slightly darker than rows so the header is visually
    # distinct (same idea as Excel freezing the first row).
    head = (
        "<tr style='background:#fee2e2;'>"
        + "".join(
            f"<th style='text-align:{align};padding:8px 10px;font-size:11px;"
            f"color:#7f1d1d;font-weight:700;text-transform:uppercase;"
            f"letter-spacing:0.5px;border:1px solid #fecaca;"
            f"'>{label}</th>"
            for label, align in header_cols
        )
        + "</tr>"
    )

    # Hidden text blobs for the clipboard. We use <pre> to preserve
    # newlines/tabs without HTML reinterpretation. Wrap in display:none.
    tsv_blob = _html_mod.escape("\n".join(tsv_rows))
    md_blob  = _html_mod.escape("\n".join(md_rows))
    # Full HTML <table> blob — written to the clipboard as text/html so
    # destinations that understand rich text (Word, Outlook, Gmail, Notion,
    # Confluence WYSIWYG, ChatGPT, this chat box) paste it as a real table.
    html_blob_raw = (
        "<table style='border-collapse:collapse;font-family:Arial,sans-serif;'>"
        + "".join(html_rows)
        + "</table>"
    )
    html_blob = _html_mod.escape(html_blob_raw)

    copy_buttons = """
      <div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap;'>
        <button id='copy-issues-rich-btn'
                onclick='copyIssues("html")'
                style='font-size:11px;font-weight:700;padding:6px 12px;
                       border-radius:6px;border:1px solid #b91c1c;
                       background:#b91c1c;color:#fff;cursor:pointer;'
                title='Copy as a real table (best for Word / Outlook / Gmail / Notion / Confluence / chat apps)'>
          📋 Copy as Table
        </button>
        <button id='copy-issues-tsv-btn'
                onclick='copyIssues("tsv")'
                style='font-size:11px;font-weight:600;padding:6px 12px;
                       border-radius:6px;border:1px solid #fecaca;
                       background:#fff;color:#b91c1c;cursor:pointer;'
                title='Copy as TSV (best for Excel / Google Sheets)'>
          📋 TSV
        </button>
        <button id='copy-issues-md-btn'
                onclick='copyIssues("md")'
                style='font-size:11px;font-weight:600;padding:6px 12px;
                       border-radius:6px;border:1px solid #fecaca;
                       background:#fff;color:#b91c1c;cursor:pointer;'
                title='Copy as Markdown (best for Jira / GitHub / Confluence source mode)'>
          📋 Markdown
        </button>
      </div>
    """

    hidden_data = (
        f"<pre id='issues-tsv-blob'  style='display:none;'>{tsv_blob}</pre>"
        f"<pre id='issues-md-blob'   style='display:none;'>{md_blob}</pre>"
        f"<pre id='issues-html-blob' style='display:none;'>{html_blob}</pre>"
    )

    # Root-cause drill-down chips. Clicking a chip filters the issues table
    # to that triage category; "All" clears. Colours mirror _triage_badge.
    cat_palette = {
        "PRODUCT_BUG":   ("#b91c1c", "#fee2e2"),
        "LOCATOR_DRIFT": ("#92400e", "#fef3c7"),
        "FLAKE":         ("#0369a1", "#dbeafe"),
        "INFRA":         ("#7c3aed", "#ede9fe"),
        "TEST_BUG":      ("#9f1239", "#fce7f3"),
        "UNKNOWN":       ("#64748b", "#f1f5f9"),
        "UNCLASSIFIED":  ("#64748b", "#f1f5f9"),
    }
    chip_parts = [
        "<button class='rc-chip' data-rc='' onclick=\"filterRootCause('',this)\" "
        "style='font-size:11px;font-weight:700;padding:3px 10px;border-radius:9999px;"
        "border:1px solid #cbd5e1;background:#1e293b;color:#fff;cursor:pointer;'>"
        f"All ({len(issue_rows)})</button>"
    ]
    for cat, n in sorted(rootcause_counts.items(), key=lambda kv: -kv[1]):
        color, bg = cat_palette.get(cat, ("#64748b", "#f1f5f9"))
        chip_parts.append(
            f"<button class='rc-chip' data-rc='{_html_attr(cat)}' "
            f"onclick=\"filterRootCause('{_html_attr(cat)}',this)\" "
            f"style='font-size:11px;font-weight:700;padding:3px 10px;border-radius:9999px;"
            f"border:1px solid {color}44;background:{bg};color:{color};cursor:pointer;'>"
            f"{cat} ({n})</button>"
        )
    rootcause_chips = (
        "<div class='no-print' style='display:flex;gap:6px;flex-wrap:wrap;"
        "align-items:center;margin-bottom:12px;'>"
        "<span style='font-size:11px;color:#94a3b8;font-weight:600;margin-right:4px;'>"
        "Root cause:</span>" + "".join(chip_parts) + "</div>"
    )

    copy_script = """
      <script>
      (function () {
        var MAP = {
          tsv:  { src: 'issues-tsv-blob',  btn: 'copy-issues-tsv-btn'  },
          md:   { src: 'issues-md-blob',   btn: 'copy-issues-md-btn'   },
          html: { src: 'issues-html-blob', btn: 'copy-issues-rich-btn' }
        };

        window.copyIssues = function (kind) {
          var m = MAP[kind] || MAP.tsv;
          var src = document.getElementById(m.src);
          var btn = document.getElementById(m.btn);
          if (!src || !btn) return;
          var text     = src.textContent;
          var original = btn.textContent;
          var flash = function () {
            btn.textContent = '✓ Copied!';
            setTimeout(function () { btn.textContent = original; }, 1500);
          };

          if (kind === 'html') {
            // Rich-text path: put BOTH text/html and text/plain on the
            // clipboard. Word / Outlook / Gmail / Notion / Confluence-
            // WYSIWYG / most chat boxes read text/html and render the
            // table. Plain-text destinations get the TSV as fallback.
            var tsvSrc = document.getElementById('issues-tsv-blob');
            var plain  = tsvSrc ? tsvSrc.textContent : text;
            if (window.ClipboardItem && navigator.clipboard && navigator.clipboard.write) {
              try {
                var blobHtml = new Blob([text],  { type: 'text/html'  });
                var blobText = new Blob([plain], { type: 'text/plain' });
                navigator.clipboard.write([
                  new ClipboardItem({ 'text/html': blobHtml, 'text/plain': blobText })
                ]).then(flash, function () { execHtmlFallback(text, btn, original); });
                return;
              } catch (e) { /* fall through */ }
            }
            execHtmlFallback(text, btn, original);
            return;
          }

          // Plain text path (tsv / md)
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(flash, function () {
              fallbackCopy(text, btn, original);
            });
          } else {
            fallbackCopy(text, btn, original);
          }
        };

        function fallbackCopy(text, btn, original) {
          var ta = document.createElement('textarea');
          ta.value = text;
          ta.style.position = 'fixed';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.focus(); ta.select();
          try { document.execCommand('copy'); } catch (e) {}
          document.body.removeChild(ta);
          btn.textContent = '✓ Copied!';
          setTimeout(function () { btn.textContent = original; }, 1500);
        }

        // Rich-text fallback for older browsers: render the HTML into
        // a temporary contenteditable div, select it, then execCommand
        // copy. The browser carries text/html to the clipboard for us.
        function execHtmlFallback(html, btn, original) {
          var holder = document.createElement('div');
          holder.contentEditable = 'true';
          holder.innerHTML = html;
          holder.style.position = 'fixed';
          holder.style.left = '-9999px';
          document.body.appendChild(holder);
          var range = document.createRange();
          range.selectNodeContents(holder);
          var sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
          try { document.execCommand('copy'); } catch (e) {}
          sel.removeAllRanges();
          document.body.removeChild(holder);
          btn.textContent = '✓ Copied!';
          setTimeout(function () { btn.textContent = original; }, 1500);
        }
      })();
      </script>
    """

    issues_toggle_script = """
      <script>
      (function () {
        var KEY = 'flowguard-issues-collapsed';
        window.toggleIssuesSection = function () {
          var sec = document.getElementById('issues-section');
          if (!sec) return;
          var collapsed = sec.classList.toggle('issues-collapsed');
          var caret = document.getElementById('issues-caret');
          if (caret) caret.textContent = collapsed ? '▸' : '▾';
          try { localStorage.setItem(KEY, collapsed ? '1' : '0'); } catch (e) {}
        };
        document.addEventListener('DOMContentLoaded', function () {
          try {
            if (localStorage.getItem(KEY) === '1') {
              var sec = document.getElementById('issues-section');
              if (sec) {
                sec.classList.add('issues-collapsed');
                var caret = document.getElementById('issues-caret');
                if (caret) caret.textContent = '▸';
              }
            }
          } catch (e) {}
        });
      })();
      </script>
      <style>
        .issues-collapsed #issues-body { display: none !important; }
      </style>
    """

    return f"""
    <div id="issues-section" class="issues-section no-print-controls"
         style="background:#fff;border:1px solid #fecaca;border-radius:12px;
                padding:18px 22px;margin-bottom:20px;">
      <div style="display:flex;align-items:center;justify-content:space-between;
                  margin-bottom:10px;gap:12px;flex-wrap:wrap;">
        <div onclick="toggleIssuesSection()"
             style="font-size:14px;font-weight:700;color:#b91c1c;cursor:pointer;
                    user-select:none;display:flex;align-items:center;gap:8px;"
             title="Click to expand / collapse the Issues table">
          <span id="issues-caret" style="display:inline-block;font-size:12px;
                color:#b91c1c;transition:transform .15s;">▾</span>
          🔴 Issues to Triage ({len(issue_rows)})
        </div>
        {copy_buttons}
      </div>
      <div id="issues-body">
        <div style="font-size:11px;color:#64748b;margin-bottom:10px;">
          📷 Failure screenshot · 🖼️ Canvas · 🩺 Triage JSON · 🔗 URL at failure · ▶ Replay
        </div>
        {rootcause_chips}
        <table id="issues-table" style="width:100%;border-collapse:collapse;">
          <thead>{head}</thead>
          <tbody>{''.join(body_html)}</tbody>
        </table>
      </div>
      {hidden_data}
      {copy_script}
      {issues_toggle_script}
    </div>
    """


def _render_browser_tabs() -> str:
    """
    Render a tab strip showing all available browser dashboards. Auto-
    detects sibling per-browser folders under reports/trend/ (created by
    parallel runs via --browser firefox / --browser webkit). The current
    browser's tab is marked active; the others link to their dashboard.

    Layout assumption (matches conftest._report_root):
      reports/trend/dashboard.html             ← chromium (root)
      reports/trend/firefox/dashboard.html     ← firefox
      reports/trend/webkit/dashboard.html      ← webkit
    """
    current = DASHBOARD_PATH.resolve()
    trend_root = Path("reports/trend").resolve()

    # Determine which engine we're currently rendering.
    if current.parent == trend_root:
        active_engine = "chromium"
    else:
        active_engine = current.parent.name  # firefox / webkit / ...

    # Discover all sibling dashboards that exist on disk.
    available: list[tuple[str, Path]] = []
    chromium_dash = trend_root / "dashboard.html"
    if chromium_dash.exists() or active_engine == "chromium":
        available.append(("chromium", chromium_dash))
    for engine in ("firefox", "webkit"):
        sub = trend_root / engine / "dashboard.html"
        if sub.exists() or active_engine == engine:
            available.append((engine, sub))

    # Skip the strip entirely if only one dashboard exists — no value yet.
    if len(available) <= 1:
        return ""

    icons = {"chromium": "🟢", "firefox": "🦊", "webkit": "🧭"}
    labels = {"chromium": "Chromium", "firefox": "Firefox", "webkit": "WebKit"}

    tabs: list[str] = []
    for engine, dash_path in available:
        is_active = (engine == active_engine)
        # Build relative href from this dashboard to the target dashboard.
        if is_active:
            href = "#"
        else:
            try:
                href = str(dash_path.resolve().relative_to(current.parent))
            except ValueError:
                # Different drives / not relative — fall back to absolute file URL.
                href = f"file://{dash_path.resolve()}"
        if is_active:
            style = (
                "background:#0f172a;color:#fff;padding:8px 18px;"
                "border-radius:8px 8px 0 0;font-weight:700;font-size:13px;"
                "border:1px solid #0f172a;border-bottom:none;"
                "display:inline-block;text-decoration:none;cursor:default;"
            )
        else:
            style = (
                "background:#f1f5f9;color:#475569;padding:8px 18px;"
                "border-radius:8px 8px 0 0;font-weight:600;font-size:13px;"
                "border:1px solid #cbd5e1;border-bottom:none;margin-right:2px;"
                "display:inline-block;text-decoration:none;"
            )
        tabs.append(
            f"<a href='{href}' style=\"{style}\">"
            f"{icons.get(engine, '🌐')} {labels.get(engine, engine.title())}"
            f"</a>"
        )

    return (
        "<div style='border-bottom:2px solid #0f172a;margin-bottom:18px;"
        "padding-bottom:0;display:flex;align-items:flex-end;gap:0;'>"
        + "".join(tabs)
        + "</div>"
    )


def _render_login_routes_section() -> str:
    """
    Table showing which login surface each test hit:
      - legacy-appypie : URL was accounts.appypie.com/login  → legacy form ran
      - flozic-authv2  : URL was elsewhere → went straight to flozic authv2
    """
    try:
        from utils.health_tracker import get_login_routes
        routes = get_login_routes()
    except Exception:
        routes = []

    if not routes:
        return ""

    legacy_count = sum(1 for _, r, _ in routes if r == "legacy-appypie")
    flozic_count = sum(1 for _, r, _ in routes if r == "flozic-authv2")

    rows = []
    for test_name, route, url in routes:
        if route == "legacy-appypie":
            badge = ("<span style='background:#fef3c7;color:#92400e;"
                     "padding:2px 8px;border-radius:10px;font-size:11px;"
                     "font-weight:600'>LEGACY accounts.appypie.com</span>")
        elif route == "flozic-authv2":
            badge = ("<span style='background:#dcfce7;color:#15803d;"
                     "padding:2px 8px;border-radius:10px;font-size:11px;"
                     "font-weight:600'>FLOZIC authv2</span>")
        else:
            badge = ("<span style='background:#f1f5f9;color:#475569;"
                     "padding:2px 8px;border-radius:10px;font-size:11px;"
                     "font-weight:600'>UNKNOWN</span>")
        rows.append(
            f"<tr>"
            f"<td style='padding:6px 12px;font-family:monospace;font-size:12px;"
            f"overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>{test_name}</td>"
            f"<td style='padding:6px 12px;white-space:nowrap'>{badge}</td>"
            f"<td style='padding:6px 12px;font-family:monospace;font-size:11px;color:#64748b;"
            f"overflow:hidden;text-overflow:ellipsis;white-space:nowrap' title='{url}'>{url}</td>"
            f"</tr>"
        )

    return f"""
    <div class="card">
      <h2>🔐 Login Route Observations
        <span style="font-size:12px;font-weight:400;color:#64748b;margin-left:8px">
          legacy accounts.appypie.com: <b style="color:#92400e">{legacy_count}</b> ·
          flozic authv2: <b style="color:#15803d">{flozic_count}</b>
        </span>
      </h2>
      <div style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;table-layout:fixed">
        <colgroup>
          <col style="width:44%">
          <col style="width:18%">
          <col style="width:38%">
        </colgroup>
        <thead><tr style="background:#f8fafc">
          <th style="text-align:left;padding:8px 12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Test</th>
          <th style="text-align:left;padding:8px 12px;white-space:nowrap">Route</th>
          <th style="text-align:left;padding:8px 12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">URL at Login</th>
        </tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
      </div>
    </div>
    """


def _render_recurring_failures_section(clusters) -> str:
    """
    Standalone dashboard section listing recurring failure clusters
    ranked by their recurring_score.  Hidden when no clusters exist.
    """
    visible = [c for c in clusters if c.recurring_score >= 0.05]
    if not visible:
        return ""
    # Established patterns (RECURRING/FLAKY/CONSISTENT/OBSERVED) first, NEW last —
    # so a wall of one-off NEW failures never buries a real recurring bug.
    _order = {"CONSISTENT_FAILURE": 0, "RECURRING": 1, "FLAKY": 2, "OBSERVED": 3, "NEW": 4}
    visible = sorted(visible, key=lambda c: (_order.get(getattr(c, "label", "NEW"), 5),
                                             -c.recurring_score))
    from collections import Counter
    counts = Counter(getattr(c, "label", "NEW") for c in visible)
    summary = " · ".join(f"{n} {lbl.replace('_', ' ').title()}"
                         for lbl, n in sorted(counts.items(), key=lambda kv: _order.get(kv[0], 5)))
    rows = []
    for c in visible[:20]:  # cap at top 20 to keep the page bounded
        label = getattr(c, "label", "NEW")
        icon, color, bg = _LABEL_STYLE.get(label, _LABEL_STYLE["NEW"])
        passes = getattr(c, "passes_in_history", 0)
        rows.append(
            "<tr>"
            f"<td style='padding:6px 12px;font-family:monospace;font-size:12px'>{c.sample_method}</td>"
            f"<td style='padding:6px 12px;font-size:12px;color:#64748b'>{c.sample_feature or '—'}</td>"
            f"<td style='padding:6px 12px;text-align:center;font-size:12px'>"
            f"<span style='display:inline-block;padding:2px 8px;border-radius:9999px;"
            f"font-size:11px;font-weight:700;background:{bg};color:{color}'>"
            f"{icon} {label.replace('_', ' ')}</span></td>"
            f"<td style='padding:6px 12px;text-align:center;font-size:12px;color:#64748b'>"
            f"{c.occurrences_in_history} fail / {passes} pass of {c.runs_seen or c.window_size} runs</td>"
            f"<td style='padding:6px 12px;text-align:right;font-size:12px;color:#64748b'>{c.recurring_score:.2f}</td>"
            f"<td style='padding:6px 12px;font-size:11px;color:#94a3b8'>{c.last_seen}</td>"
            "</tr>"
        )
    table = "".join(rows)
    return f"""
    <div class="dark-surface" style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;
                padding:18px 22px;margin-bottom:20px;">
      <div style="display:flex;align-items:baseline;justify-content:space-between;
                  margin-bottom:6px;">
        <div style="font-size:14px;font-weight:700;color:#1e293b;">
          🔁 Failure History
        </div>
        <div style="font-size:11px;color:#64748b;">
          last {visible[0].window_size} runs · {summary}
        </div>
      </div>
      <div style="font-size:11px;color:#94a3b8;margin-bottom:10px;">
        NEW = failed once · OBSERVED = twice · RECURRING = 3+ · FLAKY = passes and fails · CONSISTENT = fails ≥90%
      </div>
      <table style="width:100%;border-collapse:collapse;">
        <thead>
          <tr style="background:#f8fafc;">
            <th style='padding:6px 12px;text-align:left;font-size:11px;color:#64748b;
                       font-weight:600;text-transform:uppercase;letter-spacing:0.5px'>Test</th>
            <th style='padding:6px 12px;text-align:left;font-size:11px;color:#64748b;
                       font-weight:600;text-transform:uppercase;letter-spacing:0.5px'>Feature</th>
            <th style='padding:6px 12px;text-align:center;font-size:11px;color:#64748b;
                       font-weight:600;text-transform:uppercase;letter-spacing:0.5px'>Type</th>
            <th style='padding:6px 12px;text-align:center;font-size:11px;color:#64748b;
                       font-weight:600;text-transform:uppercase;letter-spacing:0.5px'>History</th>
            <th style='padding:6px 12px;text-align:right;font-size:11px;color:#64748b;
                       font-weight:600;text-transform:uppercase;letter-spacing:0.5px'>Score</th>
            <th style='padding:6px 12px;text-align:left;font-size:11px;color:#64748b;
                       font-weight:600;text-transform:uppercase;letter-spacing:0.5px'>Last seen</th>
          </tr>
        </thead>
        <tbody>{table}</tbody>
      </table>
    </div>
    """


def _render_feature_rows(by_feature: dict) -> str:
    rows = []
    for feat, counts in sorted(by_feature.items()):
        total  = counts["pass"] + counts["fail"] + counts["skip"]
        rate   = round(counts["pass"] / total * 100) if total else 0
        colour = "#16a34a" if rate == 100 else ("#ca8a04" if rate >= 80 else "#dc2626")
        bar    = f"<div style='height:6px;border-radius:3px;background:#e2e8f0;width:120px;display:inline-block;vertical-align:middle'><div style='height:6px;border-radius:3px;background:{colour};width:{rate}%'></div></div>"
        rows.append(
            f"<tr>"
            f"<td style='padding:6px 12px;font-size:13px'>{feat}</td>"
            f"<td style='padding:6px 12px;font-size:12px;text-align:center'>{counts['pass']}</td>"
            f"<td style='padding:6px 12px;font-size:12px;text-align:center;color:#dc2626'>{counts['fail']}</td>"
            f"<td style='padding:6px 12px'>{bar} <span style='font-size:11px;color:{colour};margin-left:6px'>{rate}%</span></td>"
            f"</tr>"
        )
    return "\n".join(rows)


def _render_trend_rows(trend: list[dict]) -> str:
    # Run history shows EXECUTED runs only. An EMPTY session (0 tests — unit-only
    # or aborted) is an audit-trail row, not a test result; mixing them in made
    # the history read as a wall of 0/0 WARNING junk.
    trend = [r for r in trend
             if r.get("run_status", "COMPLETED") == "COMPLETED" and (r.get("total") or 0) > 0]
    if not trend:
        return "<tr><td colspan='5' style='text-align:center;color:#94a3b8;padding:20px'>No trend data yet</td></tr>"
    rows = []
    for run in reversed(trend[-10:]):
        colour = {"READY": "#16a34a", "WARNING": "#ca8a04", "AT_RISK": "#ea580c", "BLOCKED": "#dc2626"}.get(run["status"], "#6b7280")
        rows.append(
            f"<tr>"
            f"<td style='padding:6px 12px;font-size:12px'>{run['label']}</td>"
            f"<td style='padding:6px 12px;font-size:12px;text-align:center'>{run['total']}</td>"
            f"<td style='padding:6px 12px;font-size:12px;text-align:center'>{run['passed']}</td>"
            f"<td style='padding:6px 12px;font-size:12px;text-align:center;color:#dc2626'>{run['failed']}</td>"
            f"<td style='padding:6px 12px;font-size:12px;font-weight:700;color:{colour}'>{run['status']}</td>"
            f"</tr>"
        )
    return "\n".join(rows)


def _render(
    records: list[TestRecord],
    stats: dict,
    decision: Any,
    trend: list[dict],
    started_at_ms: int,
    *,
    health_score: int = 0,
    layered: Any = None,
    mobile_findings: list[dict] | None = None,
    clusters: list[ErrorCluster] | None = None,
) -> str:
    run_time  = datetime.fromtimestamp(started_at_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Executive summary — DETERMINISTIC, composed from the run's real numbers
    # (see utils/ai_exec_summary). It is fed the mobile finding/coverage-gap
    # context so it cannot claim "all requirements met / recommended for
    # release" off the pytest pass count while blind to product findings.
    exec_html = ""
    try:
        from utils.ai_exec_summary import summarize as _exec_summarize
        _mf = _mobile_findings()
        _mobile_ctx = None
        if _mf:
            from utils.ai_mobile_triage import group_findings
            _actionable = sum(
                1 for x in _mf
                if (x.get("severity") or "").lower() in ("blocker", "major", "minor"))
            _gaps = sum(
                1 for x in _mf
                if "UNTESTED" in (x.get("message") or "")
                or "UNSUPPORTED" in (x.get("message") or ""))
            _mobile_ctx = {
                "findings": _actionable,
                "patterns": len(group_findings(_mf)),
                "coverage_gaps": _gaps,
            }
        exec_summary = _exec_summarize(
            stats, records, decision.status.value, mobile=_mobile_ctx)
    except Exception as e:
        logger.warning("Exec summary failed (non-fatal): %s", e)
        exec_summary = ""
    if exec_summary:
        exec_html = (
            "<div class='tint-surface' style='background:#f8fafc;border-left:4px solid #0ea5e9;"
            "padding:14px 18px;margin-bottom:18px;border-radius:6px;'>"
            "<div style='font-size:10px;font-weight:700;color:#0284c7;"
            "text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;'>"
            "Executive Summary"
            "</div>"
            f"<div style='font-size:13px;color:#1e293b;line-height:1.5;'>{exec_summary}</div>"
            "</div>"
        )

    # Triage box — only shown when not READY
    triage_html = ""
    if decision.status != ReleaseStatus.READY:
        first_fail = next((r.method for r in records if r.status == "FAIL"), "—")
        triage_html = f"""
        <div class="tint-surface" style="background:{decision.bg};border:2px solid {decision.color}33;border-radius:12px;
                    padding:20px 24px;margin-bottom:24px">
          <div style="font-size:11px;font-weight:700;color:{decision.color};
                      text-transform:uppercase;letter-spacing:1px;margin-bottom:12px">
            🔍 30-Second Triage — What You Need To Know Right Now
          </div>
          <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:20px">
            <div>
              <div style="font-size:10px;font-weight:700;color:#94a3b8;text-transform:uppercase;
                          letter-spacing:0.8px;margin-bottom:6px">WHAT BROKE</div>
              <div style="font-size:13px;font-weight:600;color:#1e293b;font-family:monospace">{first_fail}</div>
            </div>
            <div>
              <div style="font-size:10px;font-weight:700;color:#94a3b8;text-transform:uppercase;
                          letter-spacing:0.8px;margin-bottom:6px">BLAST RADIUS</div>
              <div style="font-size:13px;font-weight:600;color:#1e293b">{stats['failed']} of {stats['total']} tests</div>
            </div>
            <div>
              <div style="font-size:10px;font-weight:700;color:#94a3b8;text-transform:uppercase;
                          letter-spacing:0.8px;margin-bottom:6px">PASS RATE</div>
              <div style="font-size:13px;font-weight:600;color:{decision.color}">{stats['pass_rate']}%</div>
            </div>
            <div>
              <div style="font-size:10px;font-weight:700;color:#94a3b8;text-transform:uppercase;
                          letter-spacing:0.8px;margin-bottom:6px">ACTION</div>
              <div style="font-size:13px;font-weight:600;color:#1e293b">{decision.description}</div>
            </div>
          </div>
        </div>
        """

    # Failure clustering across runs — read archived snapshots, group by
    # fingerprint, compute recurring scores. Skips silently if there's no
    # archive yet (first run, no snapshots/ dir).
    try:
        from utils.failure_clustering import build_clusters
        recurring_clusters = build_clusters(last_n=30)
    except Exception as e:
        logger.warning("Failure clustering failed (non-fatal): %s", e)
        recurring_clusters = []
    recurring_by_method = {c.sample_method: c for c in recurring_clusters}
    recurring_html      = _render_recurring_failures_section(recurring_clusters)
    issues_html         = _render_issues_section(records)

    test_rows    = _render_test_rows(records, recurring_by_method=recurring_by_method)
    toolbar_html = _render_test_results_toolbar(records)
    filter_sort_script = _filter_sort_script()
    feature_rows = _render_feature_rows(stats["by_feature"])
    trend_rows   = _render_trend_rows(trend)
    health_html  = _render_health_section(health_score, layered) if layered else ""
    mobile_html  = _render_mobile_section(mobile_findings or [], layered)
    cluster_html = _render_cluster_section(clusters or [])
    login_routes_html = _render_login_routes_section()
    browser_tabs_html = _render_browser_tabs()

    # Prominent link to the combined cross-engine (Hybrid) report. It always
    # lives at reports/trend/combined-report.html; compute a relative href from
    # THIS engine's dashboard location (chromium: sibling; webkit: ../).
    import os as _os
    try:
        _combined_href = _os.path.relpath(
            Path("reports/trend/combined-report.html"), DASHBOARD_PATH.parent)
    except Exception:
        _combined_href = "combined-report.html"
    combined_banner = (
        f"<a href='{_combined_href}' class='no-print' "
        "style='display:block;margin-bottom:20px;padding:12px 18px;border-radius:10px;"
        "background:linear-gradient(90deg,#eef2ff,#faf5ff);border:1px solid #c7d2fe;"
        "color:#3730a3;font-weight:700;font-size:14px;text-decoration:none'>"
        "📊 Open the Combined Cross-Engine Report (Chromium + WebKit, Hybrid layout) &rarr;"
        "<span style='display:block;font-weight:500;font-size:12px;color:#6366f1;margin-top:2px'>"
        "This per-engine dashboard shows one engine; the combined report cross-references both with "
        "provenance, an engine-comparison matrix, and per-section PDF export.</span></a>"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flozic FlowGuard — Test Dashboard</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
          background: #f8fafc; color: #1e293b; }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
  h2 {{ font-size: 15px; font-weight: 700; color: #475569; margin-bottom: 12px;
        text-transform: uppercase; letter-spacing: 0.5px; }}
  .card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 12px;
           padding: 20px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
  /* Excel-style grid: every table on the dashboard shows visible row +
     column lines around every cell. Single shared rule so it applies to
     Test Results, Issues, Recurring Failures, Feature Coverage, JS
     Clusters, Run History — every table without per-section overrides. */
  table {{ width: 100%; border-collapse: collapse; border: 1px solid #cbd5e1; }}
  th, td {{ border: 1px solid #e2e8f0; }}
  th {{ text-align: left; padding: 8px 12px; font-size: 11px; font-weight: 700;
        color: #475569; text-transform: uppercase; letter-spacing: 0.8px;
        background: #f8fafc;
        border-bottom: 2px solid #cbd5e1; }}
  /* Dark-mode gridlines */
  body.dark table {{ border-color: #475569 !important; }}
  body.dark th, body.dark td {{ border-color: #334155 !important; }}
  body.dark th {{ background: #1e293b !important; border-bottom-color: #475569 !important; }}
  a {{ text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  /* ── Collapsible sections ──────────────────────────────────────── */
  .collapse-toggle {{ cursor: pointer; user-select: none; }}
  .collapse-caret {{ display: inline-block; width: 14px; transition: transform .15s;
                     color: #94a3b8; font-size: 11px; }}
  .section-collapsed .collapse-body {{ display: none !important; }}
  .section-collapsed .collapse-caret {{ transform: rotate(-90deg); }}

  /* ── Screenshot hover preview popover ──────────────────────────── */
  #hover-preview {{ position: fixed; z-index: 99999; display: none;
                    pointer-events: none; border: 2px solid #cbd5e1;
                    border-radius: 8px; box-shadow: 0 10px 30px rgba(0,0,0,.35);
                    background: #fff; padding: 3px; max-width: 460px; max-height: 320px; }}
  #hover-preview img {{ max-width: 452px; max-height: 312px; display: block; border-radius: 5px; }}

  /* ── Dark mode ─────────────────────────────────────────────────── */
  body.dark {{ background: #0f172a !important; color: #e2e8f0 !important; }}
  body.dark .card {{ background: #1e293b !important; border-color: #334155 !important;
                     box-shadow: 0 1px 3px rgba(0,0,0,.4) !important; }}
  body.dark h2 {{ color: #cbd5e1 !important; }}
  body.dark th {{ color: #94a3b8 !important; border-bottom-color: #334155 !important; }}
  body.dark tr:not(:last-child) td {{ border-bottom-color: #334155 !important; }}
  /* Re-theme the inline-styled section surfaces + neutral text in dark mode */
  body.dark .issues-section {{ background: #1e293b !important; border-color: #7f1d1d !important; }}
  body.dark .dark-surface {{ background: #1e293b !important; border-color: #334155 !important; }}
  body.dark td[style*="color:#64748b"],
  body.dark td[style*="color:#1e293b"],
  body.dark div[style*="color:#1e293b"],
  body.dark div[style*="color:#0f172a"] {{ color: #e2e8f0 !important; }}
  body.dark select, body.dark input, body.dark button.theme-aware {{
    background: #0f172a !important; color: #e2e8f0 !important; border-color: #334155 !important; }}

  /* ── Dark mode: large tinted surfaces ──────────────────────────────
     The stat cards, health cells, exec-summary panel and decision pills
     set a pale tint (#eef2ff / #dcfce7 / #f8fafc / score_band bg) as an
     INLINE style, so it beats any plain CSS rule. Left alone in dark mode
     you get a near-white panel whose text has been lightened by the rules
     above — i.e. invisible. Swap the surface, keep the accent border.     */
  body.dark .tint-surface {{
    background: #172135 !important;
    border-color: #334155 !important; }}
  /* The exec-summary panel's left accent bar should stay coloured. */
  body.dark .tint-surface[style*="border-left"] {{
    border-left-color: #0ea5e9 !important; }}

  /* ── Dark mode: accent-colour contrast lift ────────────────────────
     These hexes are chosen for contrast against a PALE tint, so on #172135
     the darkest of them (#14532d, #92400e) are effectively invisible. Each
     is mapped to the same hue several steps lighter, preserving the colour
     coding rather than flattening everything to one neutral.

     SOURCE OF TRUTH — these values are not invented here:
       utils/layered_health_scores.score_band()  → the 5 band colours
       utils/risk_interpreter                    → the decision colours
     If a colour is added or changed there, add the matching row here or it
     will silently render unreadable in dark mode.
     The [style*=...] idiom matches the neutral-text rules above, and is
     needed because these colours are applied inline.                       */
  /* score_band(): EXCELLENT / HEALTHY / FAIR / POOR / CRITICAL */
  body.dark [style*="color:#14532d"] {{ color: #4ade80 !important; }}
  body.dark [style*="color:#166534"] {{ color: #4ade80 !important; }}
  body.dark [style*="color:#713f12"] {{ color: #fbbf24 !important; }}
  body.dark [style*="color:#9a3412"] {{ color: #fb923c !important; }}
  /* risk_interpreter decision colours */
  body.dark [style*="color:#92400e"] {{ color: #fbbf24 !important; }}
  body.dark [style*="color:#ca8a04"] {{ color: #facc15 !important; }}
  body.dark [style*="color:#ea580c"] {{ color: #fb923c !important; }}
  body.dark [style*="color:#16a34a"] {{ color: #4ade80 !important; }}
  body.dark [style*="color:#dc2626"] {{ color: #f87171 !important; }}
  /* Section accents used across the cards */
  body.dark [style*="color:#0369a1"] {{ color: #38bdf8 !important; }}  /* pass-rate blue */
  body.dark [style*="color:#0284c7"] {{ color: #38bdf8 !important; }}  /* exec-summary label */
  body.dark [style*="color:#6366f1"] {{ color: #a5b4fc !important; }}  /* total indigo */
  body.dark [style*="color:#7c3aed"] {{ color: #c4b5fd !important; }}  /* PRODUCT domain */
  body.dark [style*="color:#b45309"] {{ color: #fbbf24 !important; }}  /* FRAMEWORK / medium */
  /* Small pale-tinted BADGES are deliberately left alone — a light chip on a
     dark page reads as intended, and re-theming them flattens the severity
     colour coding that makes the tables scannable. */

  /* ────────────────────────────────────────────────────────────────
     Print / PDF export rules — triggered by the "🖨️ Export PDF"
     button (which calls window.print()). The user picks "Save as PDF"
     in the browser's print dialog. Browsers normalise this to a PDF
     using the rules below.
     ──────────────────────────────────────────────────────────────── */
  @media print {{
    /* Make background colors / badges actually print on Chrome / Edge */
    * {{ -webkit-print-color-adjust: exact !important;
         print-color-adjust: exact !important; }}
    body {{ background: #fff !important; }}
    .container {{ max-width: 100% !important; padding: 16px !important; }}
    /* Hide interactive controls in the printed copy */
    .no-print {{ display: none !important; }}
    /* Allow long tables to break across pages cleanly */
    table {{ page-break-inside: auto; }}
    tr {{ page-break-inside: avoid; page-break-after: auto; }}
    /* The Issues section is the most important content; give it a
       page-break-before so it lands at the top of a fresh page when long */
    .issues-section {{ page-break-before: auto; }}
    /* Shrink fonts slightly for denser print */
    .card, table, td, th {{ font-size: 11px !important; }}
    /* Hide the copy buttons in the issues section but keep the section */
    .issues-section button {{ display: none !important; }}
    /* Force every <a> URL to NOT print the bracketed URL after the link
       text — browsers do this by default which makes the tables noisy */
    a[href]:after {{ content: "" !important; }}
  }}
</style>
</head>
<body>
<div class="container">

  <!-- Header -->
  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:24px">
    <div>
      <div style="font-size:22px;font-weight:800;color:#0f172a">
        Flozic FlowGuard — Test Dashboard
      </div>
      <div style="font-size:13px;color:#64748b;margin-top:4px">
        Run started: {run_time} &nbsp;·&nbsp; Generated: {generated}
      </div>
    </div>
    <div style="text-align:right">
      <div class="tint-surface" style="display:inline-block;padding:8px 18px;border-radius:8px;
                  background:{decision.bg};border:2px solid {decision.color}55">
        <span style="font-size:18px;font-weight:800;color:{decision.color}">{decision.label}</span>
      </div>
      <div class="no-print" style="margin-top:10px;display:flex;gap:8px;
           align-items:center;justify-content:flex-end;">
        <button onclick="toggleDarkMode()" class="theme-aware"
                style="font-size:12px;font-weight:600;padding:8px 14px;
                       border-radius:6px;border:1px solid #cbd5e1;
                       background:#fff;color:#0f172a;cursor:pointer;"
                title="Toggle dark / light mode">
          <span id="dark-mode-label">🌙 Dark</span>
        </button>
        <label style="font-size:11px;color:#64748b;display:flex;align-items:center;
               gap:5px;cursor:pointer;user-select:none;"
               title="Auto-reload every 10s to pick up newly generated results">
          <input type="checkbox" id="auto-reload-toggle" onchange="toggleAutoReload()">
          🔄 Auto-refresh
        </label>
        <button onclick="window.print()" class="theme-aware"
                style="font-size:12px;font-weight:600;padding:8px 16px;
                       border-radius:6px;border:1px solid #cbd5e1;
                       background:#fff;color:#0f172a;cursor:pointer;"
                title="Export this dashboard as PDF via your browser's print dialog">
          🖨️ Export PDF
        </button>
      </div>
    </div>
  </div>

  {combined_banner}

  <!-- Browser tabs (chromium / firefox / webkit) — only shown when
       multiple per-browser dashboards exist on disk. -->
  {browser_tabs_html}

  <!-- AI Executive Summary -->
  {exec_html}

  <!-- Triage box -->
  {triage_html}

  <!-- Summary cards -->
  <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:20px">
    {_summary_card("Total", str(stats["total"]), "#6366f1", "#eef2ff")}
    {_summary_card("Passed", str(stats["passed"]), "#16a34a", "#dcfce7")}
    {_summary_card("Failed", str(stats["failed"]), "#dc2626", "#fee2e2")}
    {_summary_card("Skipped", str(stats["skipped"]), "#94a3b8", "#f1f5f9")}
    {_summary_card("Pass Rate", f"{stats['pass_rate']}%", "#0369a1", "#e0f2fe")}
  </div>

  <!-- Health Score + Layered Scores -->
  {health_html}
  {mobile_html}

  <!-- JS Error Clusters -->
  {cluster_html}

  <!-- Recurring Failures Across Runs -->
  {recurring_html}

  <!-- Login route observations -->
  {login_routes_html}

  <!-- Issues to Triage — focused FAIL/INVALID table with full context -->
  {issues_html}

  <!-- Feature breakdown -->
  <div class="card">
    <h2>Feature Coverage</h2>
    <table>
      <thead><tr>
        <th>Feature</th><th style="text-align:center">Pass</th>
        <th style="text-align:center">Fail</th><th>Pass Rate</th>
      </tr></thead>
      <tbody>{feature_rows}</tbody>
    </table>
  </div>

  <!-- All test results -->
  <div class="card">
    <h2>Test Results ({stats["total"]} regression tests · {_fmt_ms(stats["total_ms"])} total)</h2>
    {toolbar_html}
    <table id="test-results-table">
      <thead><tr>
        <th class="sortable" data-sort="method"   onclick="sortTestTable('method',this)" style="cursor:pointer;user-select:none;">Test Method <span class="sort-ind"></span></th>
        <th class="sortable" data-sort="feature"  onclick="sortTestTable('feature',this)" style="cursor:pointer;user-select:none;">Feature <span class="sort-ind"></span></th>
        <th class="sortable" data-sort="category" onclick="sortTestTable('category',this)" style="cursor:pointer;user-select:none;">Category <span class="sort-ind"></span></th>
        <th class="sortable" data-sort="status"   onclick="sortTestTable('status',this)" style="cursor:pointer;user-select:none;">Status <span class="sort-ind"></span></th>
        <th class="sortable" data-sort="duration" onclick="sortTestTable('duration',this)" style="cursor:pointer;user-select:none;text-align:right;">Duration <span class="sort-ind"></span></th>
        <th>Artifacts</th>
      </tr></thead>
      <tbody id="test-results-body">{test_rows}</tbody>
    </table>
    <div id="tr-empty" style="display:none;padding:16px;text-align:center;
         color:#94a3b8;font-size:13px;">No tests match the current filters.</div>
  </div>

  <!-- Trend -->
  <div class="card">
    <h2>Run History (last 10 runs)</h2>
    <table>
      <thead><tr>
        <th>Run</th><th style="text-align:center">Total</th>
        <th style="text-align:center">Passed</th>
        <th style="text-align:center">Failed</th><th>Status</th>
      </tr></thead>
      <tbody>{trend_rows}</tbody>
    </table>
  </div>

  <div style="text-align:center;font-size:11px;color:#94a3b8;margin-top:8px;padding-bottom:24px">
    Powered by Flozic FlowGuard Test Framework
</div>

</div>
{filter_sort_script}
</body>
</html>"""


def _summary_card(label: str, value: str, color: str, bg: str) -> str:
    # `tint-surface` marks a large pale-tinted panel. The tint is an inline
    # style (so it wins over CSS), which is why dark mode needs a class hook
    # with !important to swap it — see the `body.dark .tint-surface` rule.
    return (
        f"<div class='tint-surface' style='background:{bg};border-radius:10px;padding:16px 20px'>"
        f"<div style='font-size:11px;font-weight:700;color:{color};text-transform:uppercase;"
        f"letter-spacing:0.8px;margin-bottom:6px'>{label}</div>"
        f"<div style='font-size:28px;font-weight:800;color:{color}'>{value}</div>"
        f"</div>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Health score section
# ─────────────────────────────────────────────────────────────────────────────

def _mobile_cell(layered: Any) -> str:
    """The Mobile domain cell.

    When mobile was not exercised this renders an explicit "not measured"
    tile rather than being omitted. A missing tile reads as "fine"; a grey
    NOT MEASURED tile reads as "we did not look", which is the truth.
    """
    score = getattr(layered, "mobile_health", None)
    quality = getattr(layered, "mobile_quality", None)
    cov = getattr(layered, "mobile_coverage", None)
    if score is None:
        # Insufficient coverage (quality computed but too little ran) vs never ran.
        if quality is not None and cov is not None:
            note = f"INSUFFICIENT COVERAGE ({round(cov * 100)}% executed)"
        else:
            note = "NOT MEASURED"
        return (
            "<div class='tint-surface' style='text-align:center;background:#f1f5f9;"
            "border-radius:8px;padding:14px 10px;border:1px dashed #94a3b855'>"
            "<div style='font-size:22px;font-weight:800;color:#64748b'>&mdash;</div>"
            "<div style='font-size:9px;font-weight:700;text-transform:uppercase;"
            "letter-spacing:0.6px;color:#64748b;margin-top:4px'>Mobile</div>"
            f"<div style='font-size:9px;color:#94a3b8;margin-top:2px'>{note}</div>"
            "</div>"
        )
    band_label, color, bg = score_band(score)
    counts = (
        f"{getattr(layered, 'mobile_blocker', 0)}B / "
        f"{getattr(layered, 'mobile_major', 0)}Maj / "
        f"{getattr(layered, 'mobile_minor', 0)}Min"
    )
    breakdown = (f"Quality {quality} × {round(cov * 100)}% coverage"
                 if quality is not None and cov is not None else counts)
    return (
        f"<div class='tint-surface' style='text-align:center;background:{bg};"
        f"border-radius:8px;padding:14px 10px;border:1px solid {color}33'>"
        f"<div style='font-size:30px;font-weight:800;color:{color}'>{score}</div>"
        f"<div style='font-size:9px;font-weight:700;text-transform:uppercase;"
        f"letter-spacing:0.6px;color:{color};margin-top:4px'>Mobile</div>"
        f"<div style='font-size:9px;color:#94a3b8;margin-top:2px'>{counts}</div>"
        f"<div style='font-size:9px;color:#94a3b8;margin-top:1px'>{breakdown}</div>"
        f"</div>"
    )


def _weighting_note(layered: Any) -> str:
    """Spell out the active weighting — it changes with mobile presence."""
    v = getattr(layered, "scoring_version", 1)
    if getattr(layered, "mobile_health", None) is None:
        return (
            f"Product 50% · Infrastructure 30% · Framework 20% weighted average "
            f"&rarr; Overall {layered.overall}/100 &nbsp;·&nbsp; scoring v{v}, "
            f"Mobile excluded (no mobile tests in this run)"
        )
    return (
        f"Product 40% · Infrastructure 25% · Framework 20% · Mobile 15% weighted "
        f"average &rarr; Overall {layered.overall}/100 &nbsp;·&nbsp; scoring v{v}"
    )


def _render_health_section(health_score: int, layered: Any) -> str:
    """Render the 0-100 health score + layered domain scores card."""

    def _score_cell(label: str, score: int) -> str:
        band_label, color, bg = score_band(score)
        return (
            f"<div class='tint-surface' style='text-align:center;background:{bg};border-radius:8px;"
            f"padding:14px 10px;border:1px solid {color}33'>"
            f"<div style='font-size:30px;font-weight:800;color:{color}'>{score}</div>"
            f"<div style='font-size:9px;font-weight:700;text-transform:uppercase;"
            f"letter-spacing:0.6px;color:{color};margin-top:4px'>{label}</div>"
            f"<div style='font-size:9px;color:#94a3b8;margin-top:2px'>{band_label}</div>"
            f"</div>"
        )

    # ONE number on the card. The badge used to show the legacy health_tracker
    # score (raw pass rate − JS-cluster penalties) while the footnote showed
    # the layered weighted overall — the 2026-08-19 full run rendered "69/100"
    # in the badge and "→ Overall 79/100" in the footnote of the SAME card.
    # Two scoring systems disagreeing on the primary surface is worse than
    # either alone. The layered overall is authoritative (it is what the trend
    # persists and what excludes harness faults); the legacy figure stays
    # visible but explicitly labelled.
    badge_score = layered.overall if layered is not None else health_score
    overall_label, overall_color, overall_bg = score_band(badge_score)

    return f"""
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <h2 style="margin:0">Health Score</h2>
        <div class="tint-surface" style="background:{overall_bg};border:2px solid {overall_color}55;border-radius:8px;
                    padding:6px 16px;font-size:22px;font-weight:800;color:{overall_color}">
          {badge_score}<span style="font-size:13px;font-weight:500;color:#94a3b8">/100</span>
        </div>
      </div>
      <p style="font-size:11px;color:#64748b;margin-bottom:14px">
        Overall: <strong>{overall_label}</strong> (weighted domain average, scoring
        v{getattr(layered, "scoring_version", 1)}; infrastructure/harness failures
        excluded from Product) &nbsp;·&nbsp; legacy pass-rate score:
        {health_score}/100 (counts every failure, incl. connectivity).
      </p>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px">
        {_score_cell("Product Health", layered.product_health)}
        {_score_cell("Infra Health", layered.infra_health)}
        {_score_cell("Framework Health", layered.framework_health)}
        {_mobile_cell(layered)}
      </div>
      <p style="font-size:10px;color:#94a3b8;margin-top:10px">
        {_weighting_note(layered)}
      </p>
    </div>
    """


# ─────────────────────────────────────────────────────────────────────────────
# Mobile findings section
#
# This replaces the standalone reports/mobile/*.html report. One surface, not
# two: a separate report is a second place to forget to look, and it drifted
# out of sync the moment a run overwrote it.
# ─────────────────────────────────────────────────────────────────────────────

_MOBILE_SEV_STYLE = {
    "blocker": ("#dc2626", "#fee2e2"),
    "major":   ("#ea580c", "#ffedd5"),
    "minor":   ("#ca8a04", "#fef9c3"),
    "info":    ("#0284c7", "#e0f2fe"),
}


_AI_CLS_STYLE = {
    "PRODUCT_DEFECT":        ("#dc2626", "#fee2e2"),
    "AUDITOR_ARTIFACT":      ("#7c3aed", "#ede9fe"),
    "RESPONSIVE_BY_DESIGN":  ("#16a34a", "#dcfce7"),
    "NEEDS_HUMAN":           ("#64748b", "#f1f5f9"),
}


def _render_mobile_ai_triage(total_groups: int | None = None) -> str:
    """The AI classification block. AI proposals only — severity, scoring and
    pass/fail stay deterministic. Absence is stated, never implied: when
    triage did not run the block says so and points at the session log —
    including how many pattern groups therefore went unanalysed, so a reader
    can never mistake a failed analysis for a complete one."""
    from utils.ai_mobile_triage import session_triage
    t = session_triage()
    if not t:
        pending = (
            f" {total_groups} pattern group(s) awaited analysis; "
            f"<strong>0 were analysed</strong>."
            if total_groups else ""
        )
        return (
            '<p style="font-size:10px;color:#94a3b8;margin:6px 0">'
            "AI analysis unavailable (no provider configured, or the analysis "
            f"failed — see the session log).{pending} Classifications would be "
            "advisory only; scoring is deterministic either way.</p>"
        )
    inputs = t.get("input_groups", {})
    # Coverage line: prefer the counts the triage stored; fall back to
    # counting the budget-excluded padding rows for older results.
    total = t.get("total_count") or len(t.get("groups", []))
    analysed = t.get("analysed_count")
    if analysed is None:
        analysed = sum(
            1 for g in t.get("groups", [])
            if g.get("rationale") != "not analysed — payload budget reached"
        )
    cov_color = "#16a34a" if analysed == total else "#d97706"
    coverage = (
        f" <span style='color:{cov_color};font-weight:700'>"
        f"AI analysed {analysed} of {total} pattern group(s).</span>"
        + ("" if analysed == total else
           " The remainder are marked NEEDS_HUMAN, not silently dropped.")
    )
    rows = []
    td = ("padding:6px 10px;border-bottom:1px solid #e2e8f033;font-size:11px;"
          "vertical-align:top")
    for g in t.get("groups", []):
        color, bg = _AI_CLS_STYLE.get(g["classification"], ("#64748b", "#f1f5f9"))
        src = inputs.get(g["id"], {})
        label = (f"{src.get('severity', '?')} · {src.get('category', '?')} "
                 f"×{src.get('count', '?')}")
        conf = f" ({int(g['confidence'] * 100)}%)" if g.get("confidence") is not None else ""
        rows.append(
            "<tr>"
            f"<td style='{td};white-space:nowrap'>{html.escape(g['id'])}</td>"
            f"<td style='{td};white-space:nowrap'>{html.escape(label)}</td>"
            f"<td style='{td};white-space:nowrap'><span style='background:{bg};"
            f"color:{color};border-radius:999px;padding:1px 8px;font-size:10px;"
            f"font-weight:700'>{html.escape(g['classification'])}{conf}</span></td>"
            f"<td style='{td};overflow-wrap:anywhere'>{html.escape(g.get('rationale', ''))}"
            + (f" <em style='color:#94a3b8'>Fix: {html.escape(g['suggested_fix'])}</em>"
               if g.get("suggested_fix") else "")
            + "</td></tr>"
        )
    summary = html.escape(t.get("summary", ""))
    return (
        "<div style='margin:8px 0'>"
        "<p style='font-size:11px;color:#64748b;margin:0 0 4px'>"
        "<strong>AI triage</strong> (advisory — proposals from the model, "
        "validated against a closed set in code; never affects scoring)."
        + coverage
        + (f" Summary: <em>{summary}</em>" if summary else "") + "</p>"
        "<div style='overflow-x:auto'><table style='width:100%;"
        "border-collapse:collapse'><thead><tr>"
        "<th style='text-align:left;font-size:10px;padding:6px 10px'>Group</th>"
        "<th style='text-align:left;font-size:10px;padding:6px 10px'>What</th>"
        "<th style='text-align:left;font-size:10px;padding:6px 10px'>Classification</th>"
        "<th style='text-align:left;font-size:10px;padding:6px 10px'>Rationale</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div></div>"
    )


def _render_mobile_section(findings: list[dict], layered: Any = None) -> str:
    """Mobile findings, grouped by severity, most serious first.

    `info` findings are shown too, and deliberately so: they carry the
    "this check could not run on this engine" notices. Hiding them would let a
    green panel imply touch coverage that WebKit cannot actually provide.
    """
    if not findings:
        if layered is not None and getattr(layered, "mobile_health", None) is None:
            return (
                '<div class="card"><h2>Mobile</h2>'
                '<p style="font-size:11px;color:#64748b">No mobile tests ran in '
                'this session, so mobile behaviour was <strong>not measured</strong>. '
                'Run <code>pytest -m mobile</code> to populate this section.</p></div>'
            )
        return (
            '<div class="card"><h2>Mobile</h2>'
            '<p style="font-size:11px;color:#16a34a">Mobile tests ran and raised '
            'no findings.</p></div>'
        )

    order = ("blocker", "major", "minor", "info")
    by_sev: dict[str, list[dict]] = {k: [] for k in order}
    for f in findings:
        by_sev.setdefault(f.get("severity", "info"), []).append(f)

    engines = sorted({(f.get("details") or {}).get("engine")
                      for f in findings if (f.get("details") or {}).get("engine")})

    # One grouping mechanism everywhere: the same device-blind, number-blind
    # collapse the AI triage uses, so the G-ids in the pattern table below
    # match the G-ids in the AI triage table. 247 per-device findings are
    # ~36-86 unique patterns — the pattern is the unit a developer acts on;
    # the per-device rows are its evidence.
    from utils.ai_mobile_triage import group_findings
    groups = group_findings(findings)
    actionable = sum(1 for f in findings
                     if (f.get("severity") or "").lower()
                     in ("blocker", "major", "minor"))
    all_devices = sorted({f.get("device") for f in findings if f.get("device")})
    n_dev = len(all_devices)

    chips = []
    for sev in order:
        n = len(by_sev.get(sev) or [])
        if not n:
            continue
        color, bg = _MOBILE_SEV_STYLE[sev]
        chips.append(
            f"<span class='tint-surface' style='background:{bg};color:{color};"
            f"border:1px solid {color}33;border-radius:999px;padding:2px 10px;"
            f"font-size:10px;font-weight:700;text-transform:uppercase'>"
            f"{n} {sev}</span>"
        )
    if groups:
        chips.append(
            "<span class='tint-surface' style='background:#f1f5f9;color:#334155;"
            "border:1px solid #33415533;border-radius:999px;padding:2px 10px;"
            "font-size:10px;font-weight:700'>"
            f"{actionable} findings → {len(groups)} unique patterns</span>"
        )

    rows = []
    for sev in order:
        for f in sorted(by_sev.get(sev) or [], key=lambda x: (x.get("page", ""), x.get("device", ""))):
            color, bg = _MOBILE_SEV_STYLE.get(sev, ("#64748b", "#f1f5f9"))
            det = f.get("details") or {}
            extra = ""
            if "drift_px" in det:
                extra = f" <em style='color:#94a3b8'>(drift {det['drift_px']}px)</em>"
            # Real cell spacing, not just a font-size: without padding the
            # rows render cramped and long finding messages collide with the
            # neighbouring column. Fixed columns get nowrap; the message
            # column wraps and word-breaks so a long URL cannot widen the
            # table past its overflow-x container.
            td_fixed = ("padding:6px 10px;border-bottom:1px solid #e2e8f033;"
                        "font-size:11px;vertical-align:top;white-space:nowrap")
            td_msg = ("padding:6px 10px;border-bottom:1px solid #e2e8f033;"
                      "font-size:11px;vertical-align:top;line-height:1.45;"
                      "overflow-wrap:anywhere;min-width:280px")
            rows.append(
                "<tr>"
                f"<td style='{td_fixed}'><span style='color:{color};"
                f"font-weight:700;font-size:10px;text-transform:uppercase'>"
                f"{html.escape(sev)}</span></td>"
                f"<td style='{td_fixed}'>{html.escape(str(f.get('engine', '') or '—'))}</td>"
                f"<td style='{td_fixed}'>{html.escape(str(f.get('page', '')))}</td>"
                f"<td style='{td_fixed}'>{html.escape(str(f.get('device', '')))}</td>"
                f"<td style='{td_fixed}'>{html.escape(str(f.get('category', '')))}</td>"
                f"<td style='{td_msg}'>{html.escape(str(f.get('message', '')))}{extra}</td>"
                "</tr>"
            )

    # ── Pattern table: one row per unique issue, most serious first ────
    th = ("text-align:left;font-size:10px;padding:6px 10px;"
          "border-bottom:2px solid #e2e8f0")
    td_p = ("padding:6px 10px;border-bottom:1px solid #e2e8f033;font-size:11px;"
            "vertical-align:top")
    pattern_rows = []
    for g in groups:
        color, bg = _MOBILE_SEV_STYLE.get(g["severity"], ("#64748b", "#f1f5f9"))
        nd = len(g["devices"])
        if n_dev > 1 and nd == n_dev:
            reach, reach_color = "cross-device", "#dc2626"
        elif nd == 1:
            reach, reach_color = "device-specific", "#0284c7"
        else:
            reach, reach_color = "partial", "#d97706"
        sample = (g["samples"][0] if g.get("samples") else g.get("pattern", ""))
        pattern_rows.append(
            "<tr>"
            f"<td style='{td_p};white-space:nowrap'>{html.escape(g['id'])}</td>"
            f"<td style='{td_p};white-space:nowrap'><span style='color:{color};"
            f"font-weight:700;font-size:10px;text-transform:uppercase'>"
            f"{html.escape(g['severity'])}</span></td>"
            f"<td style='{td_p};white-space:nowrap'>{html.escape(g['category'])}</td>"
            f"<td style='{td_p};overflow-wrap:anywhere;min-width:260px;"
            f"line-height:1.45'>{html.escape(sample)}</td>"
            f"<td style='{td_p}'>{html.escape(', '.join(g['pages']))}</td>"
            f"<td style='{td_p};white-space:nowrap'>{nd}/{n_dev} "
            f"<span style='color:{reach_color};font-size:10px;font-weight:700'>"
            f"{reach}</span></td>"
            f"<td style='{td_p};text-align:right'>{g['count']}</td>"
            "</tr>"
        )
    pattern_html = ""
    if pattern_rows:
        pattern_html = (
            "<p style='font-size:11px;color:#64748b;margin:8px 0 4px'>"
            "<strong>Unique issue patterns</strong> — one row per distinct "
            "issue; Count is how many per-device/per-page findings it "
            "produced. G-ids match the AI triage table.</p>"
            "<div style='overflow-x:auto'><table style='width:100%;"
            "border-collapse:collapse'><thead><tr>"
            f"<th style='{th}'>ID</th><th style='{th}'>Severity</th>"
            f"<th style='{th}'>Check</th><th style='{th}'>Example finding</th>"
            f"<th style='{th}'>Pages</th><th style='{th}'>Devices</th>"
            f"<th style='{th};text-align:right'>Count</th>"
            f"</tr></thead><tbody>{''.join(pattern_rows)}</tbody></table></div>"
        )

    # ── Coverage gaps: aggregated, never buried in the raw table ───────
    # UNTESTED/UNSUPPORTED notes are a statement about what this engine
    # could not exercise. As 78 identical rows they vanish; as one row per
    # check they read as what they are: a coverage gap, not a pass.
    gaps: dict[str, dict] = {}
    for f in findings:
        msg = f.get("message") or ""
        if "UNTESTED" in msg or "UNSUPPORTED" in msg:
            g = gaps.setdefault(f.get("category") or "?",
                                {"count": 0, "pages": set(), "sample": msg})
            g["count"] += 1
            g["pages"].add(f.get("page") or "?")
    untested_note = ""
    if gaps:
        gap_rows = "".join(
            "<tr>"
            f"<td style='{td_p};white-space:nowrap'>{html.escape(check)}</td>"
            f"<td style='{td_p}'>{html.escape(', '.join(sorted(g['pages'])))}</td>"
            f"<td style='{td_p};text-align:right'>{g['count']}</td>"
            f"<td style='{td_p};overflow-wrap:anywhere;min-width:260px;"
            f"line-height:1.45'>{html.escape(g['sample'][:200])}</td>"
            "</tr>"
            for check, g in sorted(gaps.items())
        )
        total_gap = sum(g["count"] for g in gaps.values())
        untested_note = (
            f"<p style='font-size:11px;color:#0284c7;margin:8px 0 4px'>"
            f"<strong>Coverage gaps on this engine: {len(gaps)} check "
            f"famil{'y' if len(gaps) == 1 else 'ies'} not exercised "
            f"({total_gap} notice(s))</strong> — UNTESTED means untested, "
            f"not passed. Nothing was substituted with a mouse event.</p>"
            "<div style='overflow-x:auto'><table style='width:100%;"
            "border-collapse:collapse'><thead><tr>"
            f"<th style='{th}'>Check</th><th style='{th}'>Pages</th>"
            f"<th style='{th};text-align:right'>Notices</th>"
            f"<th style='{th}'>Why</th>"
            f"</tr></thead><tbody>{gap_rows}</tbody></table></div>"
        )

    # ── The three-questions strip. One number cannot answer "how many
    # observations were made", "how many distinct issues is that", and "how
    # much of the framework actually executed" — so each gets its own tile,
    # and the health score can never be misread as any of them.
    total_gap = sum(g["count"] for g in gaps.values()) if gaps else 0
    sev_counts = {s: len(by_sev.get(s) or []) for s in order}

    def _tile(n, label, color="#334155"):
        return (
            "<div style='min-width:92px;padding:8px 12px;border:1px solid "
            "#e2e8f0;border-radius:10px;text-align:center'>"
            f"<div style='font-size:18px;font-weight:800;color:{color}'>{n}</div>"
            f"<div style='font-size:9px;color:#64748b;text-transform:uppercase;"
            f"letter-spacing:.5px'>{label}</div></div>"
        )

    strip_tiles = [
        _tile(len(findings), "Findings"),
        _tile(len(groups), "Unique patterns"),
        _tile(total_gap, "Coverage gaps", "#0284c7"),
        _tile(sev_counts.get("blocker", 0), "Blocker", _MOBILE_SEV_STYLE["blocker"][0]),
        _tile(sev_counts.get("major", 0), "Major", _MOBILE_SEV_STYLE["major"][0]),
        _tile(sev_counts.get("minor", 0), "Minor", _MOBILE_SEV_STYLE["minor"][0]),
    ]

    # Executed coverage per engine, from the sidecars (each engine's last
    # run). Derived from the auditor's attempt tally, NOT from findings — a
    # check that runs clean emits nothing, so findings under-count execution.
    # Two coverage figures, because a reviewer must not have to reconcile
    # "67% executed" against "Quality × 100% coverage" themselves:
    #   raw execution  = executed / attempted            (all invocations)
    #   scoring coverage = executed / (attempted − N/A)   (drops structurally
    #                      unsupported checks; THIS is what multiplies Quality)
    coverage_lines = []
    try:
        from utils.mobile_report_builder import load_engine_summaries
        for s in load_engine_summaries():
            cs = s.get("check_stats") or {}
            att = int(cs.get("attempted") or 0)
            exe = int(cs.get("executed") or 0)
            na  = int(cs.get("na_structural") or 0)
            if att:
                executable = att - na
                raw_pct = round(100 * exe / att)
                scor_pct = round(100 * exe / executable) if executable > 0 else 0
                eng = html.escape(str(s.get("engine", "?")))
                coverage_lines.append(
                    f"<strong>{eng}</strong> "
                    f"{raw_pct}% raw execution ({exe}/{att}) &nbsp;·&nbsp; "
                    f"{scor_pct}% scoring coverage ({exe}/{executable}"
                    + (f", {na} N/A — structurally unsupported" if na else "")
                    + ")"
                )
    except Exception:
        pass
    coverage_html = (
        "<p style='font-size:11px;color:#64748b;margin:2px 0 8px'>Coverage — "
        + " &nbsp;|&nbsp; ".join(coverage_lines)
        + ". <strong>Raw execution</strong> is the share of all interaction-check "
          "invocations that ran; <strong>scoring coverage</strong> excludes checks "
          "the engine structurally cannot run (N/A — e.g. WebKit lacks the "
          "Chromium CDP gesture/network APIs) and is the figure multiplied into "
          "the mobile score, so an engine is never penalised for what it cannot "
          "execute. UNTESTED means untested, never a pass. Note: these are "
          "interaction-check <em>invocations</em> — a different population from "
          "the pass/fail regression tests in the header above; the two counts "
          "are not comparable.</p>"
    ) if coverage_lines else ""
    stat_strip = (
        "<div style='display:flex;gap:10px;flex-wrap:wrap;margin:4px 0 6px'>"
        + "".join(strip_tiles) + "</div>" + coverage_html
    )

    engine_note = (f" &nbsp;·&nbsp; engine(s): {', '.join(engines)}" if engines else "")

    ai_html = _render_mobile_ai_triage(total_groups=len(groups))

    return f"""
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <h2 style="margin:0">Mobile</h2>
        <div style="display:flex;gap:6px;flex-wrap:wrap">{''.join(chips)}</div>
      </div>
      <p style="font-size:11px;color:#64748b;margin:0 0 8px">
        Findings from the mobile suite{engine_note}. Only <strong>blocker</strong>
        fails a test; major/minor/info are recorded here.
      </p>
      {stat_strip}
      {pattern_html}
      {ai_html}
      {untested_note}
      <details style="margin-top:8px">
        <summary style="font-size:11px;color:#64748b;cursor:pointer;font-weight:700">
          All {len(findings)} raw findings (per device/page — the evidence behind the patterns above)
        </summary>
        <div style="overflow-x:auto">
          <table style="width:100%;border-collapse:collapse">
            <thead><tr>
              <th style="text-align:left;font-size:10px;padding:6px 10px;border-bottom:2px solid #e2e8f0">Severity</th>
              <th style="text-align:left;font-size:10px;padding:6px 10px;border-bottom:2px solid #e2e8f0">Engine</th>
              <th style="text-align:left;font-size:10px;padding:6px 10px;border-bottom:2px solid #e2e8f0">Page</th>
              <th style="text-align:left;font-size:10px;padding:6px 10px;border-bottom:2px solid #e2e8f0">Device</th>
              <th style="text-align:left;font-size:10px;padding:6px 10px;border-bottom:2px solid #e2e8f0">Check</th>
              <th style="text-align:left;font-size:10px;padding:6px 10px;border-bottom:2px solid #e2e8f0">Finding</th>
            </tr></thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </div>
      </details>
    </div>
    """


# ─────────────────────────────────────────────────────────────────────────────
# JS error cluster section
# ─────────────────────────────────────────────────────────────────────────────

def _render_cluster_section(clusters: list[ErrorCluster]) -> str:
    """Render the JS error cluster card."""
    if not clusters:
        return (
            "<div class='card'>"
            "<h2>JS Error Clusters</h2>"
            "<p style='color:#94a3b8;font-size:12px;padding:8px 0'>No JS errors recorded this session. ✓</p>"
            "</div>"
        )

    _SEV_STYLE = {
        "CRITICAL": ("background:#fee2e2;color:#dc2626", "●"),
        "HIGH":     ("background:#ffedd5;color:#ea580c", "●"),
        "MEDIUM":   ("background:#fef9c3;color:#b45309", "●"),
        "LOW":      ("background:#f0fdf4;color:#16a34a", "●"),
    }
    _DOM_COLOR = {
        "PRODUCT":        "#7c3aed",
        "INFRASTRUCTURE": "#0369a1",
        "FRAMEWORK":      "#b45309",
        "UNKNOWN":        "#6b7280",
    }

    rows = []
    for c in clusters:
        sev_style, dot = _SEV_STYLE.get(c.severity, ("", "●"))
        dom_color = _DOM_COLOR.get(c.domain, "#6b7280")
        new_badge = (
            "<span style='display:inline-block;padding:1px 6px;border-radius:999px;"
            "background:#dcfce7;color:#166534;font-size:9px;font-weight:700;"
            "margin-left:6px'>🆕 NEW</span>"
        ) if c.is_new else ""
        rows.append(
            f"<tr>"
            f"<td style='padding:7px 12px;font-size:11px;font-family:monospace'>"
            f"{c.title[:90]}{new_badge}</td>"
            f"<td style='padding:7px 12px;text-align:center;font-weight:700'>{c.count}</td>"
            f"<td style='padding:7px 12px;font-size:11px;font-weight:600;color:{dom_color}'>{c.domain}</td>"
            f"<td style='padding:7px 12px'>"
            f"<span style='display:inline-block;padding:2px 8px;border-radius:9999px;"
            f"font-size:10px;font-weight:700;{sev_style}'>{dot} {c.severity}</span></td>"
            f"<td style='padding:7px 12px;font-size:11px;color:#64748b'>{c.first_seen_in}</td>"
            f"</tr>"
        )

    return (
        "<div class='card'>"
        f"<h2>JS Error Clusters ({len(clusters)} group{'s' if len(clusters) != 1 else ''})</h2>"
        "<table>"
        "<thead><tr>"
        "<th>Error Pattern</th>"
        "<th style='text-align:center'>Count</th>"
        "<th>Domain</th>"
        "<th>Severity</th>"
        "<th>First Seen In</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
        "</div>"
    )
