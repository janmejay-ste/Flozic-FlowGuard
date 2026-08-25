# Mobile Scoring v3 — Design

Status: revised per review gate 2026-08-25 — pending FINAL spec review before implementation
Date: 2026-08-25
Scope: the mobile band of `utils/layered_health_scores.py`, its call site, the
mobile display, and a small refinement to the coverage tally (see Scope note).
No auditor logic, dashboard-grouping, or report-shape changes ride along — those
are already committed and frozen, so any score movement is attributable to this
change alone.

## Problem

v2 scores the mobile band from a flat per-finding penalty
(`minor 3 / major 8 / blocker 25`, capped at 60). On real data this saturates:
247 minor findings × 3 = 741, capped to 60 → Mobile 40. A 20-issue run and a
247-issue run both land on 40, and two engines that found the identical issues
but exercised different amounts of the suite score identically.

| engine   | findings | patterns | check tally (att/exec) | Mobile v2 |
| -------- | -------: | -------: | ---------------------: | --------: |
| chromium |      247 |       75 |          216 / 216     |        40 |
| webkit   |      247 |       86 |          216 / 144     |        40 |

## Goal

A mobile score that (a) discriminates a light run from a heavy one,
(b) reflects coverage that *should* have run but didn't (flakes/crashes)
WITHOUT punishing an engine for checks it structurally cannot run, and
(c) treats a rare-but-severe issue as worse than a pile of cosmetic ones —
no hard floor, no conflation.

## Model

```
Mobile = None                       if executable == 0 or coverage < 0.33
       = round(Quality × coverage)  otherwise
```

### Quality (0–100) — from unique issue patterns, not raw findings

```
Quality = min(severity_ceiling, round(100 × exp(−Σ weights / K)))
```

- **Unit: patterns.** Findings collapsed with the committed
  `utils.ai_mobile_triage.group_findings()` (device-blind, number-blind).
  Verified on real data: 247 actionable findings → 86 patterns with **zero
  collisions** — every pattern is one issue repeated across devices/sizes,
  distinct elements stay distinct (e.g. `tap_target` is 82 separate patterns).
  `info` findings are excluded (coverage-gap notices must never penalise).
- **Per-pattern weight** = `severity_base × reach_mult`
  - `severity_base`: `minor 1.0`, `major 5.0`, `blocker 12.0`
  - `reach_mult = 0.5 + (n_devices − 1) / max(total_devices − 1, 1)` → linear
    0.5 (one device) → 1.5 (all devices); `max(…,1)` guards a single-device
    matrix. This is the "reproducibility" signal we have (reproduces across the
    matrix). Cross-run reproducibility is out of scope (needs trend plumbing).
- **K = 120 — PROVISIONAL, explicitly uncalibrated.** One run cannot calibrate
  a curve's steepness; K=120 is a single anchor (this run → Quality ≈ 60).
  Interpretably: in `100·exp(−Σ/K)`, K=120 means **Quality = 50 at Σ ≈ 83
  weighted patterns**. Shipped as provisional with a calibration plan (below),
  not presented as empirical.
- **severity_ceiling** = `20 if any blocker else 55 if any major else 100`.
  The worst severity present sets a ceiling; volume erodes within it. A lone
  major → ≤55 regardless of how clean everything else is; a lone blocker → ≤20
  (a blocker also fails the mobile test outright). **PROVISIONAL:** this run
  has 247 findings, all minor — zero major/blocker mobile findings exist, so
  55/20 are conservative rules, not fitted values. Calibrate when real
  major/blocker data appears.

### coverage — hybrid: one metric, discounts only what should have run

The coverage tally splits every interaction-check invocation three ways:

- **executed** — ran to a real conclusion (incl. content-driven "nothing
  applicable here", which counts as executed: we looked).
- **na_structural** — the engine literally cannot run it (UNTESTED /
  UNSUPPORTED: the CDP-gated touch/scroll/pan/sticky/network families on
  WebKit). Reported separately as N/A coverage gaps; **excluded from the
  denominator**.
- **failed_nonstructural** — attempted but crashed/flaked ("NOT verified").
  Stays in the denominator as a real coverage gap.

One coverage metric, used both to gate and to discount:

```
executable = attempted − na_structural
coverage   = min(executed / executable, 1.0)     # executable > 0
```

- **Structural N/A excluded from `executable`** — an engine is never
  penalised for CDP-gated checks it cannot run; those are reported separately
  as N/A gaps and touch neither the gate nor the discount denominator.
- **coverage never drops for structural reasons** — it is 1.0 in a clean run
  and falls only when *runnable* checks flake or crash.
- **No 0.5 floor.** A single coverage metric both gates and discounts:
  - **Gate:** `executable == 0` OR `coverage < 0.33` → **Mobile = None**,
    marked "insufficient coverage to score" (never a partial score). Same
    honesty as v2's "not measured", not a fabricated discounted number.
  - **Discount:** otherwise `Mobile = round(Quality × coverage)`.
- Two separate denominators (a prior draft) collapse to this one anyway once
  structural is excluded from both, so a single metric is kept. The
  "almost-everything-structural" thinness case is not separately gated; it is a
  non-scenario with current engines (WebKit is 144/216 = 67% executable) and
  can be revisited if a future engine is that limited. This run: Chromium
  `coverage` 216/216 = 1.00, WebKit 144/144 = 1.00 — both clear the gate.
- **Why hybrid:** penalising WebKit for CDP-impossible checks mixes product
  quality with test-framework capability and makes the score misleading. An
  engine is judged only on what it *can* run; structural gaps are surfaced as
  N/A, not as a score penalty. The discount then bites only on flakes/crashes
  — coverage that genuinely should have run and didn't.

### Composite (unchanged from v2)

- Mobile keeps weight **0.15**
  (`product 0.40 / infra 0.25 / framework 0.20 / mobile 0.15`).
- `mobile_health is None` → weights renormalise to v1 exactly (`50/30/20`), so
  a non-mobile run scores identically under v2 and v3.
- `SCORING_VERSION = 3`.

## Worked examples (real committed data, these constants, HYBRID coverage)

- **chromium**: 75 patterns, Σ≈57.9, ceiling 100 → Quality 62; executable
  216/216 → coverage 1.0 → **Mobile 62**.
- **webkit**: 86 patterns, Σ≈62.4, ceiling 100 → Quality 59; 72 gaps are
  **all structural** (h_pan/scroll/sticky/network, 0 crashes) → executable =
  216 − 72 = 144, coverage 144/144 = 1.0 → **Mobile 59**.
- The earlier multiplicative model gave WebKit 39 — that drop was purely the
  CDP-impossible checks, i.e. the exact product-vs-capability confusion the
  hybrid removes. Chromium (62) and WebKit (59) now differ only by their small
  quality gap (WebKit visited app_directory and found 11 more patterns).
- Discrimination probes (Quality): 20 single-device minors → 92; this run →
  ~60; one cross-device major → min(55, 94) = 55; any blocker → ≤20.
- The coverage discount now fires only when checks flake/crash. Example: if 20
  of 144 executable WebKit checks had crashed, coverage 124/144 = 0.86 →
  Mobile 59 × 0.86 = 51.

## Calibration plan (K and severity ceilings)

Both are shipped provisional and must be refit against real data, not guessed:

1. Collect mobile runs spanning quality bands as the product changes.
2. Agree anchors: what Σ (weighted-pattern load) should read "clean" (~90),
   "mediocre" (~60), "poor" (~30); fit K to them.
3. Once real major/blocker mobile findings exist, calibrate the 55/20 ceilings
   against how severe those observed cases actually are.
4. Any refit bumps `SCORING_VERSION` again so trend history stays interpretable.

## Honesty constraints

- **No user-impact multiplier** — category is a poor proxy for element role;
  it would be a guess and double-count severity. Its home is the auditor
  (severity bump by element role), a separate later change.
- **No confidence multiplier** — the only confidence signal is the AI triage's,
  and the invariant is *AI never affects scoring*. Auditor findings are
  deterministic.
- **K and ceilings are provisional** — see calibration plan.

## Scope note (honest expansion)

The hybrid coverage decision requires the coverage tally to distinguish
structural (engine-impossible) from non-structural (crash/flake) not-executed
checks. So v3 touches, beyond the scorer:

- `utils/mobile_interaction_auditor.py` `_tallied` — classify a not-executed
  check as `na_structural` (message says UNTESTED/UNSUPPORTED) vs
  `failed_nonstructural` (says "NOT verified").
- `utils/mobile_report_builder.py` `note_check_run` / `session_check_stats` /
  the sidecar `check_stats` — record `{attempted, executed, na_structural}`.

This is tightly scoped to the coverage concern; no check's pass/fail behaviour
changes. Verified today: WebKit's 72 not-executed checks are 78 structural
notices / 0 crashes, so the split is well-defined on real data.

## Implementation surface

- `utils/layered_health_scores.py` — replace `_MOBILE_PENALTY`/`_MOBILE_PENALTY_CAP`
  with the quality-curve + severity-ceiling + hybrid coverage discount + gate;
  `SCORING_VERSION = 3`; `compute()` gains a `check_stats: dict | None` input.
- `utils/mobile_interaction_auditor.py` + `utils/mobile_report_builder.py` —
  the 3-way tally (Scope note).
- `conftest.py` — pass `session_check_stats()` into `compute()` at teardown.
- `utils/dashboard_builder.py` + `utils/pdf_report_builder.py` — render Mobile
  as a one-line breakdown (`Mobile 59 = Quality 59 × 100% executable coverage`);
  N/A structural gaps already show in the stat strip's coverage-gaps panel.

## Testing

- Curve discrimination: 20-pattern vs 86-pattern runs score apart.
- Severity ceilings: one major → ≤55; one blocker → ≤20 regardless of volume.
- Coverage metric: structural gaps do NOT reduce `coverage` (WebKit-shaped
  input, 0 crashes → coverage 1.0); non-structural crashes DO reduce it.
- Gate: `executable == 0` → None; `coverage < 0.33` → None (marked insufficient
  coverage, never a partial score). Structural N/A excluded from `executable`,
  so it drives neither the gate nor the discount.
- No-mobile run: v3 overall == v1 overall (renormalisation identity).
- Trend chart: `mobile_health is None` entries are skipped, never plotted as 0.
- `scoring_version == 3` stamped on result and trend entry; v2 history
  untouched (snapshot writer is append-only — verified).

## Out of scope

User-impact weighting, any auditor severity logic, cross-run reproducibility,
real-device execution, re-tuning composite domain weights, and refitting K /
ceilings (that is the calibration plan, done later with data).
