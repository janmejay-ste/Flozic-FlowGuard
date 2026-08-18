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
import json
import os
from datetime import datetime, timezone


class MobileReportCollector:
    _SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2}
    _SEVERITY_COLOR = {"blocker": "#dc2626", "major": "#ea580c", "minor": "#ca8a04"}

    def __init__(self):
        self.findings = []  # list of Inconsistency.to_dict()
        self.pages_tested = set()
        self.devices_tested = set()

    def record(self, findings):
        for f in findings:
            self.findings.append(f.to_dict())
            self.pages_tested.add(f.page)
            self.devices_tested.add(f.device)

    def _sorted_findings(self):
        return sorted(
            self.findings,
            key=lambda f: (f["page"], self._SEVERITY_ORDER.get(f["severity"], 9), f["device"]),
        )

    def summary(self):
        by_severity = {"blocker": 0, "major": 0, "minor": 0}
        for f in self.findings:
            by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "pages_tested": sorted(self.pages_tested),
            "devices_tested": sorted(self.devices_tested),
            "total_findings": len(self.findings),
            "by_severity": by_severity,
        }

    def write(self, out_dir="reports/mobile"):
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
        rows = "\n".join(
            f"""<tr>
                <td>{f['page']}</td>
                <td>{f['device']}</td>
                <td>{f['category']}</td>
                <td><span class="badge" style="background:{self._SEVERITY_COLOR[f['severity']]}">{f['severity']}</span></td>
                <td>{f['message']}</td>
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
  <div class="meta">Generated {summary['generated_at']} · Pages: {', '.join(summary['pages_tested']) or '—'} ·
    Devices: {', '.join(summary['devices_tested']) or '—'}</div>
  <div class="summary">
    <div class="card"><b>{summary['total_findings']}</b>Total findings</div>
    <div class="card"><b style="color:#dc2626">{summary['by_severity'].get('blocker', 0)}</b>Blocker</div>
    <div class="card"><b style="color:#ea580c">{summary['by_severity'].get('major', 0)}</b>Major</div>
    <div class="card"><b style="color:#ca8a04">{summary['by_severity'].get('minor', 0)}</b>Minor</div>
  </div>
  <table>
    <thead><tr><th>Page</th><th>Device</th><th>Category</th><th>Severity</th><th>Finding</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="5">No inconsistencies found 🎉</td></tr>'}</tbody>
  </table>
</body></html>"""
