"""
PDF report builder — generates a PDF test report from session data.
Python port of Java PdfReportBuilder (Java used openhtmltopdf).

Strategy:
  1. Build an HTML string (same data as the dashboard, in a print-friendly layout).
  2. If weasyprint is installed, convert to PDF → reports/trend/report.pdf
  3. If weasyprint is NOT installed, save the HTML → reports/trend/report-printable.html
     (user can open in browser and Ctrl+P → Save as PDF).

Install weasyprint for auto-PDF:
    pip install weasyprint

Note: weasyprint requires GTK+ on Windows. If that's not available, the
HTML fallback is always generated.
"""

from __future__ import annotations

from html import escape as esc
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from utils.snapshot_writer import TestRecord
from utils.error_clusterer import ErrorCluster
from utils.layered_health_scores import LayeredScores, score_band

logger = logging.getLogger(__name__)

PDF_PATH  = Path("reports/trend/report.pdf")
HTML_PATH = Path("reports/trend/report-printable.html")


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def build(
    records: Sequence[TestRecord],
    stats: dict[str, Any],
    decision: Any,
    scores: LayeredScores,
    clusters: Sequence[ErrorCluster],
    started_at_ms: int,
) -> Path:
    """
    Build the PDF (or printable HTML) report.

    Parameters mirror what dashboard_builder.build() already computes so
    callers can pass the same objects without re-computing.

    Returns the path to the written file (PDF if weasyprint available,
    else printable HTML).
    """
    html = _render_report(records, stats, decision, scores, clusters, started_at_ms)

    PDF_PATH.parent.mkdir(parents=True, exist_ok=True)
    HTML_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Always write the printable HTML as fallback
    HTML_PATH.write_text(html, encoding="utf-8")
    logger.info("[PdfReportBuilder] Printable HTML → %s", HTML_PATH)

    # Attempt PDF via weasyprint
    try:
        from weasyprint import HTML as WeasyprintHTML  # type: ignore
        WeasyprintHTML(string=html, base_url=str(HTML_PATH.parent.resolve())).write_pdf(str(PDF_PATH))
        logger.info("[PdfReportBuilder] PDF written → %s", PDF_PATH)
        return PDF_PATH
    except ImportError:
        logger.info(
            "[PdfReportBuilder] weasyprint not installed — PDF skipped. "
            "Install with: pip install weasyprint  "
            "Printable HTML available at: %s",
            HTML_PATH,
        )
    except Exception as exc:
        logger.warning("[PdfReportBuilder] PDF generation failed: %s", exc)

    return HTML_PATH


# ─────────────────────────────────────────────────────────────────────────────
# HTML rendering (print-optimised layout)
# ─────────────────────────────────────────────────────────────────────────────

