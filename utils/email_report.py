"""
Email-safe run report.

Builds the subject line and HTML body for the post-run email. Pure functions
over data the session already computes — no SMTP, no Playwright, no file IO
except reading artifacts that are already on disk. That keeps it unit-testable
without a mail server.

WHY THIS IS NOT THE DASHBOARD HTML
----------------------------------
reports/trend/dashboard.html cannot be the email body. Mail clients strip
<style> blocks (Gmail partially, Outlook aggressively), do not support flex or
grid, and drop JavaScript — so the dashboard's theme toggle, filters and
collapsible sections all die, and it renders as an unstyled wall of text.

So the body below is deliberately old-fashioned: tables for layout, styles
inlined on every element, no scripts, no external images. The dashboard is
ATTACHED for anyone who wants the interactive version.

WHAT IS DELIBERATELY NOT ATTACHED
---------------------------------
reports/failures/ — 144MB at the time of writing, individual folders up to
2.9MB, and the auth-flow DOM captures contain the full OAuth URL including
`state` and `code_challenge`. Attaching those would exfiltrate them to every
recipient. The summary artifacts total ~64KB, which is what ATTACHMENTS lists.
tests/unit/test_email_report.py has a regression test asserting no
reports/failures path can appear in a payload.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

TREND_DIR = Path("reports/trend")

# Summary artifacts only — small, and free of captured page state.
ATTACHMENTS: tuple[Path, ...] = (
    TREND_DIR / "report-printable.html",
    TREND_DIR / "dashboard.html",
    TREND_DIR / "python-health-snapshot.json",
    TREND_DIR / "run_summary" / "ai-cost.json",
    TREND_DIR / "run_summary" / "network_overview.json",
)

# Anything under here must never be attached or linked. See module docstring.
FORBIDDEN_PATH_FRAGMENTS = ("reports/failures", "recordings")

_MAX_FAILURE_ROWS = 25


@dataclass
class EmailPayload:
    subject: str
    html_body: str
    text_body: str
    attachments: list[Path] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION


# ── Helpers ────────────────────────────────────────────────────────────


def strip_query(url: str | None) -> str:
    """Drop the query string from a URL.

    Failure URLs in this product routinely carry OAuth `state` and
    `code_challenge` parameters. The path is what makes a failure legible; the
    query is only a leak risk in an email.
    """
    if not url:
        return ""
    return url.split("?", 1)[0]


def _esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def existing_attachments(paths: Sequence[Path] = ATTACHMENTS) -> list[Path]:
    """The subset of `paths` that exists, with forbidden paths filtered out.

    The filter is belt-and-braces: ATTACHMENTS is a fixed list, but this makes
    the guarantee hold even if a caller passes its own paths.
    """
    out: list[Path] = []
    for p in paths:
        s = str(p).replace("\\", "/")
        if any(bad in s for bad in FORBIDDEN_PATH_FRAGMENTS):
            logger.warning("[email] Refusing to attach %s — forbidden path.", p)
            continue
        if p.exists():
            out.append(p)
    return out


# ── Subject ────────────────────────────────────────────────────────────


def build_subject(stats: dict, health: int | None, when: datetime | None = None) -> str:
    """
    Outcome-first subject, so it is readable without opening the mail.

        [FlowGuard] PASS 196/196 · health 100 · 11 Aug 12:37
        [FlowGuard] FAIL 14 of 196 · health 50 · 11 Aug 10:45
    """
    when = when or datetime.now()
    total = int(stats.get("total", 0) or 0)
    failed = int(stats.get("failed", 0) or 0)
    passed = int(stats.get("passed", 0) or 0)
    stamp = when.strftime("%d %b %H:%M")

    if total == 0:
        head = "NO TESTS"
    elif failed:
        head = f"FAIL {failed} of {total}"
    else:
        head = f"PASS {passed}/{total}"

    bits = [f"[FlowGuard] {head}"]
    if health is not None:
        bits.append(f"health {health}")
    bits.append(stamp)
    return " · ".join(bits)


# ── Body ───────────────────────────────────────────────────────────────


def _failure_rows(records: Sequence[Any]) -> list[dict[str, str]]:
    """One row per failed test, enriched from its triage.json if present."""
    rows: list[dict[str, str]] = []
    for r in records:
        if getattr(r, "status", "") != "FAIL":
            continue
        row = {
            "test": str(getattr(r, "method", "") or ""),
            "feature": str(getattr(r, "feature", "") or ""),
            "category": "",
            "severity": "",
            "diagnosis": "",
        }
        folder = getattr(r, "artifact_folder", None)
        if folder:
            triage = _read_json(Path("reports/failures") / str(folder) / "triage.json")
            row["category"] = str(triage.get("category", "") or "")
            row["severity"] = str(triage.get("severity", "") or "")
            row["diagnosis"] = str(triage.get("diagnosis", "") or "")
        rows.append(row)
    return rows


def build_html_body(
    stats: dict,
    decision: Any,
    scores: Any = None,
    records: Sequence[Any] = (),
    exec_summary: str = "",
    cost: dict | None = None,
) -> str:
    """
    Table-based, fully inline-styled HTML. Intentionally plain — see the module
    docstring for why the dashboard markup can't be reused here.
    """
    cost = cost or {}
    session = cost.get("session", {}) if isinstance(cost, dict) else {}

    label = _esc(getattr(decision, "label", "—"))
    colour = getattr(decision, "color", "#0f172a")
    verdict_bg = getattr(decision, "bg", "#f8fafc")

    total = int(stats.get("total", 0) or 0)
    failed = int(stats.get("failed", 0) or 0)

    def cell(v, bold=False, colour_="#0f172a"):
        weight = "700" if bold else "400"
        return (
            f'<td style="padding:6px 10px;border-bottom:1px solid #e2e8f0;'
            f'font-family:Arial,Helvetica,sans-serif;font-size:13px;'
            f'color:{colour_};font-weight:{weight}">{v}</td>'
        )

    stat_cells = "".join(
        f'<td align="center" style="padding:10px 14px;border:1px solid #e2e8f0;'
        f'font-family:Arial,Helvetica,sans-serif">'
        f'<div style="font-size:11px;color:#64748b;text-transform:uppercase;'
        f'letter-spacing:.5px">{_esc(k)}</div>'
        f'<div style="font-size:22px;font-weight:700;color:{c}">{_esc(v)}</div></td>'
        for k, v, c in (
            ("Total", total, "#334155"),
            ("Passed", stats.get("passed", 0), "#16a34a"),
            ("Failed", failed, "#dc2626" if failed else "#64748b"),
            ("Skipped", stats.get("skipped", 0), "#64748b"),
            ("Pass rate", f"{stats.get('pass_rate', 0)}%", "#0369a1"),
        )
    )

    rows = _failure_rows(records)
    truncated = max(0, len(rows) - _MAX_FAILURE_ROWS)
    if rows:
        body_rows = "".join(
            "<tr>"
            + cell(_esc(r["test"]), bold=True)
            + cell(_esc(r["feature"]))
            + cell(
                f'{_esc(r["category"])}'
                + (f' / {_esc(r["severity"])}' if r["severity"] else ""),
                colour_="#b45309",
            )
            + cell(_esc(r["diagnosis"]))
            + "</tr>"
            for r in rows[:_MAX_FAILURE_ROWS]
        )
        head = "".join(
            f'<th align="left" style="padding:6px 10px;background:#f1f5f9;'
            f'border-bottom:2px solid #cbd5e1;font-family:Arial,Helvetica,sans-serif;'
            f'font-size:11px;color:#475569;text-transform:uppercase">{h}</th>'
            for h in ("Test", "Feature", "Triage", "Diagnosis")
        )
        failures_block = (
            '<h3 style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
            'color:#0f172a;margin:22px 0 8px">Failures</h3>'
            '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
            'style="border-collapse:collapse">'
            f"<tr>{head}</tr>{body_rows}</table>"
            + (
                f'<p style="font-family:Arial,Helvetica,sans-serif;font-size:12px;'
                f'color:#64748b">…and {truncated} more. Full detail in the '
                f"attached dashboard.</p>"
                if truncated
                else ""
            )
        )
    else:
        failures_block = (
            '<p style="font-family:Arial,Helvetica,sans-serif;font-size:13px;'
            'color:#16a34a;margin:22px 0 0">No failures in this run.</p>'
        )

    health_block = ""
    if scores is not None:
        health_block = (
            '<p style="font-family:Arial,Helvetica,sans-serif;font-size:13px;'
            'color:#334155;margin:14px 0 0">'
            f"<strong>Health {_esc(getattr(scores, 'overall', '—'))}/100</strong>"
            f" &nbsp;·&nbsp; product {_esc(getattr(scores, 'product_health', '—'))}"
            f" &nbsp;·&nbsp; infra {_esc(getattr(scores, 'infra_health', '—'))}"
            f" &nbsp;·&nbsp; framework {_esc(getattr(scores, 'framework_health', '—'))}"
            "</p>"
        )

    summary_block = ""
    if exec_summary:
        summary_block = (
            f'<div style="background:#f8fafc;border-left:4px solid #0ea5e9;'
            f'padding:12px 16px;margin:18px 0">'
            f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:10px;'
            f'font-weight:700;color:#0284c7;text-transform:uppercase;'
            f'letter-spacing:1px;margin-bottom:4px">Summary</div>'
            f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;'
            f'color:#1e293b;line-height:1.5">{_esc(exec_summary)}</div></div>'
        )

    cost_block = ""
    if session.get("calls"):
        cost_block = (
            '<p style="font-family:Arial,Helvetica,sans-serif;font-size:12px;'
            'color:#64748b;margin:16px 0 0">'
            f"AI spend this run: <strong>${session.get('usd', 0):.4f}</strong> "
            f"across {_esc(session.get('calls'))} call(s)."
            "</p>"
        )

    return (
        '<div style="max-width:760px;margin:0 auto;padding:18px;'
        'background:#ffffff">'
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:19px;'
        'font-weight:700;color:#0f172a">Flozic FlowGuard — Run Report</div>'
        f'<div style="display:inline-block;margin:12px 0;padding:6px 14px;'
        f'background:{verdict_bg};border:2px solid {colour}55;border-radius:6px;'
        f'font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:700;'
        f'color:{colour}">{label}</div>'
        f"{summary_block}"
        '<table cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:collapse;margin-top:6px">'
        f"<tr>{stat_cells}</tr></table>"
        f"{health_block}{failures_block}{cost_block}"
        '<p style="font-family:Arial,Helvetica,sans-serif;font-size:11px;'
        'color:#94a3b8;margin-top:24px;border-top:1px solid #e2e8f0;'
        'padding-top:10px">Generated by FlowGuard. The interactive dashboard is '
        'attached; per-failure artifacts (screenshots, DOM, video) stay on the '
        'run machine and are not emailed.</p>'
        "</div>"
    )


def build_text_body(stats: dict, decision: Any, records: Sequence[Any] = ()) -> str:
    """Plain-text alternative. Required: a multipart/alternative without one
    looks like spam to several filters, and some clients prefer it."""
    lines = [
        "Flozic FlowGuard — Run Report",
        f"Verdict: {getattr(decision, 'label', '—')}",
        "",
        f"Total {stats.get('total', 0)} · passed {stats.get('passed', 0)} · "
        f"failed {stats.get('failed', 0)} · skipped {stats.get('skipped', 0)} "
        f"· pass rate {stats.get('pass_rate', 0)}%",
    ]
    rows = _failure_rows(records)
    if rows:
        lines += ["", "Failures:"]
        for r in rows[:_MAX_FAILURE_ROWS]:
            tag = " / ".join(x for x in (r["category"], r["severity"]) if x)
            lines.append(f"  - {r['test']} [{tag}] {r['diagnosis']}".rstrip())
    else:
        lines += ["", "No failures in this run."]
    return "\n".join(lines)


def build(
    stats: dict,
    decision: Any,
    scores: Any = None,
    records: Sequence[Any] = (),
    exec_summary: str = "",
    when: datetime | None = None,
) -> EmailPayload:
    """Assemble the full payload."""
    cost = _read_json(TREND_DIR / "run_summary" / "ai-cost.json")
    health = getattr(scores, "overall", None) if scores is not None else None
    return EmailPayload(
        subject=build_subject(stats, health, when=when),
        html_body=build_html_body(
            stats, decision, scores=scores, records=records,
            exec_summary=exec_summary, cost=cost,
        ),
        text_body=build_text_body(stats, decision, records=records),
        attachments=existing_attachments(),
    )
