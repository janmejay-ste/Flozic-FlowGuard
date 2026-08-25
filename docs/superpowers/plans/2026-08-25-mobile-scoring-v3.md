# Mobile Scoring v3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the saturating flat mobile penalty with a coverage-adjusted quality score that discriminates light from heavy runs and never penalises an engine for checks it structurally cannot run.

**Architecture:** `Mobile = round(Quality × coverage)`, gated to `None` when coverage is insufficient. `Quality` is a `severity × device-reach` weighted exponential curve over unique issue *patterns* (from the existing `group_findings`), with worst-severity ceilings. `coverage = executed / executable` where `executable = attempted − na_structural`; structural (engine-impossible) checks are excluded from the denominator and reported as N/A. The change is isolated to the mobile band of `utils/layered_health_scores.py`, a 3-way refinement of the coverage tally, one call site, and two display helpers.

**Tech Stack:** Python 3.11, pytest. No new dependencies.

## Global Constraints

- Python 3.11; `.venv/bin/python -m pytest` is the runner. NO new dependencies.
- Invariant: **AI never affects scoring.** The scorer must not read AI-triage output. (No confidence multiplier.)
- `reports/` is gitignored runtime state — never commit it; read committed sidecars only as test fixtures via literal paths.
- `SCORING_VERSION` bumps to `3`. A run with no mobile data must score identically under v2 and v3 (weights renormalise to `50/30/20`).
- `K=120` and the `55/20` severity ceilings ship **provisional** (no major/blocker mobile data exists to calibrate them); mark them so in code comments.
- Commit message trailer, every commit: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Real-data acceptance: Chromium Mobile 62, WebKit Mobile 59 (both `coverage` 1.0 this run).

---

## File Structure

- `utils/mobile_report_builder.py` — coverage tally: `_SESSION_CHECKS`, `note_check_run`, `session_check_stats`. **Modify** to record `na_structural`.
- `utils/mobile_interaction_auditor.py` — `_tallied` decorator. **Modify** to classify structural vs crash.
- `utils/layered_health_scores.py` — the scorer. **Modify** the mobile band + constants + `LayeredScores` + `compute()` signature.
- `conftest.py` — session teardown. **Modify** the `compute_scores(...)` call to pass check stats.
- `utils/dashboard_builder.py` (`_mobile_cell`) and `utils/pdf_report_builder.py` (`_mobile_score_box`) — **Modify** to show the `Quality × coverage` breakdown and the "insufficient coverage" state.
- `tests/unit/test_mobile_scoring.py` — **Modify** (append) all new cases.

---

### Task 1: 3-way coverage tally (structural vs crash vs executed)

**Files:**
- Modify: `utils/mobile_report_builder.py` (`_SESSION_CHECKS` init, `note_check_run`)
- Modify: `utils/mobile_interaction_auditor.py` (`_tallied`, lines ~55–87)
- Test: `tests/unit/test_mobile_scoring.py`

