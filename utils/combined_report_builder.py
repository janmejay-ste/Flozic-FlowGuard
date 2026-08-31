"""
Combined cross-engine FlowGuard report — HYBRID layout.

Reads each engine's PERSISTED artifacts (latest snapshot, mobile-summary.json,
and — when present — the report-data.json sidecar) and renders ONE report:

    Executive Summary   (per-engine scores + release decision, side by side)
    Engine Comparison   (mobile scores; shared / engine-only findings, from data)
    Cross-Engine Analysis
        Issues to Triage      (every FAIL, tagged with its engine)
        Systemic Concentration(features with clustered failures, per engine)
        Recurring Failures    (from run history)
        Login Route Observations   (sidecar — pending until persistence lands)
    Chromium   (Mobile · Health · JS Errors)
    WebKit     (Mobile · Health · JS Errors)

Design rules (from the approved Hybrid spec):
  * Every metric carries an explicit engine + run-timestamp PROVENANCE badge,
    so a number can never be misread as belonging to another engine.
  * shared / chromium-only / webkit-only is COMPUTED FROM THE DATA, and a test
    that simply was not executed on an engine is labelled "not run", never
    silently turned into an engine-specific defect.
  * Each section has an Export-PDF button that prints only that section
    (client-side print stylesheet — no weasyprint / GTK dependency).

Additive: this module never touches the per-engine dashboard pipeline. It is a
read-only consumer of what the runs already persisted.
"""
from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape as esc
from pathlib import Path
from typing import Any

from utils.snapshot_writer import TestRecord
from utils.layered_health_scores import compute as compute_scores, LayeredScores, score_band

# Reuse the existing per-section renderers where their inputs are recoverable
# from persisted data (DRY — one source of truth for how a section looks).
from utils import dashboard_builder as _db

OUT_PATH = Path("reports/trend/combined-report.html")

# Engines we look for, in display order. chromium lives at the trend root;
# every other engine lives in its own subdir.
_ENGINES = ("chromium", "webkit")


# ─────────────────────────────────────────────────────────────────────────────
# Data loading (post-hoc, from disk)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EngineData:
    engine: str
    present: bool = False
    snapshot_path: str | None = None
    run_ms: int | None = None          # snapshot timestamp (ms)
    records: list[TestRecord] = field(default_factory=list)
    mobile: dict | None = None         # mobile-summary.json
    layered: LayeredScores | None = None   # recomputed — TRUSTED ONLY for mobile sub-scores
    sidecar: dict | None = None        # report-data.json (login routes, js clusters, authoritative domains) — may be absent
    auth: dict | None = None           # latest total>0 trend row: authoritative overall/mobile/status

    @property
    def run_label(self) -> str:
        if not self.run_ms:
            return "no run"
        return datetime.fromtimestamp(self.run_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    @property
    def total(self) -> int:
        return len(self.records)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.records if r.status == "FAIL")


def _engine_root(engine: str, base: str) -> str:
    return base if engine == "chromium" else os.path.join(base, engine)


def _load_trend_auth(root: str) -> dict | None:
    """Latest COMPLETED (total>0) trend-history row for this engine — the
    AUTHORITATIVE overall/mobile/status the real run computed in-session.
    We read these rather than recompute, because Product/Infra/Framework
    health depend on in-session JS ErrorClusters that snapshots don't persist,
    so a recompute would silently disagree with the real dashboard.
    """
    path = os.path.join(root, "trend-history.json")
    try:
        rows = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rows = rows if isinstance(rows, list) else rows.get("entries", [])
    good = [r for r in rows if (r.get("total") or 0) > 0]
    return good[-1] if good else None


def _records_from_snapshot(path: str) -> tuple[list[TestRecord], int | None]:
    """Reconstruct TestRecords from a persisted snapshot's `tests` array.
    Snapshot keys: category/login/feature/class/method/status/duration/cohort
    (+videoPath). harness_fault is not persisted in v3 snapshots → default False.
    """
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return [], None
    recs: list[TestRecord] = []
    for t in d.get("tests") or []:
        recs.append(TestRecord(
            category=t.get("category", ""),
            login=t.get("login", ""),
            feature=t.get("feature", ""),
            clazz=t.get("class", ""),
            method=t.get("method", ""),
            status=t.get("status", ""),
            duration_ms=int(t.get("duration", 0) or 0),
            cohort=t.get("cohort", "baseline"),
            video_path=t.get("videoPath"),
            harness_fault=bool(t.get("harnessFault", False)),
            error_signature=t.get("errorSignature", ""),
            artifact_folder=t.get("artifacts"),
        ))
    return recs, d.get("timestamp")


def load_engine(engine: str, base: str = "reports/trend") -> EngineData:
    root = _engine_root(engine, base)
    ed = EngineData(engine=engine)

    # Pick the latest NON-EMPTY snapshot. A pytest session always archives a
    # snapshot at teardown — including unit-only runs that executed 0 browser
    # tests — so the newest file on disk may be an EMPTY run. Reading that would
    # wipe this engine's data in the report; skip EMPTY snapshots (same
    # run-hygiene rule the trend/flake layers follow).
    snaps = sorted(glob.glob(os.path.join(root, "snapshots", "*.json")), key=os.path.getmtime)
    for snap in reversed(snaps):
        recs, run_ms = _records_from_snapshot(snap)
        if recs:
            ed.snapshot_path, ed.records, ed.run_ms, ed.present = snap, recs, run_ms, True
            break
    else:
        if snaps:  # only empty snapshots exist — record presence but no data
            ed.snapshot_path = snaps[-1]
            _, ed.run_ms = _records_from_snapshot(snaps[-1])

    ms_path = os.path.join(root, "mobile-summary.json")
    if os.path.isfile(ms_path):
        try:
            ed.mobile = json.load(open(ms_path, encoding="utf-8"))
        except (OSError, ValueError):
            ed.mobile = None

    sc_path = os.path.join(root, "report-data.json")
    if os.path.isfile(sc_path):
        try:
            ed.sidecar = json.load(open(sc_path, encoding="utf-8"))
        except (OSError, ValueError):
            ed.sidecar = None

    ed.auth = _load_trend_auth(root)

    # Recompute layered scores from what we have (records + mobile findings/coverage).
    if ed.present:
        mf = (ed.mobile or {}).get("findings") if ed.mobile else None
        cs = (ed.mobile or {}).get("check_stats") if ed.mobile else None
        try:
            ed.layered = compute_scores(
                ed.records, [],
                mobile_findings=mf,
                mobile_tested=bool(mf is not None),
                check_stats=cs,
            )
        except Exception:
            ed.layered = None
    return ed


# ─────────────────────────────────────────────────────────────────────────────
# Small presentational helpers
# ─────────────────────────────────────────────────────────────────────────────

def _truncate_url(url: str, limit: int = 80) -> str:
    """Hard string truncation for display — CSS max-width on a td doesn't
    constrain table layout, so a long URL would blow the table out of its
    card. Full URL stays in the cell's title tooltip."""
    return url if len(url) <= limit else url[:limit] + "…"


def _prov(engine: str, run_label: str, extra: str = "") -> str:
    """Provenance chip — engine + run timestamp on every metric/section."""
    colour = {"chromium": "#1a73e8", "webkit": "#8e44ad"}.get(engine, "#64748b")
    tail = f" · {esc(extra)}" if extra else ""
    return (
        f"<span class='prov' style='border-color:{colour};color:{colour}'>"
        f"{esc(engine)} · {esc(run_label)}{tail}</span>"
    )


