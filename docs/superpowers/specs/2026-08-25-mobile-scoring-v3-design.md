# Mobile Scoring v3 — Design

Status: approved in brainstorming, pending spec review
Date: 2026-08-25
Scope: isolated change to the mobile band of `utils/layered_health_scores.py`
and its call site + display + tests. No auditor, dashboard-grouping, or
report-shape changes ride along — those are already committed and frozen so
that any score movement is attributable to this change alone.

## Problem

v2 scores the mobile band from a flat per-finding penalty
(`minor 3 / major 8 / blocker 25`, capped at 60). On real data this
saturates: 247 minor findings × 3 = 741, capped to 60 → Mobile 40. A 20-issue
run and a 247-issue run both land on 40, and two engines that found the
identical issues but exercised different amounts of the suite score
identically — hiding that one tested far less than the other.

Concretely, this run:

| engine   | findings | patterns | executed coverage | Mobile v2 |
| -------- | -------: | -------: | ----------------: | --------: |
| chromium |      247 |       75 |    216/216 = 100% |        40 |
| webkit   |      247 |       86 |     144/216 = 67% |        40 |

Both 40, despite WebKit leaving a third of its checks unexecuted.

## Goal

A mobile score that (a) discriminates a light run from a heavy one,
(b) reflects how much of the suite actually executed, and (c) treats a
rare-but-severe issue as worse than a pile of cosmetic ones — without a hard
floor and without conflating the two concerns opaquely.

## Model

```
Mobile = round(Quality × coverage_factor)          # multiplicative discount
```

### Quality (0–100) — from the unique issue patterns, not raw findings

```
Quality = min(severity_ceiling, round(100 × exp(−Σ weights / K)))
```

- **Unit: patterns.** Findings are collapsed with the already-committed
  `utils.ai_mobile_triage.group_findings()` (device-blind, number-blind).
  `info`-severity findings are excluded — they are coverage-gap notices and
  must never penalise (unchanged rule from v2).
- **Per-pattern weight** = `severity_base × reach_mult`
  - `severity_base`: `minor 1.0`, `major 5.0`, `blocker 12.0`
  - `reach_mult = 0.5 + (n_devices − 1) / max(total_devices − 1, 1)` → linear
    from **0.5** (one device) to **1.5** (all devices). The `max(…, 1)` guards
    a single-device matrix (would otherwise divide by zero); with one device
    total, every pattern is `reach_mult = 0.5`. This is the "reproducibility"
    signal we actually have: reproducing across the device matrix. A separate
    cross-run term is out of scope (needs trend plumbing).
- **K = 120** — the single data-calibrated knob. Anchored so this run
  (86 mostly-low-reach minor patterns, Σ≈61) lands Quality ≈ 60. Chosen from
  the real minor distribution, not guessed.
- **severity_ceiling** = `20 if any blocker else 55 if any major else 100`.
  The worst severity present sets a ceiling; volume erodes within it. This
  exists because a single global `K` tuned for minor volume makes one major
  score ~94 on the bare curve — absurd. The ceiling makes one major → ≤55,
  one blocker → ≤20 (a blocker also fails the mobile test outright).

### coverage_factor — the multiplicative discount

```
coverage_factor = clamp(executed / attempted, 0.5, 1.0)
```

- Sourced from `mobile_report_builder.session_check_stats()` — the
  per-invocation interaction-check tally (attempted vs executed), NOT inferred
  from findings. A check that runs clean emits no finding, so findings
  under-count execution.
- **Floored at 0.5**: incomplete coverage discounts the score, but at worst
  halves it — a run that executed only what it could still reports what it
  measured, rather than being annihilated toward zero.
- **Capped at 1.0**: coverage can discount, never boost.
- **`attempted == 0` → Mobile = None** ("not measured"), never 0. No mobile
  interaction checks ran, so there is no signal — same class of honesty as
  v2's `mobile_health = None`.

### Composite (unchanged from v2)

- Mobile keeps weight **0.15** in the overall composite
  (`product 0.40 / infra 0.25 / framework 0.20 / mobile 0.15`).
- When no mobile ran (`mobile_health is None`), weights renormalise to v1
  exactly (`50/30/20`), so a non-mobile run scores identically under v2 and
  v3 — the version bump is not a silent rescoring of the existing suite.
- `SCORING_VERSION = 3`.

## Worked examples (real data, these constants)

- **chromium**: 75 patterns, Σ≈57.9, ceiling 100 → Quality 62; coverage
  216/216 → 1.0 → **Mobile 62**.
- **webkit**: 86 patterns, Σ≈62.4, ceiling 100 → Quality 59; coverage
  144/216 = 0.667 (exact ratio, not the rounded 67% shown in the UI) →
  59 × 0.667 = 39.3 → **Mobile 39**.
- Discrimination probes (Quality only): 20 single-device minors → 92;
  this run → 60; one cross-device major → min(55, 94) = **55**; any blocker
  → ≤20.

The two engines now differ (62 vs 39) on near-identical quality, purely
because WebKit executed less — the intended behaviour.

## Honesty constraints (why the model is shaped this way)

- **No user-impact multiplier.** Impact keys on element role, which the check
  category is a poor proxy for; it would be a guess and would double-count
  severity. Dropped from the scorer. Its honest home is the auditor (bump
  severity by element role, where the selector is known) — a separate, later
  change.
- **No confidence multiplier.** The only confidence signal is the AI triage's,
  and the project invariant is *AI never affects scoring*. Auditor findings
  are deterministic. A confidence term would violate the invariant or be a
  fake constant.
- **Major/blocker weights and ceilings are un-calibrated by data** — this run
  has 247 findings, all minor; zero major/blocker mobile findings exist. The
  minor curve (`K`) is data-calibrated; the severity handling is a
  conservative *rule* (ceilings), to be refined when real major/blocker
  mobile findings appear. Marked provisional in code.

## Implementation surface

- `utils/layered_health_scores.py` — replace `_MOBILE_PENALTY` / `_MOBILE_PENALTY_CAP`
  with the quality-curve + severity-ceiling + coverage-discount; `SCORING_VERSION = 3`;
  `compute()` gains a `check_stats: dict | None` input (executed/attempted).
- `conftest.py` — pass `mobile_report_builder.session_check_stats()` into
  `compute()` at session teardown.
- `utils/dashboard_builder.py` + `utils/pdf_report_builder.py` — render the
  Mobile number as a one-line breakdown (`Mobile 39 = Quality 59 × 67% coverage`)
  so the multiplicative discount is not opaque. Coverage % already shows in the
  stat strip.
- `tests/unit/test_mobile_scoring.py` — new cases (see Testing).

## Testing

- Curve discrimination: 20-pattern vs 86-pattern runs score meaningfully apart.
- Severity ceilings: one major → ≤55; one blocker → ≤20 regardless of volume.
- Coverage discount: identical findings, 100% vs 67% coverage → different Mobile.
- Coverage floor: 10% coverage does not drive Mobile below Quality × 0.5.
- `attempted == 0` → `mobile_health is None`.
- No-mobile run: v3 overall == v1 overall (renormalisation identity).
- `scoring_version == 3` stamped on the result and the trend entry.

## Trend continuity

No code change. Each trend entry already carries `scoring_version`, so a v2
`40` and a v3 `39` are distinguishable and not silently compared on the chart.

## Out of scope

User-impact weighting, any auditor change, cross-run reproducibility, real-device
execution, and re-tuning the composite domain weights. Explicitly deferred.
