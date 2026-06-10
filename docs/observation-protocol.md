# Observation Protocol — Python Migration Stability Baseline

Phase 0 of `docs/migration-execution-plan.md`. This document
operationalizes the observation period that gates Phase 1.

**Purpose.** Reduce the risk that the next 30+ engineering hours
(Phases 1–6) are built on an unstable Python foundation. NOT a
go/no-go vote on the migration itself — full migration is already
committed to per the execution plan.

**Hypothesis.** The Python infrastructure (fixtures, snapshot writer,
page objects, bridge contract) is stable across multiple runs in
production-realistic conditions.

## What the observation window measures

The **baseline cohort**: the 19 currently-migrated Python test methods,
unchanged for the duration of the window.

| Cohort | Members | Operational profile | Frozen during window? |
|---|---|---|---|
| `baseline-smoke` | 18 methods — Pricing/Homepage smoke + LoginEntry/SignupEntry | Unattended-headless capable | **YES — frozen** |
| `baseline-auth` | 1 method — `test_sanity_journey` | Human-attended-headed required (R-1) | **YES — frozen** |
| `experiment-*` | Any future migrations | Variable | Separate; do not affect baseline measurement |

These two baseline cohorts are **NOT aggregated** in the
end-of-window report. They're operationally different populations:

- `baseline-smoke` is the suite that could run in CI today
- `baseline-auth` is the suite that requires R-1 mitigation before CI

Aggregating "19 of 19 baseline tests pass" hides the fact that only
18 of them can run unattended. Per the user-corrected discipline:
*"18/18 unattended + 1/1 headed is not the same as 19/19 unattended."*

Anything outside the baseline cohort can change during the window —
that's what cohort tagging enables. But the baseline cohort itself is
locked. No selector tweaks, no timeout adjustments, no "while I'm
here" refactors to baseline test code.

## Operational steps

### 1. Run setup

Each day during the observation window, execute the baseline cohort:

```powershell
cd "C:\Users\Janmejay\OneDrive\Desktop\automate-workflow-py"
.\.venv\Scripts\Activate.ps1
pytest --headed --cohort baseline
```

Save the resulting `python-health-snapshot.json` with a date suffix:

```powershell
Copy-Item reports\trend\python-health-snapshot.json `
  reports\trend\observation\baseline-YYYY-MM-DD.json