def _render_report(
    records: Sequence[TestRecord],
    stats: dict[str, Any],
    decision: Any,
    scores: LayeredScores,
    clusters: Sequence[ErrorCluster],
    started_at_ms: int,
) -> str:
    run_time  = datetime.fromtimestamp(started_at_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    prod_label, prod_color, prod_bg = score_band(scores.product_health)
    infra_label, infra_color, infra_bg = score_band(scores.infra_health)
    fw_label, fw_color, fw_bg = score_band(scores.framework_health)

    test_rows = _test_rows(records)
    cluster_rows = _cluster_rows(clusters)
    feature_rows = _feature_rows(stats.get("by_feature", {}))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Appy Pie Automate — Test Report {run_time}</title>
<style>
  @page {{ size: A4; margin: 20mm 15mm; }}
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: Arial, 'Segoe UI', sans-serif; font-size: 11px; color: #1e293b; }}
  h1 {{ font-size: 18px; font-weight: 800; margin-bottom: 4px; }}
  h2 {{ font-size: 13px; font-weight: 700; color: #475569; margin: 16px 0 6px;
        text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid #e2e8f0;
        padding-bottom: 4px; }}
  .meta {{ font-size: 10px; color: #64748b; margin-bottom: 16px; }}
  .decision {{ display: inline-block; padding: 5px 14px; border-radius: 6px;
               font-size: 15px; font-weight: 800; background: {decision.bg};
               color: {decision.color}; border: 2px solid {decision.color}55;
               margin-bottom: 16px; }}
  .cards {{ display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }}
  .card {{ flex: 1; min-width: 80px; border-radius: 8px; padding: 10px 12px;
           border: 1px solid #e2e8f0; }}
  .card .label {{ font-size: 9px; font-weight: 700; text-transform: uppercase;
                  letter-spacing: 0.6px; margin-bottom: 4px; }}
  .card .value {{ font-size: 22px; font-weight: 800; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 12px; font-size: 10px; }}
  th {{ text-align: left; padding: 5px 8px; font-size: 9px; font-weight: 700;
        color: #94a3b8; text-transform: uppercase; letter-spacing: 0.6px;
        border-bottom: 2px solid #e2e8f0; }}
  td {{ padding: 5px 8px; border-bottom: 1px solid #f1f5f9; }}
  tr.fail {{ background: #fef2f2; }}
  .badge {{ display: inline-block; padding: 1px 6px; border-radius: 999px;
            font-size: 9px; font-weight: 700; }}
  .badge-pass {{ background: #dcfce7; color: #166534; }}
  .badge-fail {{ background: #fee2e2; color: #dc2626; }}
  .badge-skip {{ background: #f1f5f9; color: #94a3b8; }}
  .badge-crit {{ background: #fee2e2; color: #dc2626; }}
  .badge-high {{ background: #ffedd5; color: #9a3412; }}
  .badge-med  {{ background: #fef9c3; color: #713f12; }}
  .page-break {{ page-break-before: always; }}
  .domain-product {{ color: #6d28d9; }}
  .domain-infra   {{ color: #0369a1; }}
  .domain-fw      {{ color: #b45309; }}
  .score-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
                 margin-bottom: 16px; }}
  .score-box {{ border-radius: 8px; padding: 12px; text-align: center; }}
  .score-box .score {{ font-size: 30px; font-weight: 800; }}
  .score-box .sub {{ font-size: 9px; font-weight: 700; text-transform: uppercase;
                     letter-spacing: 0.5px; margin-top: 4px; }}
  @media print {{
    body {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  }}
</style>
</head>
<body>

<!-- Cover / Header -->
<h1>Appy Pie Automate — Test Report</h1>
<div class="meta">
  Run started: <strong>{run_time}</strong> &nbsp;·&nbsp;
  Generated: {generated} &nbsp;·&nbsp;
  Tests: {stats.get("total", 0)} &nbsp;·&nbsp;
  Framework: Python {_python_version()} + Playwright
</div>

<!-- Release Decision -->
<div class="decision">{decision.label}</div>
<div style="font-size:11px;color:#475569;margin-bottom:16px">{decision.description}</div>

<!-- Summary cards -->
<div class="cards">
  {_card("Total",     str(stats.get("total",    0)), "#6366f1", "#eef2ff")}
  {_card("Passed",    str(stats.get("passed",   0)), "#16a34a", "#dcfce7")}
  {_card("Failed",    str(stats.get("failed",   0)), "#dc2626", "#fee2e2")}
  {_card("Skipped",   str(stats.get("skipped",  0)), "#94a3b8", "#f1f5f9")}
  {_card("Pass Rate", f"{stats.get('pass_rate', 0)}%", "#0369a1", "#e0f2fe")}
</div>

<!-- Health Score -->
<h2>Health Score</h2>
<div class="score-grid">
  <div class="score-box" style="background:{prod_bg};border:1px solid {prod_color}44">
    <div class="score" style="color:{prod_color}">{scores.product_health}</div>
    <div class="sub" style="color:{prod_color}">Product Health</div>
  </div>
  <div class="score-box" style="background:{infra_bg};border:1px solid {infra_color}44">
    <div class="score" style="color:{infra_color}">{scores.infra_health}</div>
    <div class="sub" style="color:{infra_color}">Infra Health</div>
  </div>
  <div class="score-box" style="background:{fw_bg};border:1px solid {fw_color}44">
    <div class="score" style="color:{fw_color}">{scores.framework_health}</div>
    <div class="sub" style="color:{fw_color}">Framework Health</div>
  </div>
  {_mobile_score_box(scores)}
</div>
<p style="font-size:10px;color:#64748b;margin-bottom:16px">
  Overall: <strong>{scores.overall}/100</strong>
  &nbsp;({_pdf_weighting_note(scores)})
</p>

{_mobile_section(scores)}

<!-- Feature Coverage -->
<h2>Feature Coverage</h2>
<table>
  <thead><tr>
    <th>Feature</th><th>Pass</th><th>Fail</th><th>Pass Rate</th>
  </tr></thead>
  <tbody>{feature_rows}</tbody>
</table>

<!-- JS Error Clusters (page break before if many tests) -->
<h2>JS Error Clusters ({len(clusters)} group{"s" if len(clusters) != 1 else ""})</h2>
{_cluster_table(cluster_rows) if clusters else
 '<p style="color:#94a3b8;font-size:10px">No JS errors recorded this session.</p>'}

<!-- Test Results -->
<h2 class="page-break">Test Results</h2>
<table>
  <thead><tr>
    <th>Test Method</th><th>Feature</th><th>Category</th>
    <th>Status</th><th style="text-align:right">Duration</th>
  </tr></thead>
  <tbody>{test_rows}</tbody>
</table>

<div style="text-align:center;font-size:9px;color:#94a3b8;margin-top:20px;border-top:1px solid #e2e8f0;padding-top:8px">
  Generated by automate-workflow-py · Python-side PDF Report Builder
</div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Render helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mobile_score_box(scores) -> str:
    """Fourth score box. 'Not measured' is rendered explicitly — an absent box
    would read as fine, a grey box reads as 'we did not look'."""
    m = getattr(scores, "mobile_health", None)
    if m is None:
        return (
            '<div class="score-box" style="background:#f1f5f9;'
            'border:1px dashed #94a3b866">'
            '<div class="score" style="color:#64748b">&mdash;</div>'
            '<div class="sub" style="color:#64748b">Mobile (not measured)</div>'
            "</div>"
        )
    _, color, bg = score_band(m)
    return (
        f'<div class="score-box" style="background:{bg};border:1px solid {color}44">'
        f'<div class="score" style="color:{color}">{m}</div>'
        f'<div class="sub" style="color:{color}">Mobile Health</div>'
        "</div>"
    )


def _pdf_weighting_note(scores) -> str:
    v = getattr(scores, "scoring_version", 1)
    if getattr(scores, "mobile_health", None) is None:
        return (f"Product 50% · Infrastructure 30% · Framework 20% weighted "
                f"average; scoring v{v}, Mobile excluded — no mobile tests ran")
    return (f"Product 40% · Infrastructure 25% · Framework 20% · Mobile 15% "
            f"weighted average; scoring v{v}")


_SEV_ORDER = {"blocker": 0, "major": 1, "minor": 2, "info": 3}


def _mobile_section(scores) -> str:
    """Mobile compatibility for the EXPORT, including other engines.

    Two parts, deliberately different in provenance:
      * This session's findings — live, from the in-process accumulator.
      * A cross-engine table read from each engine's persisted
        mobile-summary.json sidecar. This is what puts a WebKit run's results
        into a Chromium session's export (and vice versa) — without it, an
        exported PDF silently carried only the exporting engine's mobile
        story. Each row shows its own generated_at: these are point-in-time
        snapshots of that engine's LAST mobile run, shown for reporting and
        never scored.
    """
    from utils.mobile_report_builder import load_engine_summaries, session_findings

    parts = ['<h2 class="page-break">Mobile Compatibility</h2>']

    findings = sorted(
        session_findings(),
        key=lambda f: (_SEV_ORDER.get(f.get("severity", "info"), 9),
                       f.get("page", ""), f.get("device", "")),
    )
    if findings:
        shown = findings[:60]
        rows = "".join(
            "<tr>"
            f"<td>{_sev_badge(str(f.get('severity', '')))}</td>"
            f"<td style='white-space:nowrap'>{esc(str(f.get('engine', '') or '—'))}</td>"
            f"<td style='white-space:nowrap'>{esc(str(f.get('page', '')))}</td>"
            f"<td style='white-space:nowrap'>{esc(str(f.get('device', '')))}</td>"
            f"<td style='white-space:nowrap'>{esc(str(f.get('category', '')))}</td>"
            f"<td style='overflow-wrap:anywhere;line-height:1.45'>"
            f"{esc(str(f.get('message', '')))}</td>"
            "</tr>"
            for f in shown
        )
        truncated = ("" if len(findings) <= 60 else
                     f'<p style="font-size:9px;color:#94a3b8">…and '
                     f'{len(findings) - 60} more (see dashboard).</p>')
        parts.append(
            f"<h3 style='font-size:11px;margin:8px 0 4px'>This session "
            f"({len(findings)} finding{'s' if len(findings) != 1 else ''})</h3>"
            "<table><thead><tr><th>Severity</th><th>Engine</th><th>Page</th>"
            "<th>Device</th><th>Check</th><th>Finding</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>{truncated}"
        )
    else:
        parts.append(
            '<p style="color:#94a3b8;font-size:10px">No mobile findings in '
            "this session"
            + (" — no mobile tests ran."
               if getattr(scores, "mobile_health", None) is None
               else " (mobile tests ran clean).") + "</p>"
        )

    # This session's AI triage, when it ran.
    from utils.ai_mobile_triage import session_triage
    t = session_triage()
    if t:
        inputs = t.get("input_groups", {})
        trows = "".join(
            "<tr>"
            f"<td style='white-space:nowrap'>{esc(g['id'])}</td>"
            f"<td style='white-space:nowrap'>{esc(str(inputs.get(g['id'], {}).get('severity', '?')))} · "
            f"{esc(str(inputs.get(g['id'], {}).get('category', '?')))} "
            f"×{inputs.get(g['id'], {}).get('count', '?')}</td>"
            f"<td style='white-space:nowrap'>{esc(g['classification'])}</td>"
            f"<td style='overflow-wrap:anywhere'>{esc(g.get('rationale', ''))}</td>"
            "</tr>"
            for g in t.get("groups", [])
        )
        parts.append(
            "<h3 style='font-size:11px;margin:10px 0 4px'>AI triage "
            "(advisory — model proposals validated against a closed set; "
            "never affects scoring)</h3>"
            + (f"<p style='font-size:10px;color:#475569'>{esc(t.get('summary', ''))}</p>"
               if t.get("summary") else "")
            + "<table><thead><tr><th>Group</th><th>What</th>"
              "<th>Classification</th><th>Rationale</th></tr></thead>"
              f"<tbody>{trows}</tbody></table>"
        )

    summaries = load_engine_summaries()
    if summaries:
        rows = ""
        for sm in summaries:
            sev = sm.get("by_severity", {})
            untested = sum(
                1 for f in sm.get("findings", [])
                if "UNTESTED" in (f.get("message") or "")
            )
            rows += (
                "<tr>"
                f"<td style='white-space:nowrap'><strong>{esc(str(sm.get('engine', '?')))}</strong></td>"
                f"<td style='white-space:nowrap'>{esc(str(sm.get('generated_at', '?'))[:19])}</td>"
                f"<td>{sev.get('blocker', 0)}</td><td>{sev.get('major', 0)}</td>"
                f"<td>{sev.get('minor', 0)}</td><td>{sev.get('info', 0)}</td>"
                f"<td>{untested}</td>"
                f"<td>{len(sm.get('devices_tested', []))}</td>"
                "</tr>"
            )
        parts.append(
            "<h3 style='font-size:11px;margin:10px 0 4px'>Latest result per "
            "engine</h3>"
            "<table><thead><tr><th>Engine</th><th>When (UTC)</th>"
            "<th>Blocker</th><th>Major</th><th>Minor</th><th>Info</th>"
            "<th>Untested</th><th>Devices</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            '<p style="font-size:9px;color:#94a3b8">Each row is that '
            "engine's most recent mobile run (point-in-time; reporting only, "
            "never scored). UNTESTED counts checks the engine cannot "
            "exercise — e.g. CDP touch gestures on WebKit — reported rather "
            "than substituted.</p>"
        )
    else:
        parts.append(
            '<p style="color:#94a3b8;font-size:10px">No per-engine mobile '
            "summaries recorded yet — run the mobile suite on each engine to "
            "populate the cross-engine table.</p>"
        )
    return "\n".join(parts)


def _card(label: str, value: str, color: str, bg: str) -> str:
    return (
        f"<div class='card' style='background:{bg};border-color:{color}33'>"
        f"<div class='label' style='color:{color}'>{label}</div>"
        f"<div class='value' style='color:{color}'>{value}</div>"
        f"</div>"
    )


def _badge(status: str) -> str:
    cls = {"PASS": "badge-pass", "FAIL": "badge-fail"}.get(status, "badge-skip")
    return f"<span class='badge {cls}'>{status}</span>"


def _sev_badge(severity: str) -> str:
    cls = {"CRITICAL": "badge-crit", "HIGH": "badge-high"}.get(severity, "badge-med")
    return f"<span class='badge {cls}'>{severity}</span>"


def _fmt_ms(ms: int) -> str:
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    return f"{ms / 60_000:.1f} min"


def _test_rows(records: Sequence[TestRecord]) -> str:
    rows = []
    for r in records:
        cls = "fail" if r.status == "FAIL" else ""
        rows.append(
            f"<tr class='{cls}'>"
            f"<td style='font-family:monospace'>{r.method}</td>"
            f"<td>{r.feature}</td>"
            f"<td>{r.category}</td>"
            f"<td>{_badge(r.status)}</td>"
            f"<td style='text-align:right'>{_fmt_ms(r.duration_ms)}</td>"
            f"</tr>"
        )
    return "\n".join(rows)


def _cluster_rows(clusters: Sequence[ErrorCluster]) -> str:
    rows = []
    for c in clusters:
        domain_cls = {
            "PRODUCT": "domain-product",
            "INFRASTRUCTURE": "domain-infra",
            "FRAMEWORK": "domain-fw",
        }.get(c.domain, "")
        new_badge = " <span class='badge' style='background:#dcfce7;color:#166534'>NEW</span>" if c.is_new else ""
        rows.append(
            f"<tr>"
            f"<td style='font-family:monospace;font-size:9px'>{c.title[:80]}{new_badge}</td>"
            f"<td style='text-align:center'>{c.count}</td>"
            f"<td class='{domain_cls}'>{c.domain}</td>"
            f"<td>{_sev_badge(c.severity)}</td>"
            f"<td style='font-size:9px;color:#64748b'>{c.first_seen_in}</td>"
            f"</tr>"
        )
    return "\n".join(rows)


def _cluster_table(rows: str) -> str:
    return (
        "<table>"
        "<thead><tr>"
        "<th>Error Pattern</th><th style='text-align:center'>Count</th>"
        "<th>Domain</th><th>Severity</th><th>First Seen In</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


def _feature_rows(by_feature: dict) -> str:
    rows = []
    for feat, counts in sorted(by_feature.items()):
        total = counts["pass"] + counts["fail"] + counts["skip"]
        rate  = round(counts["pass"] / total * 100) if total else 0
        colour = "#16a34a" if rate == 100 else ("#ca8a04" if rate >= 80 else "#dc2626")
        rows.append(
            f"<tr>"
            f"<td>{feat}</td>"
            f"<td style='text-align:center'>{counts['pass']}</td>"
            f"<td style='text-align:center;color:#dc2626'>{counts['fail']}</td>"
            f"<td style='color:{colour};font-weight:700'>{rate}%</td>"
            f"</tr>"
        )
    return "\n".join(rows)


def _python_version() -> str:
    import sys
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"