def _section(sec_id: str, title: str, body: str, prov: str = "") -> str:
    """Wrap a section with an id, a title, a provenance chip, and an
    Export-PDF button that prints only this section."""
    return f"""
<section class="fg-section" id="{sec_id}">
  <div class="fg-sec-head">
    <h2>{title} {prov}</h2>
    <button class="fg-export-btn" onclick="exportSection('{sec_id}')" title="Export just this section to PDF">⤓ Export PDF</button>
  </div>
  {body}
</section>"""


def _pending(section_name: str, source: str) -> str:
    return (
        f"<div class='pending'>⏳ <strong>{esc(section_name)}</strong> is not yet in the "
        f"combined report because it lives only in <code>{esc(source)}</code> (in-session "
        f"memory) and is not persisted to disk. It will appear here once the per-engine "
        f"<code>report-data.json</code> sidecar is written and each engine has run again.</div>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sections
# ─────────────────────────────────────────────────────────────────────────────

def _score_pill(label: str, val: int | None) -> str:
    if val is None:
        return f"<td class='sc'><span class='na'>n/a</span><div class='sl'>{esc(label)}</div></td>"
    band_label, txt, _bg = score_band(val)
    return (
        f"<td class='sc'><span class='scv' style='color:{txt}'>{val}</span>"
        f"<div class='sl'>{esc(label)}</div>"
        f"<div class='sb' style='color:{txt}'>{esc(band_label)}</div></td>"
    )


_REL_RANK = {"BLOCKED": 4, "AT_RISK": 3, "INCONCLUSIVE": 3, "WARNING": 2, "READY": 1, "—": 0}


def _is_full(auth: dict | None) -> bool:
    """A run counts as full-suite-equivalent (its release verdict stands for
    the whole product) only when it executed the regression population."""
    return bool(auth and (auth.get("total") or 0) > 200)


def _overall_release(engines: list[EngineData]) -> str:
    """Release = the WORST engine decision. A full-suite BLOCK blocks release;
    a partial-scope engine can never upgrade that."""
    worst, rank = "—", -1
    for ed in engines:
        s = str((ed.auth or {}).get("status", "—")).upper()
        if _REL_RANK.get(s, 0) > rank:
            rank, worst = _REL_RANK.get(s, 0), s
    return worst


def _engine_status_label(auth: dict) -> tuple[str, str]:
    """Full-suite runs keep their real verdict. A partial-scope run's green is
    NOT proof the product is releasable — only that its executed scope passed,
    so it is labelled 'PASSING — EXECUTED SCOPE', never a bare READY."""
    status = str(auth.get("status", "—")).upper()
    if _is_full(auth):
        return status, status.lower()
    failed = auth.get("failed") or 0
    return ("PASSING — EXECUTED SCOPE" if failed == 0 else "ISSUES — EXECUTED SCOPE"), "scope"


def _eligibility(engines: list[EngineData]) -> str:
    suites = {("full" if _is_full(ed.auth) else "partial") for ed in engines if ed.auth}
    return "Full" if suites == {"full"} else "Partial"


def _release_reason(engines: list[EngineData], overall: str) -> str:
    """Explain the release verdict by naming the run that drove it, rather than
    the terse 'worst engine decision'. Prefer a full-suite driver."""
    drivers = [ed for ed in engines
               if str((ed.auth or {}).get("status", "")).upper() == overall]
    full_driver = next((ed for ed in drivers if _is_full(ed.auth)), None)
    if full_driver:
        return (f"the full-suite {full_driver.engine} run is {overall}; "
                "partial-scope coverage on other engines cannot override a full-suite decision")
    if drivers:
        return (f"{drivers[0].engine} is {overall} (no full-suite run to override it — "
                "treat this as provisional until a full suite runs)")
    return "no completed run to decide from"


_SEV_CHIP = {
    "blocker": ("#b91c1c", "#fee2e2"), "major": ("#c2410c", "#ffedd5"),
    "minor": ("#92400e", "#fef3c7"), "info": ("#475569", "#f1f5f9"),
}


def _sev_chip(sev: str) -> str:
    c, bg = _SEV_CHIP.get(str(sev).lower(), ("#475569", "#f1f5f9"))
    return (f"<span class='sev-chip' style='color:{c};background:{bg}'>"
            f"{esc(str(sev).upper())}</span>")


def _mobile_stats(ed: EngineData) -> dict:
    """Per-engine mobile numbers for the overview table (same sources as the
    mobile strip: authoritative score, deterministic quality/coverage, counts)."""
    out = {"score": None, "quality": None, "coverage": None,
           "blocker": 0, "major": 0, "minor": 0, "info": 0, "rules": None}
    out["score"] = (ed.auth or {}).get("mobile")
    if ed.layered is not None:
        out["quality"] = getattr(ed.layered, "mobile_quality", None)
        cov = getattr(ed.layered, "mobile_coverage", None)
        out["coverage"] = f"{round(cov * 100)}%" if isinstance(cov, (int, float)) else None
    findings = (ed.mobile or {}).get("findings") or []
    for f in findings:
        s = str(f.get("severity", "")).lower()
        if s in out:
            out[s] += 1
    try:
        from utils.ai_mobile_triage import group_findings
        groups = group_findings([f for f in findings if str(f.get("severity", "")).lower() != "info"])
        out["rules"] = len({(str(g.get("severity", "")).lower(), str(g.get("category", ""))) for g in groups})
    except Exception:
        pass
    return out


def _render_engine_overview(engines: list[EngineData]) -> str:
    """Side-by-side Chromium | WebKit metric table at the top of the report:
    Health Score / Test Results / Mobile rows, one column per engine."""
    def cell_score(v):
        if not isinstance(v, int):
            return "<td class='num muted'>n/a</td>"
        bl, txt, bg = score_band(v)
        return (f"<td class='num' style='background:{bg}'><strong style='color:{txt}'>{v}</strong>"
                f" <span style='color:{txt};font-size:10px;font-weight:700'>{esc(bl)}</span></td>")

    def cell(v):
        return f"<td class='num'>{esc(str(v)) if v is not None else '<span class=muted>n/a</span>'}</td>"

    cols, status_cells, rowsets = [], [], []
    for ed in engines:
        auth = ed.auth or {}
        hs = (ed.sidecar or {}).get("health") or {}
        recs = ed.records
        total = len(recs); passed = sum(1 for r in recs if r.status == "PASS")
        failed = sum(1 for r in recs if r.status == "FAIL"); skipped = sum(1 for r in recs if r.status == "SKIP")
        rate = f"{round(passed / total * 100, 1)}%" if total else None
        label, cls = _engine_status_label(auth) if auth else ("—", "scope")
        status_cells.append(f"<td class='num'><span class='rel rel-{cls}'>{esc(label)}</span></td>")
        cols.append(f"<th class='num'>{_prov(ed.engine, ed.run_label)}</th>")
        m = _mobile_stats(ed)
        rowsets.append({
            "Overall": cell_score(auth.get("overall")),
            "Product Health": cell_score(hs.get("product")),
            "Infra Health": cell_score(hs.get("infrastructure")),
            "Framework Health": cell_score(hs.get("framework")),
            "Mobile": cell_score(auth.get("mobile")),
            "Total": cell(total or auth.get("total")),
            "Passed": cell(passed), "Failed": cell(failed), "Skipped": cell(skipped),
            "Pass rate": cell(rate),
            "Mobile score": cell(m["score"]), "Quality": cell(m["quality"]),
            "Coverage": cell(m["coverage"]), "Blocker": cell(m["blocker"]),
            "Major": cell(m["major"]), "Minor": cell(m["minor"]),
            "Info": cell(m["info"]), "Rules": cell(m["rules"]),
        })

    def group(title: str, keys: list[str]) -> str:
        n = len(engines) + 1
        rows = f"<tr class='ovr-grp'><td colspan='{n}'>{esc(title)}</td></tr>"
        for k in keys:
            rows += f"<tr><td>{esc(k)}</td>" + "".join(rs[k] for rs in rowsets) + "</tr>"
        return rows

    body = (
        f"<tr><td>Status</td>{''.join(status_cells)}</tr>"
        + group("Health Score", ["Overall", "Product Health", "Infra Health", "Framework Health", "Mobile"])
        + group("Test Results", ["Total", "Passed", "Failed", "Skipped", "Pass rate"])
        + group("Mobile", ["Mobile score", "Quality", "Coverage", "Blocker", "Major", "Minor", "Info", "Rules"])
    )
    return (
        "<table class='ovr'><thead><tr><th>Metric</th>" + "".join(cols)
        + f"</tr></thead><tbody>{body}</tbody></table>"
    )


def _render_exec_summary(engines: list[EngineData]) -> str:
    """Release banner + the side-by-side Chromium | WebKit metric table
    (Health Score / Test Results / Mobile rows, one column per engine)."""
    overall = _overall_release(engines)
    elig = _eligibility(engines)
    warn = (
        "" if elig == "Full" else
        "<div class='elig elig-partial'>⚠ <strong>Comparison eligibility: Partial.</strong> "
        "The engines executed <strong>different test populations</strong>, so the two Overall scores are "
        "<strong>not directly comparable</strong> — a higher number on a smaller scope is not a healthier product. "
        "See the coverage matrix in Engine Comparison.</div>"
    )
    return (
        f"<div class='rel-banner rel-{overall.lower()}'>RELEASE: {esc(overall)}"
        f"<span class='rel-why'>Release decision = {esc(overall)} because "
        f"{esc(_release_reason(engines, overall))}.</span></div>"
        f"{_render_engine_overview(engines)}"
        f"{warn}"
        "<p class='note'>Product / Infra / Framework per engine are shown in the per-engine sections below, "
        "and read ⏳ until the <code>report-data.json</code> sidecar persists them "
        "(they depend on in-session error-cluster data — never recomputed here).</p>"
    )


def _fp(r: TestRecord) -> tuple[str, str]:
    """Cross-engine fingerprint for a test: (class::method, feature)."""
    return (f"{r.clazz}::{r.method}", r.feature)


def _render_engine_comparison(chromium: EngineData, webkit: EngineData) -> str:
    c_run = {_fp(r) for r in chromium.records}
    w_run = {_fp(r) for r in webkit.records}
    c_fail = {_fp(r) for r in chromium.records if r.status == "FAIL"}
    w_fail = {_fp(r) for r in webkit.records if r.status == "FAIL"}

    shared_fail = sorted(c_fail & w_fail)
    # Chromium failed, and WebKit ran it and did NOT fail → genuine chromium-specific.
    chromium_specific = sorted(f for f in (c_fail - w_fail) if f in w_run)
    webkit_specific = sorted(f for f in (w_fail - c_fail) if f in c_run)
    # Chromium failed but WebKit never ran it → not comparable (population gap).
    chromium_notrun = sorted(f for f in c_fail if f not in w_run)
    webkit_notrun = sorted(f for f in w_fail if f not in c_run)

    c_mob = (chromium.auth or {}).get("mobile")
    w_mob = (webkit.auth or {}).get("mobile")

    comparable_exec = len(c_run & w_run)
    chromium_only_exec = len(c_run - w_run)
    webkit_only_exec = len(w_run - c_run)
    not_comparable = len(chromium_notrun) + len(webkit_notrun)
    elig = _eligibility([chromium, webkit])
    _cexe = ((chromium.mobile or {}).get("check_stats") or {}).get("executed")
    _wexe = ((webkit.mobile or {}).get("check_stats") or {}).get("executed")
    comparable_mobile = min(_cexe, _wexe) if isinstance(_cexe, int) and isinstance(_wexe, int) else None
    _basis = f"{comparable_exec} co-executed test(s)"
    if comparable_mobile:
        _basis += f" + {comparable_mobile} comparable mobile check(s)"

    def _lst(items: list[tuple[str, str]]) -> str:
        if not items:
            return "<li class='muted'>none</li>"
        return "".join(
            f"<li><code>{esc(m)}</code> <span class='feat'>{esc(feat)}</span></li>"
            for m, feat in items[:40]
        ) + ("" if len(items) <= 40 else f"<li class='muted'>+{len(items) - 40} more</li>")

    return f"""
<div class='elig elig-{elig.lower()}'>Comparison eligibility: <strong>{elig}</strong>
{'' if elig == 'Full' else '— engines ran different populations; only genuinely co-executed tests are compared below.'}</div>
<div class='basis'>⚖ Comparison basis: <strong>{_basis}</strong></div>
<div class='cmp-scores'>
  <div class='cmp-cell'>{_prov('chromium', chromium.run_label)}<div class='cmp-mob'>Mobile {c_mob if c_mob is not None else 'n/a'}</div></div>
  <div class='cmp-vs'>vs</div>
  <div class='cmp-cell'>{_prov('webkit', webkit.run_label)}<div class='cmp-mob'>Mobile {w_mob if w_mob is not None else 'n/a'}</div></div>
</div>
<div class='cmp-summary'>
  <div><span class='scv'>{comparable_exec}</span><div class='sl'>Tests comparable (ran on both)</div></div>
  <div><span class='scv'>{chromium_only_exec}</span><div class='sl'>Chromium-only execution</div></div>
  <div><span class='scv'>{webkit_only_exec}</span><div class='sl'>WebKit-only execution</div></div>
  <div><span class='scv'>{len(shared_fail)}</span><div class='sl'>Shared failures</div></div>
  <div><span class='scv'>{not_comparable}</span><div class='sl'>Not comparable</div></div>
</div>
<p class='note'>Four states, computed from what each engine actually executed — a test the other engine never ran is <strong>NOT COMPARABLE</strong>, never a pass and never an engine-specific defect.</p>
<div class='cmp-grid'>
  <div class='cmp-box shared'><h3>SHARED — both ran, both failed ({len(shared_fail)})</h3><ul>{_lst(shared_fail)}</ul></div>
  <div class='cmp-box'><h3>CHROMIUM-ONLY defect ({len(chromium_specific)})<br><small>both ran · Chromium failed · WebKit passed</small></h3><ul>{_lst(chromium_specific)}</ul></div>
  <div class='cmp-box'><h3>WEBKIT-ONLY defect ({len(webkit_specific)})<br><small>both ran · WebKit failed · Chromium passed</small></h3><ul>{_lst(webkit_specific)}</ul></div>
  <div class='cmp-box notrun'><h3>NOT COMPARABLE — Chromium failed, WebKit never ran it ({len(chromium_notrun)})</h3><ul>{_lst(chromium_notrun)}</ul></div>
  <div class='cmp-box notrun'><h3>NOT COMPARABLE — WebKit failed, Chromium never ran it ({len(webkit_notrun)})</h3><ul>{_lst(webkit_notrun)}</ul></div>
</div>
<h3 class='blk'>Comparison Coverage — what each engine actually executed</h3>
{_render_comparison_coverage(chromium, webkit)}"""


def _cov_cell(v: Any) -> str:
    return str(v) if v is not None else "—"


def _render_comparison_coverage(chromium: EngineData, webkit: EngineData) -> str:
    """Population matrix — makes it visually unavoidable WHY scores may not be
    comparable: the engines did not execute the same populations."""
    c = (chromium.mobile or {}).get("check_stats") or {}
    w = (webkit.mobile or {}).get("check_stats") or {}
    c_full = chromium.total if _is_full(chromium.auth) else None
    w_full = webkit.total if _is_full(webkit.auth) else None
    c_exe, w_exe = c.get("executed"), w.get("executed")
    comparable = min(c_exe, w_exe) if isinstance(c_exe, int) and isinstance(w_exe, int) else None
    rows = [
        ("Full regression suite", c_full, w_full),
        ("Mobile checks attempted", c.get("attempted"), w.get("attempted")),
        ("Mobile executed", c_exe, w_exe),
        ("Mobile structural N/A (engine-unsupported)", c.get("na_structural"), w.get("na_structural")),
        ("Comparable mobile (executed on both)", comparable, comparable),
    ]
    body = "".join(
        f"<tr><td>{esc(n)}</td><td class='num'>{_cov_cell(cv)}</td><td class='num'>{_cov_cell(wv)}</td></tr>"
        for n, cv, wv in rows
    )
    return (
        "<table class='cov'><thead><tr><th>Population</th><th class='num'>Chromium</th>"
        f"<th class='num'>WebKit</th></tr></thead><tbody>{body}</tbody></table>"
        "<p class='note'>Only the <strong>Comparable mobile</strong> row is an apples-to-apples "
        "basis. A dash means that population was not executed on that engine — not a pass.</p>"
    )


# Cap embedded screenshots so a shared export stays bounded (no Pillow to
# thumbnail, so we embed raw PNGs). Beyond the cap, cards stay text-only.
_MAX_EMBEDDED_SHOTS = 12


def _load_failure_evidence(folder_name: str | None, embed_shot: bool) -> dict:
    """Read the on-disk evidence for one failure so it can be INLINED into the
    report (self-contained for sharing): AI triage.json (diagnosis / fix /
    category / confidence), the page url.txt, and — up to the cap — screenshot.png
    embedded as a data-URI so it travels inside the exported file."""
    ev: dict = {}
    if not folder_name:
        return ev
    base = Path("reports/failures") / folder_name
    if not base.is_dir():
        return ev
    tj = base / "triage.json"
    if tj.is_file():
        try:
            t = json.loads(tj.read_text(encoding="utf-8"))
            ev.update(diagnosis=t.get("diagnosis"), suggested_fix=t.get("suggested_fix"),
                      triage_category=t.get("category"), confidence=t.get("confidence"))
        except (OSError, ValueError):
            pass
    u = base / "url.txt"
    if u.is_file():
        try:
            ev["url"] = u.read_text(encoding="utf-8").strip()[:400]
        except OSError:
            pass
    if embed_shot:
        sc = base / "screenshot.png"
        if sc.is_file():
            try:
                b = sc.read_bytes()
                if len(b) <= 400_000:
                    import base64
                    ev["screenshot"] = "data:image/png;base64," + base64.b64encode(b).decode()
            except OSError:
                pass
    return ev


def _render_combined_issues(engines: list[EngineData]) -> str:
    fails = [(ed, r) for ed in engines for r in ed.records if r.status == "FAIL"]
    if not fails:
        return "<p class='muted'>No failures across either engine 🎉</p>"
    # Most-actionable first: failures WITH a captured signature (real, triage-able)
    # ahead of unsigned ones, so a dev hits the useful cards immediately.
    fails.sort(key=lambda er: (0 if (er[1].error_signature or "").strip() else 1,
                               0 if er[1].artifact_folder else 1))
    cards, embedded = [], 0
    for ed, r in fails:
        commit = ((ed.sidecar or {}).get("git", {}) or {}).get("commit") or ""
        ev = _load_failure_evidence(r.artifact_folder, embed_shot=embedded < _MAX_EMBEDDED_SHOTS)
        if ev.get("screenshot"):
            embedded += 1
        cat = ev.get("triage_category") or ("⚠ harness" if r.harness_fault else "unclassified")
        sig = (r.error_signature or "").strip()
        summary = (
            f"{_prov(ed.engine, ed.run_label)} "
            f"<code class='mono'>{esc(r.clazz)}::{esc(r.method)}</code> "
            f"<span class='di-cat'>{esc(cat)}</span>"
        )
        body = []
        if sig:
            body.append(f"<div class='di-sig'><strong>Error:</strong> <code>{esc(sig[:280])}</code></div>")
        else:
            body.append("<div class='muted'>No error signature captured for this failure.</div>")
        if ev.get("diagnosis"):
            body.append(f"<div><strong>Diagnosis:</strong> {esc(str(ev['diagnosis']))}</div>")
        if ev.get("suggested_fix"):
            body.append(f"<div><strong>Suggested fix:</strong> {esc(str(ev['suggested_fix']))}</div>")
        meta = [f"feature: {esc(r.feature)}", f"engine: {esc(ed.engine)}"]
        if ev.get("url"):
            meta.append(f"page: <a href='{esc(ev['url'])}' target='_blank' rel='noopener'>{esc(ev['url'])}</a>")
        if commit:
            meta.append(f"commit: <code>{esc(commit[:10])}</code>")
        if ev.get("confidence") is not None:
            meta.append(f"triage confidence: {esc(str(ev['confidence']))}")
        body.append(f"<div class='di-meta'>{' · '.join(meta)}</div>")
        if ev.get("screenshot"):
            # Single embed (not duplicated in an <a href>) to keep the export small.
            body.append(f"<img class='di-shot' src='{ev['screenshot']}' alt='failure screenshot'>")
        elif r.artifact_folder:
            body.append("<div class='muted'>Screenshot available in the evidence folder "
                        f"(not embedded — over the {_MAX_EMBEDDED_SHOTS}-image cap for this export).</div>")
        cards.append(f"<details class='di-card'><summary>{summary}</summary>"
                     f"<div class='di-body'>{''.join(body)}</div></details>")
    intro = (
        f"<p class='note'>{len(fails)} failing test(s) — each expands to the <strong>error, AI diagnosis, "
        "suggested fix, page URL, git commit and an embedded screenshot</strong>, so a developer can act on it "
        f"straight from this report. {embedded} screenshot(s) embedded (cap {_MAX_EMBEDDED_SHOTS}) to keep the "
        "export self-contained yet bounded.</p>"
    )
    return intro + "".join(cards)


def _render_systemic(engines: list[EngineData]) -> str:
    """FAILURE CONCENTRATION — many failures in the same FEATURE. This is the
    WEAKEST of the three signals and does NOT claim a common cause: 27 failures
    in one feature could be 10 backend + 8 selector + 5 data + 4 product. For a
    proven common cause see the Systemic Failures section (signature-matched)."""
    blocks = []
    for ed in engines:
        by_feat: dict[str, int] = {}
        for r in ed.records:
            if r.status == "FAIL":
                by_feat[r.feature] = by_feat.get(r.feature, 0) + 1
        hot = sorted(((n, f) for f, n in by_feat.items() if n >= 3), reverse=True)
        if not hot:
            continue
        items = "".join(
            f"<li><strong>{n}</strong> failures — <span class='feat'>{esc(f)}</span></li>"
            for n, f in hot
        )
        blocks.append(f"<div class='sysblk'>{_prov(ed.engine, ed.run_label)}<ul>{items}</ul></div>")
    if not blocks:
        return "<p class='muted'>No feature has ≥3 failures this run.</p>"
    return (
        "<p class='note'><strong>Concentration ≠ common cause.</strong> This groups failures by "
        "FEATURE only — it does not prove they share a root cause. Signature-matched grouping is in "
        "the Systemic Failures section below.</p>" + "".join(blocks)
    )


def _render_systemic_failures(engines: list[EngineData]) -> str:
    """SYSTEMIC FAILURES — independent tests whose NORMALIZED failure signatures
    MATCH (a proven common cause), computed by failure_clustering.detect_systemic.
    Distinct from Failure Concentration: membership requires a matching real
    signature, never mere feature co-location."""
    from utils.failure_clustering import detect_systemic

    fail_records: list[dict] = []
    signed = 0
    for ed in engines:
        for r in ed.records:
            if r.status != "FAIL":
                continue
            sig = getattr(r, "error_signature", "") or ""
            if sig:
                signed += 1
            fail_records.append({
                "method": f"{r.clazz}::{r.method}", "feature": r.feature,
                "errorSignature": sig, "engine": ed.engine, "status": "FAIL",
            })

    if signed == 0:
        return (
            "<div class='pending'>⏳ <strong>Systemic detection pending.</strong> No failing test in "
            "the persisted runs carries a normalized error signature yet — signatures are captured from "
            "runs made after this feature landed. Re-run the suite so failures persist "
            "<code>errorSignature</code>, then this section groups independent tests by matching "
            "signature. (Until then, see Failure Concentration for the weaker feature-level view.)</div>"
        )

    systemic = detect_systemic(fail_records, min_tests=3)
    if not systemic:
        return (
            f"<p class='muted'>No systemic failure detected: {signed} failing test(s) carry signatures, "
            "but no signature is shared by ≥3 independent tests. (Feature concentration, a weaker signal, "
            "may still appear above.)</p>"
        )

    conf_color = {"High": "#b91c1c", "Medium": "#92400e", "Low": "#64748b"}
    cards = []
    for s in systemic:
        col = conf_color.get(s.confidence, "#64748b")
        methods = "".join(f"<li><code>{esc(m)}</code></li>" for m in s.test_methods[:30])
        if len(s.test_methods) > 30:
            methods += f"<li class='muted'>+{len(s.test_methods) - 30} more</li>"
        cards.append(
            "<div class='sysfail'>"
            f"<div class='sysfail-head'><span class='sysfail-sig'>“{esc(s.sample_message[:120])}”</span>"
            f"<span class='sysfail-conf' style='color:{col};border-color:{col}'>{esc(s.confidence)} confidence</span></div>"
            f"<div class='sysfail-stats'>"
            f"<span><strong>{s.affected_tests}</strong> independent tests</span>"
            f"<span><strong>{s.features}</strong> feature(s)</span>"
            f"<span>engines: {esc(', '.join(s.engines) or '—')}</span></div>"
            f"<details class='drill'><summary>affected tests</summary><ul>{methods}</ul></details>"
            "</div>"
        )
    return (
        "<p class='note'>Independent tests whose <strong>normalized failure signatures match</strong> — "
        "evidence of one common cause, not just shared features. Confidence scales with the number of "
        "independent tests sharing the signature.</p>" + "".join(cards)
    )


def _render_recurring(base: str) -> str:
    try:
        import utils.failure_clustering as fc
        try:
            clusters = fc.build_clusters(snapshot_dir=Path(base) / "snapshots", last_n=30)  # type: ignore[call-arg]
        except TypeError:
            fc.SNAPSHOT_DIR = Path(base) / "snapshots"
            clusters = fc.build_clusters(last_n=30)
    except Exception as e:  # pragma: no cover - defensive
        return f"<p class='muted'>Recurring clustering unavailable: {esc(str(e))}</p>"
    body = _db._render_recurring_failures_section(clusters)
    return body or "<p class='muted'>No recurring failures in the run history.</p>"


# Deterministic recommendation per mobile check — the "what to do" a dev needs,
# shown in a Recommended column throughout the mobile tables.
_RECS = {
    "tap_target": "Enlarge to ≥44×44px touch target",
    "font_size": "Increase text to ≥12px (inputs ≥16px to avoid iOS zoom)",
    "input_zoom": "Set input font-size ≥16px to stop iOS focus-zoom",
    "horizontal_overflow": "Constrain width to the viewport; remove horizontal scroll",
    "overflow": "Constrain width to the viewport; remove horizontal scroll",
    "fixed_width": "Use a responsive width instead of a fixed px width",
    "viewport_meta": "Add a proper responsive viewport meta tag",
    "above_fold": "Ensure the element renders above the fold on small screens",
    "cross_device": "Reconcile the layout difference across devices",
}


def _recommendation_for(category: str) -> str:
    return _RECS.get(str(category).lower(), "Review against mobile UX guidelines")


def _render_mobile_ai_triage(mobile: dict | None) -> str:
    """AI triage of the mobile findings, read from the persisted
    mobile-summary.ai_triage (advisory only — never affects severity/scoring).
    Its suggested_fix is the recommendation for each issue group."""
    t = (mobile or {}).get("ai_triage") or {}
    groups = t.get("groups") or []
    analysed, total = t.get("analysed_count"), t.get("total_count")
    if not groups:
        return ("<p class='muted'>AI triage unavailable for this run (no provider configured or analysis "
                "failed). Classifications are advisory only; scoring is deterministic either way.</p>")
    rows = "".join(
        f"<tr><td class='mono'>{esc(str(g.get('id','')))}</td>"
        f"<td><span class='di-cat'>{esc(str(g.get('classification','')))}</span></td>"
        f"<td class='num'>{esc(str(g.get('confidence','')))}</td>"
        f"<td>{esc(str(g.get('rationale','')))}</td>"
        f"<td class='rec'>{esc(str(g.get('suggested_fix','')))}</td>"
        f"<td>{esc(str(g.get('suggested_owner','')))}</td></tr>"
        for g in groups
    )
    head = f" — analysed {analysed} of {total} group(s)" if analysed is not None else ""
    summ = f"<p class='note'>{esc(str(t.get('summary','')))}</p>" if t.get("summary") else ""
    return (
        f"<p class='note'>AI triage (advisory — never moves severity or the score){head}. "
        "The <strong>Recommended fix</strong> column is the AI's proposed action per issue group.</p>"
        f"{summ}<table><thead><tr><th>Group</th><th>Classification</th><th class='num'>Conf.</th>"
        "<th>Rationale</th><th>Recommended fix</th><th>Owner</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _render_mobile_compact(ed: EngineData) -> str:
    """Light mobile view: authoritative score + coverage + severity counts +
    top issue patterns. Avoids the full 400-row raw findings table (which made
    the combined page ~1MB and unresponsive)."""
    if not ed.mobile:
        return "<p class='muted'>No mobile run persisted for this engine.</p>"
    findings = ed.mobile.get("findings") or []
    lay = ed.layered
    mob = (ed.auth or {}).get("mobile")
    q = getattr(lay, "mobile_quality", None) if lay else None
    cov = getattr(lay, "mobile_coverage", None) if lay else None
    cov_pct = f"{round(cov * 100)}%" if isinstance(cov, (int, float)) else "n/a"
    sev = {"blocker": 0, "major": 0, "minor": 0, "info": 0}
    for f in findings:
        s = str(f.get("severity", "")).lower()
        if s in sev:
            sev[s] += 1
    try:
        from utils.ai_mobile_triage import group_findings
        groups = group_findings([f for f in findings if str(f.get("severity", "")).lower() != "info"])
    except Exception:
        groups = []
    # Roll patterns UP to the RULE level (severity + check), so a check that
    # fires on 40 elements is ONE actionable 'fix once → clear many' row rather
    # than 40 near-identical tap_target rows.
    _sev_rank = {"blocker": 0, "major": 1, "minor": 2, "info": 3}
    rules: dict[tuple, dict] = {}
    for g in groups:
        key = (str(g.get("severity", "")).lower(), str(g.get("category", "")))
        r = rules.setdefault(key, {"occ": 0, "devices": set(), "pages": set(), "variants": 0})
        r["occ"] += int(g.get("count", 0) or 0)
        r["devices"].update(g.get("devices") or [])
        r["pages"].update(g.get("pages") or [])
        r["variants"] += 1
    rule_rows = sorted(rules.items(), key=lambda kv: (_sev_rank.get(kv[0][0], 9), -kv[1]["occ"]))
    rows = "".join(
        f"<tr><td>{_sev_chip(s)}</td><td class='mono'>{esc(cat)}</td>"
        f"<td class='num'>{d['occ']}</td><td class='num'>{len(d['devices'])}</td>"
        f"<td class='num'>{len(d['pages'])}</td><td class='num'>{d['variants']}</td>"
        f"<td class='rec'>{esc(_recommendation_for(cat))}</td></tr>"
        for (s, cat), d in rule_rows
    ) or "<tr><td colspan='7' class='muted'>no actionable rules</td></tr>"
    # Per-element drill-down — folds the per-engine dashboard's G-table into
    # the combined report so element-level detail lives here, not in a second
    # dashboard. Severity-ordered, capped to keep the export bounded.
    _sev = {"blocker": 0, "major": 1, "minor": 2, "info": 3}
    elem = "".join(
        f"<tr><td>{_sev_chip(f.get('severity',''))}</td>"
        f"<td class='mono'>{esc(str(f.get('category','')))}</td>"
        f"<td>{esc(str(f.get('message',''))[:150])}</td>"
        f"<td>{esc(str(f.get('device','')))}</td>"
        f"<td>{esc(str(f.get('page','')))}</td>"
        f"<td class='mono'>{esc(str(f.get('selector') or '—'))}</td>"
        f"<td class='rec'>{esc(_recommendation_for(f.get('category','')))}</td></tr>"
        for f in sorted(findings, key=lambda x: _sev.get(str(x.get('severity','')).lower(), 9))[:200]
    ) or "<tr><td colspan='7' class='muted'>none</td></tr>"
    _more = f"<p class='note'>Showing first 200 of {len(findings)}.</p>" if len(findings) > 200 else ""
    elem_drill = (
        f"<details class='drill'><summary>View all {len(findings)} per-element findings "
        "(element · device · page · selector · recommended)</summary>"
        "<table><thead><tr><th>Severity</th><th>Check</th><th>Element / issue</th>"
        "<th>Device</th><th>Page</th><th>Selector</th><th>Recommended</th></tr></thead>"
        f"<tbody>{elem}</tbody></table>{_more}</details>"
    )
    return (
        f"<div class='mob-strip'>"
        f"<div><span class='scv'>{mob if mob is not None else 'n/a'}</span><div class='sl'>Mobile score</div></div>"
        f"<div><span class='scv'>{q if q is not None else 'n/a'}</span><div class='sl'>Quality</div></div>"
        f"<div><span class='scv'>{cov_pct}</span><div class='sl'>Coverage</div></div>"
        f"<div><span class='scv'>{sev['blocker']}</span><div class='sl'>Blocker</div></div>"
        f"<div><span class='scv'>{sev['major']}</span><div class='sl'>Major</div></div>"
        f"<div><span class='scv'>{sev['minor']}</span><div class='sl'>Minor</div></div>"
        f"<div><span class='scv'>{sev['info']}</span><div class='sl'>Info</div></div>"
        f"<div><span class='scv'>{len(rules)}</span><div class='sl'>Rules</div></div>"
        f"</div>"
        f"<table><thead><tr><th>Severity</th><th>Rule / check</th>"
        f"<th class='num'>Occurrences</th><th class='num'>Devices</th>"
        f"<th class='num'>Pages</th><th class='num'>Distinct elements</th><th>Recommended</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"<p class='note'>Grouped by <strong>rule</strong> (fix once → clear many): "
        f"{len(rules)} rule(s) rolled up from {len(groups)} unique patterns / {len(findings)} raw findings. "
        "The <strong>Recommended</strong> column is the fix for each check.</p>"
        f"{elem_drill}"
        f"<details class='drill'><summary>🤖 AI Triage — "
        f"{len(((ed.mobile or {}).get('ai_triage') or {}).get('groups') or [])} group(s), "
        f"click to expand</summary>{_render_mobile_ai_triage(ed.mobile)}</details>"
        f"<p class='note'>Mobile findings are <strong>engine-specific</strong> — counts differ between "
        f"engines because of executable checks, structural N/A (engine can't run them), and engine-specific "
        f"DOM/layout behaviour. Compare mobile across engines only via the Engine Comparison coverage matrix.</p>"
    )


def _render_js_clusters(clusters: list[dict]) -> str:
    """Render JS error clusters persisted in the sidecar. An empty list means
    the run genuinely observed no JS errors (a result), not missing data."""
    if not clusters:
        return "<p class='muted'>No JS error clusters recorded this run.</p>"
    rows = "".join(
        "<tr>"
        f"<td>{esc(str(c.get('domain', c.get('severity', '—'))))}</td>"
        f"<td>{esc(str(c.get('title', c.get('message', '')))[:120])}</td>"
        f"<td class='num'>{esc(str(c.get('count', '')))}</td>"
        "</tr>"
        for c in clusters[:50]
    )
    return (
        "<table><thead><tr><th>Domain / severity</th><th>Cluster</th>"
        f"<th class='num'>Count</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def _stat_tiles(records: list) -> str:
    """The old dashboard's Total/Passed/Failed/Skipped/Pass-rate tiles —
    computed by COUNTING records (not a score recompute), so always real."""
    total = len(records)
    passed = sum(1 for r in records if r.status == "PASS")
    failed = sum(1 for r in records if r.status == "FAIL")
    skipped = sum(1 for r in records if r.status == "SKIP")
    rate = f"{round(passed / total * 100, 1)}%" if total else "—"
    tiles = [
        ("Total", total, "#eef2ff", "#4338ca"), ("Passed", passed, "#dcfce7", "#15803d"),
        ("Failed", failed, "#fee2e2", "#b91c1c"), ("Skipped", skipped, "#f1f5f9", "#64748b"),
        ("Pass rate", rate, "#dbeafe", "#0369a1"),
    ]
    cells = "".join(
        f"<div class='tile' style='background:{bg}'><div class='tile-v' style='color:{c}'>{v}</div>"
        f"<div class='tile-l'>{esc(lbl)}</div></div>"
        for lbl, v, bg, c in tiles
    )
    return f"<div class='tiles'>{cells}</div>"


def _hcard(label: str, val) -> str:
    if not isinstance(val, int):
        return (f"<div class='hcard hcard-pending'><div class='hcard-v'>⏳</div>"
                f"<div class='hcard-l'>{esc(label)}</div><div class='hcard-b'>pending sidecar</div></div>")
    bl, txt, bg = score_band(val)
    return (f"<div class='hcard' style='background:{bg}'><div class='hcard-v' style='color:{txt}'>{val}</div>"
            f"<div class='hcard-l'>{esc(label)}</div><div class='hcard-b' style='color:{txt}'>{esc(bl)}</div></div>")


def _health_cards(ed: EngineData) -> str:
    """The old dashboard's big Product/Infra/Framework/Mobile health cards.
    Domains come from the authoritative sidecar (never recomputed); Overall &
    Mobile from the run's trend row."""
    auth = ed.auth or {}
    hs = (ed.sidecar or {}).get("health") or {}
    overall = auth.get("overall")
    ov_band = score_band(overall)[0] if isinstance(overall, int) else "—"
    cards = (_hcard("Product Health", hs.get("product")) + _hcard("Infra Health", hs.get("infrastructure"))
             + _hcard("Framework Health", hs.get("framework")) + _hcard("Mobile", auth.get("mobile")))
    sc_pop = (ed.sidecar or {}).get("population") or {}
    note = ""
    if sc_pop.get("total") and sc_pop["total"] != len(ed.records):
        note = (f"<p class='note'>⚠ Product / Infra / Framework are from a "
                f"<strong>{esc(str(sc_pop.get('suite', '?')))}</strong> run ({sc_pop['total']} tests); the "
                f"tiles above are the latest {len(ed.records)}-test run. A fresh full run refreshes both together.</p>")
    return (
        f"<div class='hoverall'><span class='hoverall-v'>{overall if overall is not None else 'n/a'}</span>"
        f"<span class='hoverall-l'>Overall · {esc(ov_band)}</span></div>"
        f"<div class='hcards'>{cards}</div>{note}"
    )


def _render_engine_block(ed: EngineData) -> str:
    if not ed.present:
        return f"<p class='muted'>No persisted run for {esc(ed.engine)}.</p>"
    if ed.sidecar is not None and "js_error_clusters" in ed.sidecar:
        js = _render_js_clusters(ed.sidecar.get("js_error_clusters") or [])
    else:
        js = _pending("JS Error Clusters", "utils/health_tracker.get_clusters()")
    return (
        f"<h3 class='blk'>Health Score</h3>{_health_cards(ed)}"
        f"<h3 class='blk'>Test Results</h3>{_stat_tiles(ed.records)}"
        f"<h3 class='blk'>Mobile</h3>{_render_mobile_compact(ed)}"
        f"<h3 class='blk'>JS Errors</h3>{js}"
    )


def _render_login_routes(engines: list[EngineData]) -> str:
    """Cross-engine login-route observations, read from each engine's sidecar.
    Pending only if NO engine has persisted a sidecar with the routes. Engines
    without a sidecar get an explicit per-engine pending note so an empty table
    is never misread as 'this engine hit no login routes'."""
    def _has(ed: EngineData) -> bool:
        return ed.sidecar is not None and "login_routes" in ed.sidecar
    have_any = any(_has(ed) for ed in engines)
    if not have_any:
        return _pending("Login Route Observations", "utils/health_tracker.get_login_routes()")
    pending_engines = [ed.engine for ed in engines if not _has(ed)]
    pending_note = (
        f"<p class='note'>⏳ Login routes for <strong>{esc(', '.join(pending_engines))}</strong> "
        f"are pending — that engine has not persisted a report-data.json sidecar yet, so this table "
        f"is not the full cross-engine picture.</p>" if pending_engines else ""
    )
    rows = []
    for ed in engines:
        for r in (ed.sidecar or {}).get("login_routes") or []:
            route = str(r.get("route", ""))
            badge = ("LEGACY accounts.appypie.com" if route == "legacy-appypie"
                     else "FLOZIC authv2" if route == "flozic-authv2" else route or "UNKNOWN")
            rows.append(
                "<tr>"
                f"<td>{_prov(ed.engine, ed.run_label)}</td>"
                f"<td class='mono'>{esc(str(r.get('test', '')))}</td>"
                f"<td>{esc(badge)}</td>"
                f"<td class='mono urlcell' title='{esc(str(r.get('url', '')))}'>{esc(_truncate_url(str(r.get('url', ''))))}</td>"
                "</tr>"
            )
    table = (
        "<p class='muted'>No login-route observations in the persisted engine(s).</p>"
        if not rows else
        "<table><thead><tr><th>Engine</th><th>Test</th><th>Route</th><th>URL</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    return table + pending_note


# ─────────────────────────────────────────────────────────────────────────────
# Assembly
# ─────────────────────────────────────────────────────────────────────────────

_CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f1f5f9;color:#1e293b;line-height:1.45}
.container{max-width:1180px;margin:0 auto;padding:24px}
.report-title{font-size:22px;font-weight:800;margin-bottom:2px}
.report-sub{color:#64748b;font-size:13px;margin-bottom:20px}
.fg-section{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:20px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.05);overflow-x:auto}
td{word-break:break-word}
td.urlcell{font-size:11px;word-break:break-all;max-width:420px}
.di-meta a{word-break:break-all}
.fg-sec-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;border-bottom:1px solid #eef2f7;padding-bottom:10px}
h2{font-size:15px;font-weight:800;color:#0f172a;text-transform:uppercase;letter-spacing:.6px}
h3.blk{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:#64748b;margin:16px 0 8px}
h4.blk{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:#7c3aed;margin:14px 0 6px}
td.rec{color:#166534;background:#f0fdf4;font-size:12px;font-weight:600}
th:last-child{white-space:nowrap}
.fg-export-btn{border:1px solid #cbd5e1;background:#f8fafc;color:#334155;font-size:12px;font-weight:600;padding:6px 12px;border-radius:8px;cursor:pointer;white-space:nowrap}
.fg-export-btn:hover{background:#eef2f7}
.prov{display:inline-block;border:1px solid;border-radius:999px;padding:1px 8px;font-size:10px;font-weight:700;vertical-align:middle;background:#fff;white-space:nowrap}
.sev-chip{display:inline-block;border-radius:999px;padding:1px 8px;font-size:10px;font-weight:800;letter-spacing:.4px;white-space:nowrap}
table.ovr{margin:12px 0}
table.ovr th{white-space:nowrap}
table.ovr td:first-child{font-weight:600;color:#475569}
tr.ovr-grp td{background:#f1f5f9;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.8px;color:#334155;padding:5px 10px}
.note{color:#64748b;font-size:12px;margin-bottom:10px}
.muted{color:#94a3b8;font-style:italic}
.mono,.feat{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.feat{color:#64748b}
.pending{background:#fffbeb;border:1px dashed #f59e0b;color:#92400e;border-radius:10px;padding:12px;font-size:13px}
table{width:100%;border-collapse:collapse;border:1px solid #cbd5e1;margin-top:6px}
th,td{border:1px solid #e2e8f0;padding:7px 10px;font-size:12px;text-align:left;vertical-align:top}
th{background:#f8fafc;font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:#475569}
table.exec td.sc{text-align:center;width:12%}
.scv{font-size:24px;font-weight:800}.sl{font-size:10px;text-transform:uppercase;color:#64748b}.sb{font-size:10px;font-weight:700}
.na{font-size:20px;color:#cbd5e1}
td.eng{white-space:nowrap}
.cmp-scores{display:flex;align-items:center;gap:18px;margin-bottom:10px}
.cmp-cell{flex:1;text-align:center;background:#f8fafc;border-radius:10px;padding:12px}
.cmp-mob{font-size:26px;font-weight:800;margin-top:6px}
.cmp-vs{color:#94a3b8;font-weight:700}
.cmp-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:10px}
.cmp-box{border:1px solid #e2e8f0;border-radius:10px;padding:12px}
.cmp-box.shared{grid-column:1/-1;background:#fef2f2;border-color:#fecaca}
.cmp-box.notrun{background:#f8fafc}
.cmp-box h3{font-size:12px;margin-bottom:6px}.cmp-box small{color:#94a3b8;font-weight:600}
.cmp-box ul{list-style:none;max-height:220px;overflow:auto}.cmp-box li{padding:2px 0;font-size:12px;border-bottom:1px solid #f1f5f9}
.sysblk{margin-bottom:10px}.sysblk ul{list-style:none;margin-top:6px}.sysblk li{padding:2px 0}
.sysfail{border:1px solid #fecaca;background:#fef2f2;border-radius:10px;padding:12px;margin-bottom:10px}
.sysfail-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.sysfail-sig{font-family:ui-monospace,Menlo,monospace;font-size:13px;font-weight:700;color:#7f1d1d}
.sysfail-conf{font-size:11px;font-weight:800;border:1px solid;border-radius:999px;padding:1px 8px}
.sysfail-stats{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:#64748b;margin:6px 0}
.sysfail details{margin-top:4px}.sysfail ul{list-style:none;max-height:180px;overflow:auto}.sysfail li{padding:1px 0;font-size:12px}
.rel{display:inline-block;margin-top:4px;font-size:10px;font-weight:800;padding:1px 8px;border-radius:6px}
.rel-blocked{background:#fee2e2;color:#b91c1c}.rel-ready{background:#dcfce7;color:#15803d}
.rel-warning,.rel-at_risk{background:#fef3c7;color:#92400e}.rel-inconclusive{background:#e0e7ff;color:#3730a3}
.mob-strip{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px}
.mob-strip>div{text-align:center;min-width:66px}
.blk-health{background:#f8fafc;border-radius:10px;padding:12px 16px;display:inline-block;margin-bottom:8px}
.tiles{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:8px}
.tile{border-radius:12px;padding:16px;text-align:center}
.tile-v{font-size:30px;font-weight:800;line-height:1}
.tile-l{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#64748b;margin-top:4px}
.hoverall{display:flex;align-items:baseline;gap:10px;margin-bottom:10px}
.hoverall-v{font-size:34px;font-weight:800;color:#0f172a}
.hoverall-l{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#64748b}
.hcards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.hcard{border:1px solid #e2e8f0;border-radius:12px;padding:16px;text-align:center}
.hcard-pending{background:#fffbeb;border-color:#fde68a}
.hcard-v{font-size:34px;font-weight:800;line-height:1}
.hcard-l{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#475569;margin-top:4px}
.hcard-b{font-size:10px;font-weight:700;margin-top:2px}
@media(max-width:720px){.tiles{grid-template-columns:repeat(2,1fr)}.hcards{grid-template-columns:repeat(2,1fr)}}
.rel-banner{border-radius:12px;padding:14px 18px;font-size:20px;font-weight:800;margin-bottom:16px;display:flex;flex-direction:column;gap:2px}
.rel-banner .rel-why{font-size:11px;font-weight:600;opacity:.85;text-transform:none;letter-spacing:0}
.rel-banner.rel-blocked{background:#fee2e2;color:#991b1b;border:1px solid #fecaca}
.rel-banner.rel-ready{background:#dcfce7;color:#166534;border:1px solid #bbf7d0}
.rel-banner.rel-warning,.rel-banner.rel-at_risk,.rel-banner.rel-inconclusive{background:#fef3c7;color:#92400e;border:1px solid #fde68a}
.eng-cards{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.eng-card{border:1px solid #e2e8f0;border-radius:12px;padding:16px;text-align:center;background:#fafcff}
.eng-name{font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.6px;color:#475569}
.eng-score{font-size:44px;font-weight:800;line-height:1;margin-top:4px}
.eng-band{font-size:11px;font-weight:700;color:#64748b;margin-bottom:6px}
.eng-pop{font-size:11px;color:#64748b;margin-top:6px}
.prov-row{margin-top:8px}
.rel-scope{background:#e0e7ff;color:#3730a3}
.basis{border-radius:10px;padding:8px 12px;font-size:12px;margin:8px 0;background:#eef2ff;border:1px solid #c7d2fe;color:#3730a3}
.elig{border-radius:10px;padding:10px 12px;font-size:12px;margin:12px 0}
.elig-partial{background:#fffbeb;border:1px solid #fde68a;color:#92400e}
.elig-full{background:#dcfce7;border:1px solid #bbf7d0;color:#166534}
.cmp-summary{display:flex;gap:12px;flex-wrap:wrap;margin:10px 0}
.cmp-summary>div{flex:1;min-width:110px;text-align:center;background:#f8fafc;border-radius:10px;padding:10px}
th.num,td.num{text-align:right}
details.drill summary{cursor:pointer;font-weight:700;font-size:13px;color:#334155;padding:8px 0}
details.drill[open] summary{margin-bottom:6px}
.di-card{border:1px solid #e2e8f0;border-radius:10px;margin-bottom:8px;background:#fff;overflow:hidden}
.di-card>summary{cursor:pointer;padding:10px 12px;font-size:12px;list-style:none;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.di-card>summary::-webkit-details-marker{display:none}
.di-card[open]>summary{border-bottom:1px solid #eef2f7;background:#f8fafc}
.di-cat{margin-left:auto;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.4px;color:#7c3aed;border:1px solid #ddd6fe;border-radius:999px;padding:1px 8px}
.di-body{padding:12px;font-size:13px;line-height:1.5;color:#1e293b}
.di-body>div{margin-bottom:6px}
.di-sig code,.di-body code{background:#f1f5f9;border-radius:4px;padding:1px 5px;font-size:12px}
.di-meta{color:#64748b;font-size:12px;border-top:1px dashed #e2e8f0;padding-top:6px}
.di-shot{max-width:100%;max-height:420px;border:1px solid #cbd5e1;border-radius:8px;margin-top:8px;display:block}
@media print{
  body{background:#fff}
  .container{max-width:none;padding:0}
  .fg-export-btn,.report-sub{display:none}
  .fg-print-hide{display:none !important}
  .fg-section{border:none;box-shadow:none;break-inside:avoid;page-break-inside:avoid}
}
"""

_JS = """
function exportSection(id){
  var secs=document.querySelectorAll('.fg-section');
  var target=document.getElementById(id);
  var reopen=[];
  if(target){ target.querySelectorAll('details:not([open])').forEach(function(d){ d.open=true; reopen.push(d); }); }
  secs.forEach(function(s){ if(s.id!==id) s.classList.add('fg-print-hide'); });
  window.print();
  setTimeout(function(){
    secs.forEach(function(s){ s.classList.remove('fg-print-hide'); });
    reopen.forEach(function(d){ d.open=false; });
  },600);
}
"""


def build(base: str = "reports/trend", out: Path | str = OUT_PATH) -> Path:
    out = Path(out)
    engines = [load_engine(e, base) for e in _ENGINES]
    by_name = {e.engine: e for e in engines}
    chromium, webkit = by_name["chromium"], by_name["webkit"]

    generated = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Locked structure: Executive Summary → Cross-Engine Analysis (genuinely
    # comparable/aggregate data) → Engine Comparison (with coverage matrix, so
    # the reader understands eligibility BEFORE the per-engine detail) →
    # Chromium → WebKit → Detailed Issues (drill-down).
    sections = "".join([
        _section("sec-exec", "Executive Summary", _render_exec_summary(engines)),
        _section("sec-systemic", "Cross-Engine · Failure Concentration", _render_systemic(engines)),
        _section("sec-systemic-failures", "Cross-Engine · Systemic Failures", _render_systemic_failures(engines)),
        _section("sec-recurring", "Cross-Engine · Failure History",
                 _render_recurring(base), _prov("chromium", chromium.run_label, "run history")),
        _section("sec-login", "Cross-Engine · 🔐 Login Route Observations",
                 _render_login_routes(engines)),
        _section("sec-compare", "Engine Comparison", _render_engine_comparison(chromium, webkit)),
        _section("sec-chromium", "Chromium", _render_engine_block(chromium),
                 _prov("chromium", chromium.run_label)),
        _section("sec-webkit", "WebKit", _render_engine_block(webkit),
                 _prov("webkit", webkit.run_label)),
        _section("sec-issues", "Detailed Issues", _render_combined_issues(engines)),
    ])

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flozic FlowGuard — Combined Cross-Engine Report</title>
<style>{_CSS}</style></head>
<body><div class="container">
  <div class="report-title">Flozic FlowGuard — Combined Cross-Engine Report</div>
  <div class="report-sub">Hybrid layout · generated {generated} · each metric carries its own engine + run provenance</div>
  {sections}
</div>
<script>{_JS}</script>
</body></html>"""

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    # Also write index.html so the combined report is the DEFAULT landing page:
    # opening reports/trend/ (or the server root) now lands on the cross-engine
    # view rather than a per-engine dashboard.
    try:
        (out.parent / "index.html").write_text(html, encoding="utf-8")
    except Exception:
        pass
    return out


if __name__ == "__main__":
    p = build()
    print(f"wrote {p}")