```

(Create `reports\trend\observation\` first if it doesn't exist.)

### 2. Per-failure inspection

For every failure encountered during the window:

1. Open the failure artifact at `reports\failures\<test>_<timestamp>\`
2. Inspect: `screenshot.png`, `dom.html`, `console.log` (if present), `url.txt`
3. **Categorize** the failure (taxonomy below)
4. Log the inspection in `docs/observation-log.md` with date, test, category, and the one-sentence evidence-based reason

Do NOT categorize from the test name or assertion message alone. The
artifact is the input; the category is the output. This subprocess is
non-negotiable — without it, the window's conclusion is a vibes call.

### 3. Failure categorization taxonomy

| Category | Definition | Decision impact |
|---|---|---|
| `product` | A real product-side defect (broken page, JS error, auth flow broken). Reproducible with manual testing. | Same impact as a Java failure of the same kind. Doesn't undermine migration; might gate a feature. |
| `selector_drift` | A selector that matched yesterday doesn't match today because the product's DOM changed. | Coverage maintenance issue. Doesn't undermine migration; signals the test needs a more durable selector. |
| `infrastructure` | Network blip, DNS issue, Cloudflare 503, CI runner OOM, browser failed to launch. | Discount from the stability conclusion. NOT migration-relevant. |
| `framework_timing` | Test failed because of a race in test code (insufficient wait, click on disappearing element). | Migration-relevant. Reflects on Playwright/Selenium difference if it's a class only one stack hits. |
| `unknown` | Cannot determine cause from the artifact within reasonable investigation time. | **Triggers re-inspection**, not automatic inconclusive verdict. See impact-based rule below. |

### 4. Flake taxonomy (three-state)

| Flake state | Definition |
|---|---|
| `observed` | A test flipped status during the window (e.g., PASS on day 2, FAIL on day 3, PASS on day 4) |
| `understood` | Root cause identified, traced to a specific category above |
| `unresolved` | Cause unknown after investigation |

A single `observed` flip is not automatically a blocker. The blocker
is `unresolved` flips weighted by impact.

## Decision rules (impact-based)

### Rule for `unknown` failures

NOT "any unknown blocks." The impact-weighted version:

> An observation window is inconclusive when **unresolved unknowns
> are large enough — in count, severity, or clustering — that they
> could plausibly reverse the conclusion**.

Examples:

| Scenario | Decision |
|---|---|
| 1 unknown failure in 95 baseline executions; Java has 7 failures across same period | **Conclusive in favor of migration.** The unknown is noise relative to the comparative signal. |
| 3 unknown failures, all in the same test, all consistent shape | **Inconclusive.** The clustering suggests something systemic; investigate before concluding. |
| 5 unknown failures distributed across 5 different tests | **Inconclusive.** Could indicate framework-side instability. |
| 0 unknown failures, 4 understood-infrastructure failures | **Conclusive.** Infrastructure noise doesn't reflect on the migration. |

### Rule for "is the migration approach valid?"

| Outcome | Action |
|---|---|
| Baseline pass rate ≥ 95% across the window; all failures categorize cleanly | **Proceed to Phase 1.** Migration approach is validated. |
| Baseline pass rate < 95% but all failures are `infrastructure` or `product` | **Proceed to Phase 1.** Failures aren't migration-relevant. |
| Baseline pass rate < 95% with `framework_timing` or `unknown` failures clustered in newly-migrated test classes | **Pause.** Investigate before proceeding. |
| Baseline pass rate < 90% under any categorization | **Pause.** Infrastructure-level issue. |
| `unresolved` flakes ≥ 2 across the window | **Pause.** Investigate. |

### Rule for "Java vs Python comparison"

Only one Python test class has a direct Java counterpart that runs in
the same window: `AuthenticatedTest` (we have 3 Java runs from earlier
in this conversation; Python has 3 runs at the same scope).

| Java vs Python pattern | Interpretation |
|---|---|
| Java fails consistently, Python passes consistently | Strong evidence Python is more reliable for this app surface |
| Java passes consistently, Python passes consistently | Migration is at least neutral; lifecycle benefits justify continuing |
| Java passes, Python fails | Investigate before proceeding |
| Both flaky in similar ways | Product-side instability; not migration-relevant |

The Python smoke tests (PricingSmokeTest, HomepageSmokeTest) don't
have direct same-window Java equivalents during this observation —
they're net-new Python coverage. They're tracked in the baseline pass
rate but not in the comparison column.

## What this window does NOT decide

1. Whether to migrate at all (Decision B already = yes per execution plan).
2. Whether Phase 2 (ConnectEditorPage port) is safe — Phase 1's diagnostic migration informs that.
3. Whether the Java scoring engine should be ported (Phase 7 is explicitly excluded).
4. Whether the hybrid bridge contract holds — that's tested separately by `PythonSnapshotBridgeTest`.

## Calendar requirements (MINIMUM, not unlock condition)

The numeric thresholds below are **necessary but not sufficient** —
the actual unlock is the evidence threshold (decision rules above).

- **Minimum:** 5 runs across at least 3 distinct calendar days.
- **Preferred:** 5 runs across 5 distinct calendar days (one per day).
- **Acceptable variation:** 7 runs across 5 days (e.g., morning + afternoon some days).

Time-of-day matters because the Appy Pie auth flow has timing variance
(observed earlier: 44s vs 84s vs 134s for the same code, same day, no
changes). Multiple time slots reduce the chance that all observations
hit the same window of upstream conditions.

### What "minimum but not unlock" means in practice

| Runs | Result pattern | Conclusion |
|---|---|---|
| 5 | Stable, all failures categorize | Proceed (minimum met AND evidence supports) |
| 5 | 4 unexplained failures in last 2 runs | **Do NOT proceed.** Numeric minimum met but evidence does not support. Investigate or extend window. |
| 3 | Perfectly stable | Do not proceed yet — numeric minimum not met. |
| 10 | Mixed; cause unclear after investigation | Extend window OR conclude inconclusive. More runs alone won't help if the cause isn't identified. |

The protocol's end-of-window report must **explicitly state** which
condition (numeric OR evidence) is met and which is not. "5 runs done"
is not a conclusion. "5 runs done AND failure attribution complete
AND no decision-reversing unknowns" is a conclusion.

## End-of-window report template

The protocol concludes with a written report at `docs/observation-window-report.md`:

```markdown
# Observation Window Report — YYYY-MM-DD to YYYY-MM-DD

## Runs

| Date | Time | Total | Pass | Fail | Wall-clock |
|---|---|---:|---:|---:|---:|
| ... | ... | ... | ... | ... | ... |

## Pass rate
Baseline cohort: X / Y across all runs (Z%)

## Flake summary
- Observed flips: N
- Understood: M (categorized as ...)
- Unresolved: P

## Failure attribution
| Category | Count | Tests affected |
|---|---:|---|
| product | ... | ... |
| selector_drift | ... | ... |
| infrastructure | ... | ... |
| framework_timing | ... | ... |
| unknown | ... | ... |

## Conclusion
Apply the decision rules above. State explicitly:
- Did baseline pass-rate meet the threshold?
- Are unresolved unknowns weighted to reverse the conclusion?
- Proceed to Phase 1 / pause / investigate?
```

## Anti-patterns to avoid

| Don't | Why |
|---|---|
| Modify baseline tests during the window | Contaminates the measurement |
| Add new test classes to baseline cohort mid-window | Same |
| Re-run a failed test "to see if it passes now" without recording the first failure | Loses the original failure artifact |
| Conclude "the window was clean" without filling out the report | Vibes calls aren't evidence |
| Skip categorization for failures that "look obvious" | The artifact is the input; assumptions aren't |
| Treat infrastructure noise as a migration verdict | Infrastructure failures don't reflect on the stack |
| Extend the window indefinitely waiting for "more data" | The rule is impact-weighted; once the conclusion is stable, the window ends |

## When this protocol is revised

- After Phase 0 completes — revise based on what was actually useful
- If a new failure category emerges that doesn't fit the five-state taxonomy
- If the impact-based rules produce a result that the team disagrees with on inspection (calibrate the thresholds)

This protocol applies to the Python migration's Phase 0. The same shape
generalizes to other multi-day observation windows (e.g., C4 noisy-suite
evidence collection in the Java repo) — see `evidence-first-analysis.md`
for the framework.
