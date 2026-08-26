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


def _render_exec_summary(engines: list[EngineData]) -> str:
    overall = _overall_release(engines)
    cards = []
    for ed in engines:
        auth = ed.auth
        if not auth:
            cards.append(
                f"<div class='eng-card'><div class='eng-name'>{esc(ed.engine)}</div>"
                f"<div class='muted'>no completed run in trend history</div></div>"
            )
            continue
        total = auth.get("total") or 0
        suite = "Full suite" if _is_full(auth) else "Mobile-only (partial scope)"
        ov = auth.get("overall")
        band = score_band(ov)[0] if isinstance(ov, int) else "—"
        label, cls = _engine_status_label(auth)
        cards.append(
            "<div class='eng-card'>"
            f"<div class='eng-name'>{esc(ed.engine)}</div>"
            f"<div class='eng-score'>{ov if ov is not None else 'n/a'}</div>"
            f"<div class='eng-band'>{esc(band)}</div>"
            f"<div class='rel rel-{cls}'>{esc(label)}</div>"
            f"<div class='eng-pop'>{esc(suite)} · {total} tests · {auth.get('failed', 0)} failed · Mobile {auth.get('mobile', 'n/a')}</div>"
            f"<div class='prov-row'>{_prov(ed.engine, ed.run_label)}</div>"
            "</div>"
        )
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
        f"<div class='eng-cards'>{''.join(cards)}</div>"
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


def _render_combined_issues(engines: list[EngineData]) -> str:
    rows = []
    for ed in engines:
        for r in ed.records:
            if r.status != "FAIL":
                continue
            rows.append(
                "<tr>"
                f"<td>{_prov(ed.engine, ed.run_label)}</td>"
                f"<td class='mono'>{esc(r.clazz)}::{esc(r.method)}</td>"
                f"<td>{esc(r.feature)}</td>"
                f"<td>{esc(r.category)}</td>"
                f"<td>{'⚠ harness' if r.harness_fault else 'product/test'}</td>"
                "</tr>"
            )
    if not rows:
        return "<p class='muted'>No failures across either engine 🎉</p>"
    return (
        f"<p class='note'>{len(rows)} failing test(s), each tagged with the engine that observed it. "
        "The answer to <em>what is wrong</em> is in the sections above; expand for the row-level detail.</p>"
        f"<details class='drill'><summary>View all {len(rows)} failing tests</summary>"
        "<table><thead><tr><th>Engine</th><th>Test</th><th>Feature</th><th>Category</th>"
        "<th>Fault layer</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></details>"
    )


def _render_systemic(engines: list[EngineData]) -> str:
    """v1 systemic proxy: failures concentrated in one feature within a run.
    (Exception-signature grouping needs exception text, which v3 snapshots do
    not persist — flagged so the label is honest.)"""
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
        return "<p class='muted'>No feature shows a failure cluster (≥3) this run.</p>"
    return (
        "<p class='note'>Failure concentration by feature (a suspected common cause). "
        "v1 groups by feature; exception-signature grouping arrives once snapshots persist "
        "the normalized error text.</p>" + "".join(blocks)
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
        f"<tr><td>{esc(s)}</td><td class='mono'>{esc(cat)}</td>"
        f"<td class='num'>{d['occ']}</td><td class='num'>{len(d['devices'])}</td>"
        f"<td class='num'>{len(d['pages'])}</td><td class='num'>{d['variants']}</td></tr>"
        for (s, cat), d in rule_rows
    ) or "<tr><td colspan='6' class='muted'>no actionable rules</td></tr>"
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
        f"<th class='num'>Pages</th><th class='num'>Distinct elements</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"<p class='note'>Grouped by <strong>rule</strong> (fix once → clear many): "
        f"{len(rules)} rule(s) rolled up from {len(groups)} unique patterns / {len(findings)} raw findings. "
        f"Per-element detail lives in the per-engine dashboard.</p>"
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


def _render_engine_block(ed: EngineData) -> str:
    if not ed.present:
        return f"<p class='muted'>No persisted run for {esc(ed.engine)}.</p>"
    auth = ed.auth or {}
    # Domain scores come ONLY from the sidecar's authoritative `health` block,
    # NEVER recomputed here. Absent → ⏳ pending.
    health_sc = (ed.sidecar or {}).get("health") or {}
    overall = auth.get("overall")
    band = f" <span class='prov'>{esc(score_band(overall)[0])}</span>" if isinstance(overall, int) else ""

    def _dom(key: str, label: str) -> str:
        v = health_sc.get(key)
        return f"{label} {v}" if isinstance(v, int) else f"{label} ⏳"

    if {"product", "infrastructure", "framework"} & health_sc.keys():
        domains = f"{_dom('product','Product')} · {_dom('infrastructure','Infra')} · {_dom('framework','Framework')}"
    else:
        domains = "<span class='muted'>Product / Infra / Framework: ⏳ pending sidecar (run with persistence)</span>"
    health = (
        f"<div class='blk-health'><span class='scv'>{overall if overall is not None else 'n/a'}</span>"
        f"<div class='sl'>Overall{band}</div><div class='note'>{domains}</div></div>"
    )

    # JS errors: from the sidecar when present (empty list = a real 'no errors'
    # result); pending only when the sidecar itself is absent.
    if ed.sidecar is not None and "js_error_clusters" in ed.sidecar:
        js = _render_js_clusters(ed.sidecar.get("js_error_clusters") or [])
    else:
        js = _pending("JS Error Clusters", "utils/health_tracker.get_clusters()")
    return f"{health}<h3 class='blk'>Mobile</h3>{_render_mobile_compact(ed)}<h3 class='blk'>JS Errors</h3>{js}"


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
                f"<td class='mono' style='max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>{esc(str(r.get('url', '')))}</td>"
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
.fg-section{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:20px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.fg-sec-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;border-bottom:1px solid #eef2f7;padding-bottom:10px}
h2{font-size:15px;font-weight:800;color:#0f172a;text-transform:uppercase;letter-spacing:.6px}
h3.blk{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:#64748b;margin:16px 0 8px}
.fg-export-btn{border:1px solid #cbd5e1;background:#f8fafc;color:#334155;font-size:12px;font-weight:600;padding:6px 12px;border-radius:8px;cursor:pointer;white-space:nowrap}
.fg-export-btn:hover{background:#eef2f7}
.prov{display:inline-block;border:1px solid;border-radius:999px;padding:1px 8px;font-size:10px;font-weight:700;vertical-align:middle;background:#fff}
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
.rel{display:inline-block;margin-top:4px;font-size:10px;font-weight:800;padding:1px 8px;border-radius:6px}
.rel-blocked{background:#fee2e2;color:#b91c1c}.rel-ready{background:#dcfce7;color:#15803d}
.rel-warning,.rel-at_risk{background:#fef3c7;color:#92400e}.rel-inconclusive{background:#e0e7ff;color:#3730a3}
.mob-strip{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px}
.mob-strip>div{text-align:center;min-width:66px}
.blk-health{background:#f8fafc;border-radius:10px;padding:12px 16px;display:inline-block;margin-bottom:8px}
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
