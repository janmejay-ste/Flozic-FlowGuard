"""
utils/mobile_report_builder.py

Collects Inconsistency records emitted during a mobile test run and writes
a single self-contained HTML file — same "works offline from file://,
no build step" philosophy as utils/dashboard_builder.py, kept deliberately
separate/lighter so this can run standalone in the mobile branch before
it's wired into the main dashboard.

Output: reports/mobile/mobile-layout-report.html + a parallel .json
(the JSON is what a future integration with dashboard_builder.py or
failure_clustering.py would consume).
"""
import html
import json
import os
from datetime import datetime, timezone


# Session-wide accumulator so layered_health_scores can see mobile findings.
#
# WHY A MODULE GLOBAL: the collector is a session fixture that writes its own
# report at fixture teardown, while scoring happens in pytest_sessionfinish.
# Reading the JSON back off disk would risk scoring a STALE file from a
# previous run, which is exactly the sort of silent wrong answer this project
# keeps having to dig out. Process-local state cannot go stale.
_SESSION_FINDINGS: list[dict] = []

# Devices actually exercised this process. Tracked SEPARATELY from findings
# because "mobile ran and found nothing" and "mobile never ran" are different
# facts with opposite meanings, and findings alone cannot tell them apart. A
# clean mobile run must score 100, not "not measured".
_SESSION_DEVICES: set[str] = set()

# Interaction-check execution tally, per engine. Findings alone CANNOT yield
# an executed-coverage number: a check that runs clean emits nothing, so
# counting findings under-counts execution. The auditor reports every check
# attempt here, with executed=False when the check could only record an
# UNTESTED/UNSUPPORTED/NOT-verified notice. "Executed 62%" and "found no
# issues" are different claims; this is what keeps them separate.
_SESSION_CHECKS: dict[str, dict[str, int]] = {}


def session_findings() -> list[dict]:
    """Every mobile finding recorded this process. Empty if none were raised."""
    return list(_SESSION_FINDINGS)


def note_device_exercised(device_name: str) -> None:
    """Record that a mobile device profile was actually driven this run.

    Called from the mobile_context fixture, so it fires even for a test that
    raises no findings at all.
    """
    if device_name:
        _SESSION_DEVICES.add(device_name)


def session_devices() -> set[str]:
    """Device profiles exercised this process. Empty => no mobile tests ran."""
    return set(_SESSION_DEVICES)


def mobile_was_exercised() -> bool:
    """True when this run drove at least one mobile device profile."""
    return bool(_SESSION_DEVICES) or bool(_SESSION_FINDINGS)


def note_check_run(engine: str, executed: bool, structural: bool = False) -> None:
    """Record one interaction-check attempt.

    executed=True  -> ran to a real conclusion (incl. content-driven N/A).
    executed=False, structural=True  -> engine literally cannot run it
        (UNTESTED/UNSUPPORTED). Excluded from the coverage denominator.
    executed=False, structural=False -> attempted but crashed/flaked
        ("NOT verified"). A real coverage gap that discounts the score.
    """
    e = _SESSION_CHECKS.setdefault(
        engine or "?", {"attempted": 0, "executed": 0, "na_structural": 0})
    e["attempted"] += 1
    if executed:
        e["executed"] += 1
    elif structural:
        e["na_structural"] += 1


def session_check_stats() -> dict[str, dict[str, int]]:
    """Per-engine {attempted, executed} interaction-check tallies."""
    return {k: dict(v) for k, v in _SESSION_CHECKS.items()}


def reset_session_findings() -> None:
    """Test-only hook."""
    _SESSION_FINDINGS.clear()
    _SESSION_DEVICES.clear()
    _SESSION_CHECKS.clear()