**Interfaces:**
- Produces: `note_check_run(engine: str, executed: bool, structural: bool = False) -> None`
- Produces: `session_check_stats() -> dict[str, dict]` where each value is `{"attempted": int, "executed": int, "na_structural": int}`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_mobile_scoring.py`:

```python
def test_check_tally_records_structural_separately():
    import utils.mobile_report_builder as mrb
    mrb.reset_session_findings()
    mrb.note_check_run("webkit", executed=True)                     # ran clean
    mrb.note_check_run("webkit", executed=False, structural=True)   # engine can't (UNTESTED)
    mrb.note_check_run("webkit", executed=False, structural=False)  # crashed (NOT verified)
    assert mrb.session_check_stats()["webkit"] == {
        "attempted": 3, "executed": 1, "na_structural": 1,
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_mobile_scoring.py::test_check_tally_records_structural_separately -q`
Expected: FAIL (`KeyError: 'na_structural'` or signature mismatch).

- [ ] **Step 3: Implement the tally change**

In `utils/mobile_report_builder.py`, replace `note_check_run` and its `_SESSION_CHECKS` entry shape:

```python
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
```

- [ ] **Step 4: Update `_tallied` to classify structural vs crash**

In `utils/mobile_interaction_auditor.py`, replace the `finally` block of `_tallied` (the `executed = not any(...)` computation and its `note_check_run` call, ~lines 80–86):

```python
        finally:
            new = self.findings[before:]
            untested = any("UNTESTED" in f.message or "UNSUPPORTED" in f.message
                           for f in new)
            crashed = any("NOT verified" in f.message for f in new)
            if crashed:                       # crash is non-structural, always counts
                note_check_run(self.engine, executed=False, structural=False)
            elif untested:                    # engine capability gap -> N/A
                note_check_run(self.engine, executed=False, structural=True)
            else:
                note_check_run(self.engine, executed=True)
```

- [ ] **Step 5: Run the tally test + full unit suite**

Run: `.venv/bin/python -m pytest tests/unit -q -p no:cacheprovider`
Expected: PASS (the new test green; all others still green — the autouse isolation fixture already save/restores `_SESSION_CHECKS`).

- [ ] **Step 6: Commit**

```bash
git add utils/mobile_report_builder.py utils/mobile_interaction_auditor.py tests/unit/test_mobile_scoring.py
git commit -m "Split mobile coverage tally into structural / crash / executed

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Quality curve + severity ceilings

**Files:**
- Modify: `utils/layered_health_scores.py` (constants near line 44–48; add `_mobile_quality` helper)
- Test: `tests/unit/test_mobile_scoring.py`

**Interfaces:**
- Consumes: `utils.ai_mobile_triage.group_findings(findings) -> list[dict]` (each group: `severity`, `category`, `devices`, `count`).
- Produces: `_mobile_quality(findings: list[dict]) -> tuple[int, dict[str, int]]` — returns `(quality_0_100, {"blocker": n, "major": n, "minor": n})`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_mobile_scoring.py`:

```python
def _mk(sev, cat, device, elem):
    return {"severity": sev, "category": cat, "device": device, "page": "homepage",
            "message": f"A '{elem}' is 80x30px on {device} — below the 44px recommended minimum."}

def test_quality_discriminates_light_from_heavy():
    from utils.layered_health_scores import _mobile_quality
    devices = ["iPhone SE","iPhone 13","iPhone 14 Pro Max","Pixel 5","Galaxy S9+","iPad Mini"]
    light = [_mk("minor","tap_target","iPhone SE",f"E{i}") for i in range(20)]      # 20 single-device patterns
    heavy = [_mk("minor","tap_target",d,f"E{i}") for i in range(20) for d in devices] + \
            [_mk("minor","tap_target",d,f"F{i}") for i in range(30) for d in devices]
    q_light, _ = _mobile_quality(light)
    q_heavy, _ = _mobile_quality(heavy)
    assert q_light > q_heavy
    assert q_light >= 85           # a light run scores well
    assert 40 <= q_heavy <= 75     # a heavy run is mediocre, not floored

def test_quality_ceiling_major_and_blocker():
    from utils.layered_health_scores import _mobile_quality
    one_major = [_mk("major","overflow","iPhone SE","X") ]
    q_major, _ = _mobile_quality(one_major)
    assert q_major <= 55           # a lone major cannot look healthy
    one_blocker = [_mk("blocker","scroll","iPhone SE","Y")]
    q_blocker, _ = _mobile_quality(one_blocker)
    assert q_blocker <= 20

def test_quality_empty_is_100():
    from utils.layered_health_scores import _mobile_quality
    q, counts = _mobile_quality([])
    assert q == 100 and counts == {"blocker": 0, "major": 0, "minor": 0}
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_mobile_scoring.py -k quality -q`
Expected: FAIL (`cannot import name '_mobile_quality'`).

- [ ] **Step 3: Add constants and the helper**

In `utils/layered_health_scores.py`: set `SCORING_VERSION = 3`, and REPLACE the two lines
`_MOBILE_PENALTY = {...}` / `_MOBILE_PENALTY_CAP = 60` with:

```python
import math

# v3 mobile model. PROVISIONAL constants — see the calibration plan in
# docs/superpowers/specs/2026-08-25-mobile-scoring-v3-design.md. K and the
# ceilings have no major/blocker mobile data to fit against yet.
_MOBILE_SEV_WEIGHT = {"minor": 1.0, "major": 5.0, "blocker": 12.0}
_MOBILE_K = 120.0                    # Quality = 50 at Σ ≈ 83 weighted patterns
_MOBILE_CEILING_BLOCKER = 20
_MOBILE_CEILING_MAJOR = 55
_MOBILE_COVERAGE_GATE = 0.33


def _mobile_quality(findings):
    """Quality 0–100 from unique issue patterns: severity × device-reach on a
    saturating curve, capped by the worst severity present."""
    from utils.ai_mobile_triage import group_findings
    counts = {"blocker": 0, "major": 0, "minor": 0}
    for f in findings:
        s = (f.get("severity") or "").lower()
        if s in counts:
            counts[s] += 1
    groups = group_findings(findings)
    if not groups:
        return 100, counts
    total_dev = len({f.get("device") for f in findings if f.get("device")}) or 1

    def reach(n):
        return 0.5 + (n - 1) / max(total_dev - 1, 1)

    sigma = sum(_MOBILE_SEV_WEIGHT[g["severity"]] * reach(len(g["devices"]))
                for g in groups)
    quality = round(100 * math.exp(-sigma / _MOBILE_K))
    if any(g["severity"] == "blocker" for g in groups):
        quality = min(quality, _MOBILE_CEILING_BLOCKER)
    elif any(g["severity"] == "major" for g in groups):
        quality = min(quality, _MOBILE_CEILING_MAJOR)
    return quality, counts
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/test_mobile_scoring.py -k quality -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add utils/layered_health_scores.py tests/unit/test_mobile_scoring.py
git commit -m "Add mobile Quality curve (severity x device-reach, ceilings)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Coverage metric, gate, and `compute()` integration

**Files:**
- Modify: `utils/layered_health_scores.py` (`LayeredScores` fields; `compute()` signature + mobile band + overall)
- Modify: `conftest.py` (`compute_scores(...)` call ~line 376; import ~line 102)
- Test: `tests/unit/test_mobile_scoring.py`

**Interfaces:**
- Produces: `_mobile_coverage(check_stats: dict | None) -> float | None` — `None` = not instrumented (score on quality alone); a float in `[0,1]` otherwise (`0.0` when nothing is executable).
- Produces: `compute(records, clusters, mobile_findings=None, mobile_tested=None, check_stats=None)`.
- Produces: `LayeredScores` gains `mobile_quality: int | None = None`, `mobile_coverage: float | None = None`.
- Consumes: `mobile_report_builder.session_check_stats()` (Task 1) in `conftest.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_mobile_scoring.py`:

```python
import json, types
from utils.error_clusterer import ErrorCluster  # noqa: E402

def _cov(att, exe, na):
    return {"attempted": att, "executed": exe, "na_structural": na}

def test_structural_gaps_do_not_discount():
    from utils.layered_health_scores import compute
    fs = [_mk("minor","tap_target","iPhone SE","A")]
    # 100 attempted, 40 executed, 60 structural -> executable 40, coverage 40/40 = 1.0
    s = compute([], [], mobile_findings=fs, mobile_tested=True, check_stats=_cov(100, 40, 60))
    assert s.mobile_coverage == 1.0
    assert s.mobile_health == s.mobile_quality      # no discount

def test_nonstructural_crashes_discount():
    from utils.layered_health_scores import compute
    fs = [_mk("minor","tap_target","iPhone SE","A")]
    # 100 attempted, 60 executed, 0 structural -> executable 100, coverage 0.6
    s = compute([], [], mobile_findings=fs, mobile_tested=True, check_stats=_cov(100, 60, 0))
    assert abs(s.mobile_coverage - 0.6) < 1e-9
    assert s.mobile_health == round(s.mobile_quality * 0.6)

def test_coverage_gate_returns_none():
    from utils.layered_health_scores import compute
    fs = [_mk("minor","tap_target","iPhone SE","A")]
    s = compute([], [], mobile_findings=fs, mobile_tested=True, check_stats=_cov(100, 20, 0))  # 0.2 < 0.33
    assert s.mobile_health is None                  # insufficient coverage
    assert s.mobile_quality is not None             # quality still computed
    s2 = compute([], [], mobile_findings=fs, mobile_tested=True, check_stats=_cov(100, 0, 100))  # executable 0
    assert s2.mobile_health is None

def test_no_coverage_instrumentation_scores_on_quality():
    from utils.layered_health_scores import compute
    fs = [_mk("minor","tap_target","iPhone SE","A")]
    s = compute([], [], mobile_findings=fs, mobile_tested=True, check_stats=None)
    assert s.mobile_coverage is None
    assert s.mobile_health == s.mobile_quality

def test_no_mobile_is_v1_identity():
    from utils.layered_health_scores import compute
    rec = types.SimpleNamespace(status="PASS", feature="Marketing", harness_fault=False)
    s = compute([rec], [], mobile_findings=[], mobile_tested=False, check_stats=None)
    assert s.mobile_health is None
    assert s.scoring_version == 3
    assert s.overall == 100          # product 100, renormalised 50/30/20, no mobile term

def test_real_run_scores_match_spec():
    from utils.layered_health_scores import compute
    for path, exp in (("reports/trend/mobile-summary.json", 62),
                      ("reports/trend/webkit/mobile-summary.json", 59)):
        d = json.load(open(path))
        cs = d.get("check_stats")
        # older sidecars may still be 2-key; skip if na_structural absent
        if not cs or "na_structural" not in cs:
            import pytest; pytest.skip(f"{path} predates 3-way tally")
        s = compute([], [], mobile_findings=d["findings"], mobile_tested=True, check_stats=cs)
        assert s.mobile_health == exp
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_mobile_scoring.py -k "structural or crashes or gate or instrumentation or identity or real_run" -q`
Expected: FAIL (`compute() got an unexpected keyword argument 'check_stats'`).

- [ ] **Step 3: Add the coverage helper + LayeredScores fields**

In `utils/layered_health_scores.py`, add after `_mobile_quality`:

```python
def _mobile_coverage(check_stats):
    """coverage = executed / executable, executable = attempted − na_structural.
    None => not instrumented (cannot discount what we did not measure).
    0.0  => nothing was executable (engine could run nothing)."""
    if not check_stats:
        return None
    att = int(check_stats.get("attempted", 0) or 0)
    if att == 0:
        return None
    executable = att - int(check_stats.get("na_structural", 0) or 0)
    if executable <= 0:
        return 0.0
    return min(int(check_stats.get("executed", 0) or 0) / executable, 1.0)
```

Add two fields to the `LayeredScores` dataclass (next to `mobile_health`):

```python
    mobile_quality: int | None = None
    mobile_coverage: float | None = None
```

- [ ] **Step 4: Rewrite the mobile band in `compute()`**

Change the signature to add `check_stats: Sequence | None = None` (as `dict | None`):

```python
def compute(
    records,
    clusters,
    mobile_findings=None,
    mobile_tested=None,
    check_stats=None,
):
```

REPLACE the whole `# ── Mobile Health ──` block (the `mf = ...` through `mobile_health = max(0, 100 - penalty)`) with:

```python
    mf = list(mobile_findings or [])
    tested = bool(mf) if mobile_tested is None else bool(mobile_tested)
    mobile_health: int | None = None
    mobile_quality: int | None = None
    mobile_coverage: float | None = None
    m_blocker = m_major = m_minor = 0
    if tested:
        mobile_quality, counts = _mobile_quality(mf)
        m_blocker, m_major, m_minor = counts["blocker"], counts["major"], counts["minor"]
        cov = _mobile_coverage(check_stats)
        mobile_coverage = cov
        if cov is None:
            mobile_health = mobile_quality           # not instrumented: quality alone
        elif cov < _MOBILE_COVERAGE_GATE:
            mobile_health = None                      # insufficient coverage -> excluded
        else:
            mobile_health = round(mobile_quality * cov)
```

Add the two new fields to the `return LayeredScores(...)` (near `mobile_blocker=m_blocker`):

```python
        mobile_quality=mobile_quality,
        mobile_coverage=mobile_coverage,
```

The overall-weighting block is unchanged: it already keys on `mobile_health is None`, so an insufficient-coverage run correctly renormalises to `50/30/20`.

- [ ] **Step 5: Wire `conftest.py`**

At the import (~line 102) add `session_check_stats as _mobile_check_stats`:

```python
from utils.mobile_report_builder import (
    ...,
    session_check_stats as _mobile_check_stats,
)
```

At the `compute_scores(...)` call (~line 376), resolve the single running engine's stats and pass them:

```python
        _cs = _mobile_check_stats()
        _engine_cs = next(iter(_cs.values()), None) if _cs else None
        scores = compute_scores(records, clusters,
                                mobile_findings=_mobile_findings(),
                                mobile_tested=_mobile_ran(),
                                check_stats=_engine_cs)
```

- [ ] **Step 6: Run the tests + full unit suite**

Run: `.venv/bin/python -m pytest tests/unit -q -p no:cacheprovider`
Expected: PASS. `test_real_run_scores_match_spec` asserts Chromium 62 / WebKit 59 (or skips if the committed sidecars predate the 3-way tally — they do until Task 1's tally has produced a fresh run; that is acceptable, the synthetic tests cover the logic).

- [ ] **Step 7: Commit**

```bash
git add utils/layered_health_scores.py conftest.py tests/unit/test_mobile_scoring.py
git commit -m "Score mobile as Quality x coverage with evidence gate (v3)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Display the Quality × coverage breakdown

**Files:**
- Modify: `utils/dashboard_builder.py` (`_mobile_cell`, ~line 1983)
- Modify: `utils/pdf_report_builder.py` (`_mobile_score_box`, ~line 245)
- Test: `tests/unit/test_mobile_scoring.py`

**Interfaces:**
- Consumes: `LayeredScores.mobile_quality`, `LayeredScores.mobile_coverage` (Task 3).

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_mobile_scoring.py`:

```python
def test_dashboard_mobile_cell_shows_breakdown():
    from utils.dashboard_builder import _mobile_cell
    layered = types.SimpleNamespace(mobile_health=59, mobile_quality=59,
                                    mobile_coverage=1.0, mobile_blocker=0,
                                    mobile_major=0, mobile_minor=247, scoring_version=3)
    out = _mobile_cell(layered)
    assert "59" in out and "Quality" in out and "coverage" in out

def test_dashboard_mobile_cell_insufficient_coverage():
    from utils.dashboard_builder import _mobile_cell
    layered = types.SimpleNamespace(mobile_health=None, mobile_quality=59,
                                    mobile_coverage=0.2, mobile_blocker=0,
                                    mobile_major=0, mobile_minor=10, scoring_version=3)
    out = _mobile_cell(layered)
    assert "INSUFFICIENT" in out.upper()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_mobile_scoring.py -k "breakdown or insufficient_coverage" -q`
Expected: FAIL (no "Quality"/"coverage"/"INSUFFICIENT" text yet).

- [ ] **Step 3: Update `_mobile_cell`**

In `utils/dashboard_builder.py`, in `_mobile_cell`, replace the `if score is None:` branch and the final `counts`/return so it distinguishes "not measured" from "insufficient coverage", and adds a breakdown line when scored:

```python
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
```

- [ ] **Step 4: Update the PDF box the same way**

In `utils/pdf_report_builder.py`, in `_mobile_score_box`, replace the `if m is None:` branch's `sub` text and the scored return to add the breakdown:

```python
    m = getattr(scores, "mobile_health", None)
    quality = getattr(scores, "mobile_quality", None)
    cov = getattr(scores, "mobile_coverage", None)
    if m is None:
        sub = ("Mobile (insufficient coverage)"
               if quality is not None and cov is not None else "Mobile (not measured)")
        return (
            '<div class="score-box" style="background:#f1f5f9;'
            'border:1px dashed #94a3b866">'
            '<div class="score" style="color:#64748b">&mdash;</div>'
            f'<div class="sub" style="color:#64748b">{sub}</div>'
            "</div>"
        )
    _, color, bg = score_band(m)
    sub = (f"Mobile — Q{quality} × {round(cov * 100)}%"
           if quality is not None and cov is not None else "Mobile Health")
    return (
        f'<div class="score-box" style="background:{bg};border:1px solid {color}44">'
        f'<div class="score" style="color:{color}">{m}</div>'
        f'<div class="sub" style="color:{color}">{sub}</div>'
        "</div>"
    )
```

- [ ] **Step 5: Run the tests + full unit suite**

Run: `.venv/bin/python -m pytest tests/unit -q -p no:cacheprovider`
Expected: PASS (all, including the existing `test_dashboard_mobile_table_*` and PDF tests).

- [ ] **Step 6: Commit**

```bash
git add utils/dashboard_builder.py utils/pdf_report_builder.py tests/unit/test_mobile_scoring.py
git commit -m "Show mobile score as Quality x coverage breakdown

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Post-implementation validation (not a task — run after all four)

- [ ] Full unit suite green: `.venv/bin/python -m pytest tests/unit -q -p no:cacheprovider`
- [ ] One live engine run to regenerate a fresh sidecar with the 3-way tally, then confirm the dashboard shows `Mobile = Quality × coverage` and the number matches the spec (Chromium ≈ 62). Command:
  `caffeinate -dimsu .venv/bin/python -m pytest -m mobile -q --headless --browser chromium`
- [ ] Confirm `scoring_version` is `3` in the fresh trend entry and that prior v2 entries are unchanged.

## Self-Review (completed by plan author)

- **Spec coverage:** Quality curve (Task 2), severity ceilings (Task 2), hybrid single coverage metric + structural exclusion (Tasks 1+3), evidence gate → None (Task 3), SCORING_VERSION=3 + no-mobile v1 identity (Task 3), 3-way tally scope expansion (Task 1), display breakdown (Task 4), K/ceilings provisional comments (Task 2). Calibration plan is documentation-only (spec), no task needed.
- **Placeholder scan:** none — every step has literal code and a runnable command.
- **Type consistency:** `note_check_run(engine, executed, structural)` and the `{attempted, executed, na_structural}` shape match across Tasks 1/3; `_mobile_quality -> (int, dict)` and `_mobile_coverage -> float|None` used consistently; `mobile_quality`/`mobile_coverage` fields defined in Task 3 and read in Task 4.