class MobileReportCollector:
    _SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2, "info": 3}
    _SEVERITY_COLOR = {
        "blocker": "#dc2626", "major": "#ea580c", "minor": "#ca8a04",
        # info = "this check could not apply here" (no carousel, no chat
        # widget). Recorded rather than skipped silently, so an absent check
        # never reads as a passing one.
        "info": "#0284c7",
    }
    _UNKNOWN_COLOR = "#64748b"

    def __init__(self, engine: str = "chromium"):
        self.findings = []  # list of Inconsistency.to_dict()
        self.pages_tested = set()
        self.devices_tested = set()
        # Engine is part of the output path. Without this, a WebKit run
        # overwrites the Chromium report and a cross-engine comparison becomes
        # impossible -- which is exactly what happened: a Chromium run clobbered
        # the WebKit findings 13 minutes after they were produced. Mirrors
        # conftest._report_root: chromium keeps the bare path, others nest.
        self.engine = engine or "chromium"

    def record(self, findings):
        for f in findings:
            d = f.to_dict()
            # Stamp the engine on every finding: the dashboard table and the
            # printable report both show cross-engine data, and a finding that
            # can't say which engine produced it is ambiguous evidence.
            d["engine"] = self.engine
            self.findings.append(d)
            _SESSION_FINDINGS.append(d)
            self.pages_tested.add(f.page)
            self.devices_tested.add(f.device)

    def _sorted_findings(self):
        return sorted(
            self.findings,
            key=lambda f: (f["page"], self._SEVERITY_ORDER.get(f["severity"], 9), f["device"]),
        )

    def summary(self):
        by_severity = {"blocker": 0, "major": 0, "minor": 0, "info": 0}
        for f in self.findings:
            by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "engine": self.engine,
            "pages_tested": sorted(self.pages_tested),
            "devices_tested": sorted(self.devices_tested),
            "total_findings": len(self.findings),
            "by_severity": by_severity,
        }

    def summary_path(self, base="reports/trend") -> str:
        """Engine-namespaced path for the machine-readable summary sidecar.

        Mirrors conftest._report_root: chromium keeps the bare trend dir,
        other engines nest. This sidecar exists because the PDF/printable
        export must be able to include OTHER engines' latest mobile results —
        a WebKit run's findings otherwise live only in that run's process
        memory and can never reach a later export. It is a DATA file, not a
        report: the dashboard remains the single human surface.
        """
        d = base if self.engine == "chromium" else os.path.join(base, self.engine)
        return os.path.join(d, "mobile-summary.json")

    def write_summary(self, base="reports/trend") -> str:
        path = self.summary_path(base)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"schema_version": 1, **self.summary(),
                       # Additive: {attempted, executed} for THIS engine, so
                       # cross-engine readers can show "% executed" without
                       # mistaking untested checks for clean ones. None when
                       # nothing was instrumented (e.g. layout-only sessions).
                       "check_stats": session_check_stats().get(self.engine),
                       "findings": self._sorted_findings()}, fh, indent=2)
        return path

    def report_dir(self, base="reports/mobile") -> str:
        """chromium -> reports/mobile/ ; webkit -> reports/mobile/webkit/"""
        return base if self.engine == "chromium" else os.path.join(base, self.engine)

    def write(self, out_dir=None):
        out_dir = out_dir or self.report_dir()
        os.makedirs(out_dir, exist_ok=True)
        summary = self.summary()

        json_path = os.path.join(out_dir, "mobile-layout-report.json")
        with open(json_path, "w") as fh:
            json.dump({"summary": summary, "findings": self._sorted_findings()}, fh, indent=2)

        html_path = os.path.join(out_dir, "mobile-layout-report.html")
        with open(html_path, "w") as fh:
            fh.write(self._render_html(summary))

        return json_path, html_path

    def _render_html(self, summary):
        def cell(v):
            return html.escape(str(v if v is not None else ""))

        rows = "\n".join(
            f"""<tr>
                <td>{cell(f['page'])}</td>
                <td>{cell(f['device'])}</td>
                <td>{cell(f['category'])}</td>
                <td><span class="badge" style="background:{
                    self._SEVERITY_COLOR.get(f['severity'], self._UNKNOWN_COLOR)
                }">{cell(f['severity'])}</span></td>
                <td>{cell(f['message'])}</td>
            </tr>"""
            for f in self._sorted_findings()
        )
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Mobile Layout Report — Flozic FlowGuard</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; color: #1e293b; }}
  h1 {{ margin-bottom: 0.25rem; }}
  .meta {{ color: #64748b; margin-bottom: 1.5rem; }}
  .summary {{ display: flex; gap: 1rem; margin-bottom: 1.5rem; }}
  .card {{ padding: 0.75rem 1.25rem; border-radius: 8px; background: #f1f5f9; }}
  .card b {{ font-size: 1.4rem; display: block; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border-bottom: 1px solid #e2e8f0; padding: 0.5rem 0.75rem; text-align: left; font-size: 0.9rem; }}
  th {{ background: #f8fafc; position: sticky; top: 0; }}
  .badge {{ color: white; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; text-transform: uppercase; }}
</style></head>
<body>
  <h1>📱 Mobile Layout Report — Flozic FlowGuard</h1>
  <div class="meta">Generated {summary['generated_at']} · Engine: <b>{summary.get('engine', '?')}</b> · Pages: {', '.join(summary['pages_tested']) or '—'} ·
    Devices: {', '.join(summary['devices_tested']) or '—'}</div>
  <div class="summary">
    <div class="card"><b>{summary['total_findings']}</b>Total findings</div>
    <div class="card"><b style="color:#dc2626">{summary['by_severity'].get('blocker', 0)}</b>Blocker</div>
    <div class="card"><b style="color:#ea580c">{summary['by_severity'].get('major', 0)}</b>Major</div>
    <div class="card"><b style="color:#ca8a04">{summary['by_severity'].get('minor', 0)}</b>Minor</div>
    <div class="card"><b style="color:#0284c7">{summary['by_severity'].get('info', 0)}</b>Not applicable</div>
  </div>
  <table>
    <thead><tr><th>Page</th><th>Device</th><th>Category</th><th>Severity</th><th>Finding</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="5">No inconsistencies found 🎉</td></tr>'}</tbody>
  </table>
</body></html>"""


def load_engine_summaries(base="reports/trend") -> list[dict]:
    """Latest persisted mobile summary per engine, for cross-engine REPORTING.

    Reads chromium's sidecar from the trend root and every engine subdir's
    sidecar. Never used for scoring — each summary is a point-in-time snapshot
    of that engine's last mobile run and carries its own generated_at so
    staleness is visible rather than silent.
    """
    out = []
    candidates = [os.path.join(base, "mobile-summary.json")]
    try:
        for entry in sorted(os.listdir(base)):
            sub = os.path.join(base, entry, "mobile-summary.json")
            if os.path.isfile(sub):
                candidates.append(sub)
    except OSError:
        pass
    for path in candidates:
        try:
            with open(path) as fh:
                d = json.load(fh)
            if isinstance(d, dict) and d.get("engine"):
                out.append(d)
        except (OSError, ValueError):
            continue
    return out
