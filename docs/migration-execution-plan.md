# Migration Execution Plan — Java/Selenium → Python/Playwright

## Operating premise

**Decision B is assumed yes.** Full migration to Playwright is the
intended end-state. Reasons (these are organizational, not
performance-based — and that's fine):

- One automation stack instead of two reduces long-term maintenance.
- Playwright's auto-wait eliminates a class of failures (StaleElementReferenceException) the team has been spending real time on.
- pytest + Playwright is more standard in current automation hiring pools than TestNG + Selenium.
- The current Java suite's complexity around dropdowns, retries, and console monitoring is large enough that "stay in Java forever" carries its own maintenance cost.

This plan does NOT re-justify the migration at every phase. It treats
the migration as committed and structures the remaining work to
**minimize risk per engineering hour**.

What this plan IS:
- A phase-by-phase execution sequence
- Decision rules for when to pause / continue / rollback
- Risk-management gates between phases
- A realistic effort estimate (30–50 engineering hours)

What this plan IS NOT:
- A re-debate about whether to migrate
- A guarantee that every phase ships unmodified
- A commitment to deadlines (calendar dates depend on engineering bandwidth)

## Current state baseline

| Metric | Value |
|---|---:|
| Java test methods migrated | 19 |
| Python test methods passing | 19 |
| Bridge contract assertions | 7/7 green |
| Cumulative engineering invested | ~6 hours |
| Migrated classes | `AuthenticatedTest`, `PricingSmokeTest`, `HomepageSmokeTest`, `LoginTest`, `SignupTest` |
| Shared infrastructure built | pytest fixtures, JsConsoleMonitor (origin-aware), MarketingHeaderComponent, AuthState, snapshot writer, failure artifact capture, console_monitor fixture |
| Java classes remaining | ~21 (mix of cheap mirror-pattern, medium browser, heavy ConnectEditorPage, very-heavy ExploreMenu) |
| Estimated engineering remaining | 30–50 hours |

## The five-level hierarchy applied to this plan

| Level | This migration |
|---|---|
| **1. Decision rule** | Continue executing the plan unless a phase produces a result that materially changes risk assessment (rules at each phase below) |
| **2. Hypothesis** | Full Playwright migration is achievable in 30–50 engineering hours without unforeseen architectural blockers |
| **3. Evidence plan** | Per-phase risk gates (below); failure-attribution discipline applied to every failure |
| **4. Experiment design** | Phases are sequential; each freezes its predecessors. Cohort tagging keeps in-progress migrations separate from the stability-baseline cohort. |
| **5. Implementation** | The migration code itself |

## Sequencing principle

Phases are ordered by **risk-reduction-per-hour**, not by class count or completeness. The riskiest unknown (ConnectEditorPage page-object design) is front-loaded so its outcome informs everything downstream. The cheapest mirror-pattern work is back-loaded because it carries low risk and benefits from reusing every previously-built component.

## Phase 0a — R-1 Discovery (added 2026-06-05)

**Effort:** ~1–2 hours of conversation, 0 engineering.

**Purpose:** Resolve the dominant operational risk before committing
engineering to phases whose value depends on its answer. R-1
(Turnstile/auth automation) was elevated during Phase 0 from a
procedural note to the largest known unresolved operational
constraint. None of its possible outcomes depend on Selenium vs
Playwright — it's an environment/access question, not a framework
question.

**Hypothesis:** A one-hour conversation with the product/security
team can resolve more uncertainty than ten hours of migration work.

**Questions to answer:**
1. Does the team intend authenticated tests to be part of CI/CD long-term?
2. Is a sanctioned automation path available (test-account bypass, Turnstile allowlist, IP-based exemption)?
3. Is persistent authenticated state (cookies / `storage_state`) acceptable from a security standpoint?
4. If none of the above: is the operational model permanently human-attended for auth tests, and is that acceptable?

**Decision rule:**

| Answer to Q4 | Effect on rest of plan |
|---|---|
| Yes, CI is intended; bypass available (Q2) | Phases 2–6 proceed; bypass integration becomes part of Phase 2's design |
| Yes, CI is intended; storage_state path acceptable (Q3) | Phases 2–6 proceed; storage_state integration becomes part of Phase 2 |
| Yes, CI is intended; no mitigation available | **Pause.** The migration produces a partial-CI suite; team decides whether that's acceptable before Phase 2 work |
| No, human-attended is fine | Phases 2–6 proceed under status-quo workflow; cut-over is human-attended for auth tests indefinitely |

**Unlock condition for Phase 1:** R-1 Discovery questions answered;
decision recorded as a new section in `docs/risk-register.md`. This
unblocks Phase 1 *and* informs Phase 2 design simultaneously.

**Why this phase exists at all:** A one-hour conversation could
remove uncertainty that affects 30+ hours of downstream work. Doing
Phases 1–6 without this answer means risking that the entire
migration ships in a state that can't satisfy its eventual
operational target. The cost of asking is bounded; the cost of not
asking is potentially the value of the migration itself.

## Phase 0 — Observation protocol + stability baseline

**Effort:** ~2 engineering hours + 5 calendar days observation.

**Purpose:** Risk-reduction. Before investing 30+ more engineering hours, confirm the existing 19 methods are stable enough that further migration won't be undermined by foundational issues.

**Hypothesis:** The Python infrastructure (fixtures, snapshot writer, page objects, bridge contract) is stable across multiple runs in production-realistic conditions.

**Deliverables:**
1. `docs/observation-protocol.md` — formal protocol with:
   - The five-state failure-attribution taxonomy (`product` / `selector_drift` / `infrastructure` / `framework_timing` / `unknown`)
   - The three-state flake taxonomy (`observed` / `understood` / `unresolved`)
   - The impact-based decision rule (an `unknown` that could plausibly reverse the conclusion blocks; a single anomaly in 95 runs does not)
   - Per-failure inspection subprocess (read screenshot + DOM + console BEFORE categorizing)
2. Cohort tagging in `snapshot_writer.py` — one-line addition to mark records as `cohort: "stability-baseline"` vs `cohort: "experiment-<name>"`
3. Daily runs of the existing 19 methods for 5 consecutive days (or as continuous a window as the team can run)
4. End-of-window report applying the protocol

**Decision rule:**

| Outcome | Action |
|---|---|
| All 19 pass consistently; failures categorize cleanly | Proceed to Phase 1 |
| 1–2 understood flakes per window | Proceed to Phase 1; flag the flaky tests for retry-tolerance |
| `unresolved` failures that could reverse the migration conclusion | Pause; investigate before proceeding |
| Infrastructure-level breakage (e.g., snapshot writer fails) | Pause; fix infrastructure before adding more migrations |

**What this phase does NOT decide:** Whether to migrate at all. It only reduces the risk that the next 30+ hours are built on an unstable foundation.

## Phase 1 — High-leverage diagnostic migration

**Effort:** ~2–3 engineering hours.

**Purpose:** Resolve specific Java failure-mode questions that affect how Phase 2's page-object design should be structured.

**Hypothesis:** Some of the failures currently observed in the Java suite are framework/tooling issues that Playwright handles differently. Specifically: the `StaleElementReferenceException` failures that dominate Java's ConnectEditorPage interactions.

**Target: `AppPairingTest`** (chosen because it's small, currently failing in Java with a clean assertion, and exercises selector-handling without requiring the full ConnectEditorPage port).

**Hypothesis table for this target:**

| Outcome | Interpretation | Effect on plan |
|---|---|---|
| Java fails, Playwright passes | Framework issue confirmed | Increases confidence in Phase 2 page-object investment |
| Java fails, Playwright fails same way | Product issue or selector drift | Phase 2 needs to plan for parallel product-side fix |
| Both fail differently | New investigation required | Phase 2 paused until understood |
| Both pass intermittently | Flakiness in upstream surface, not stack-specific | Phase 2 needs retry tolerance designed in |

**Decision rule:**
- Whichever quadrant the result lands in determines whether Phase 2 begins immediately or pauses for product/framework work.
- Do NOT migrate additional diagnostic targets in this phase. Phase 1's job is to inform Phase 2, not to maximize diagnostic coverage.

## Phase 2 — ConnectEditorPage design + first workflow test

**Effort:** ~10–14 engineering hours. This is the largest single phase.

**Purpose:** Build the ConnectEditorPage Python equivalent and validate it against one workflow test. Every subsequent workflow test reuses this work.

**Hypothesis:** The Connect editor's multi-step dropdown / choices-container / custom-value flows can be expressed cleanly in Playwright, with comparable or better reliability than Java's Selenium-based implementation.

**Sub-phases (sequential within Phase 2):**

| Sub-phase | Scope | Effort |
|---|---|---:|
| 2a. Page-object architecture | Read the Java ConnectEditorPage (~2000 lines). Identify the distinct interaction patterns (search-and-select app, click event label, continue variants, dropdown handling, choices-container fields, readmorebutton2 chips, contenteditable fields, direct inputs). Design the Python module layout. | 2–3 hr |
| 2b. Core page-object port | Implement `pages/connect_editor_page.py` with the methods needed for `CreateConnectWorkflowTest`. Skip Mindbody-specific custom-value handlers; those come in Phase 3. | 4–6 hr |
| 2c. Migrate `CreateConnectWorkflowTest` | Port the 17-step Google Sheets → Gmail Draft workflow as the validation target. | 2–3 hr |
| 2d. End-to-end run + tuning | Run the migrated test headed, debug selector / timing issues with captured artifacts. | 2 hr |

**Decision rule:**
- If `CreateConnectWorkflowTest` passes end-to-end (matching the Java equivalent's success rate within a 2-run window): proceed to Phase 3.
- If it fails consistently due to ConnectEditorPage design issues (not product-side flakiness): redesign within Phase 2 budget; do NOT proceed to Phase 3 until the page-object pattern is proven.
- If it fails due to product-side flakiness (same way as the Java equivalent): proceed to Phase 3 — the ConnectEditorPage works; the product is the variable.

**Hard cost cap:** 14 hours. If we exceed it, that's a signal the ConnectEditorPage design is wrong and needs Phase-0-style re-evaluation, not more implementation.

## Phase 3 — Workflow test family expansion

**Effort:** ~4–6 engineering hours total. Drops dramatically because ConnectEditorPage is built.

**Purpose:** Port the remaining workflow tests that share ConnectEditorPage as the foundation.

**Targets in order:**

| Target | Effort | Why this order |
|---|---:|---|
| `GoHighLevelMindbodyConnectTest` | ~3 hr (Mindbody field-mapping is new; rest reuses Phase 2) | Already exists in Java; adds custom-value handling to ConnectEditorPage |
| `HomepageExhaustiveTest` | ~2 hr (different surface than Connect editor; may use marketing infrastructure instead) | Verifies the Connect-editor work didn't accidentally couple to non-Connect tests |

**Decision rule per target:**
- Each test must pass end-to-end with the same success rate as its Java equivalent before declaring done.
- If a test surfaces a new ConnectEditorPage interaction not covered in Phase 2, that interaction gets added to the page object and Phase 3 effort grows correspondingly.

## Phase 4 — ExploreMenu test family

**Effort:** ~8–12 engineering hours.

**Purpose:** Port the long-running iteration-heavy test family. These are the longest-running tests in the Java suite (15-minute runtimes).

**Hypothesis:** Playwright's tracing and persistent-context features make iteration-heavy test debugging substantially easier than Selenium's session-per-iteration model.

**Sub-phases:**

| Sub-phase | Scope | Effort |
|---|---|---:|
| 4a. `ExploreMenuTestBase` port | Base class with iteration framework, 5-gate editor-ready check, login cycle between iterations | 3–4 hr |
| 4b. `ExploreMenuIntegrationTest` | Subclass A (Popular App Integrations) | 1–2 hr |
| 4c. `TopAppCombinationsTest` | Subclass B | 1 hr |
| 4d. `TrendingAppIntegrationsTest` | Subclass C | 1 hr |
| 4e. Long-run optimization | Add Playwright tracing for failed iterations; verify videos for full runs are manageable | 2–4 hr |

**Decision rule:**
- After 4a + 4b: a single subclass passing end-to-end proves the iteration framework. Decide whether to migrate B and C, or wait for evidence the base is stable across many iterations.
- After all four: measure wall-clock against Java equivalents. If Python is materially slower (>2×), investigate before declaring Phase 4 done.

## Phase 5 — Coverage migrations (cheap batch)

**Effort:** ~4–6 engineering hours total.

**Purpose:** Migrate the remaining low-risk tests now that all infrastructure exists.

**Targets:** `AppyPieNavigationTest`, `RebrandCompletionTest`, `ResponsiveTest`, `ErrorHandlingTest`, `AppDirectoryTest`, `HomepageLinksTest`, `PricingLinksTest`, `HomepageSeoTest`, `PricingSeoTest`, `HomepageFunctionalTest`, `PricingFunctionalTest`.

These can be batched because by Phase 5:
- All page-object base classes exist
- Marketing infrastructure is complete
- Console monitor classifier is proven
- Auth helpers exist for both authenticated and non-authenticated flows
- Patterns are repeated, not invented

**Decision rule:**
- Migrate in cost order (cheapest first).
- Each test must pass independently before moving to the next.
- Pause if a test surfaces a missing infrastructure piece (would indicate Phase 4 work was incomplete).

## Phase 6 — Java cut-over

**Effort:** ~4–8 engineering hours.

**Purpose:** Shift CI to the Python suite. Decommission the Java test surface (keeping scoring engine in Java per the hybrid model).

**Sub-phases:**

| Sub-phase | Scope | Effort |
|---|---|---:|
| 6a. Java DashboardBuilder reads Python snapshot | Wire the `python-health-snapshot.json` merge into the Java analytics pipeline so the unified dashboard shows Python test rows | 2–3 hr |
| 6b. CI configuration | Update GitHub Actions / Jenkins / whatever the CI is to run Python suite instead of Java tests | 1–2 hr |
| 6c. Archive Java test code | Move `src/test/java/testing/` to `src/test/java/.archive/testing/`; preserve git history; do NOT delete | 1 hr |
| 6d. Documentation updates | Update README, SYSTEM_DESIGN.md, scoring-system.md to reflect new test entrypoint | 1–2 hr |

**Decision rule:**
- 6a must work before 6b (CI shouldn't run Python tests without dashboard rendering them).
- 6c MUST be reversible — archive, don't delete — for at least 90 days after cut-over.
- Rollback path: if a critical issue surfaces post-cut-over, restore Java tests from the archive and re-enable in CI within 1 day.

## What stays Java (permanent hybrid component)

Per the hybrid model that the conversation has converged on, these
remain in Java forever (unless a future Phase 0-style re-evaluation
changes the decision):

| Component | Why it stays |
|---|---|
| `HealthPolicy`, `HealthTracker`, scoring algorithm | ~6 months of investment; algorithm is now complex (C1–C4); rewriting is 8+ weeks of risk for no behavioural gain |
| `DashboardBuilder` HTML output | ~2500 lines; Jinja2 port is a rewrite, not a translation |
| PDF report builders (openhtmltopdf) | No clean Python equivalent; weasyprint produces different output |
| B4 schema migrator + C4 cluster scoring | Sealed-interface architecture relies on Java's compile-time exhaustiveness |
| Bridge contract test (`PythonSnapshotBridgeTest`) | This is the test that prevents the contract from drifting |

The Java surface area shrinks from ~80% of the codebase to ~50%
(scoring + dashboard + reports + bridge verification only). The
test-driving surface area becomes 100% Python.

## Risk register

| Risk | Probability | Mitigation |
|---|---|---|
| ConnectEditorPage port takes more than 14 hours | Medium | Hard cap; pause and redesign if exceeded |
| ExploreMenu iteration framework doesn't translate well | Medium-Low | 4a alone gates the rest of Phase 4 |
| Cohort contamination during parallel migration + observation | Low | Cohort tagging in snapshot writer (Phase 0) |
| Cut-over reveals unforeseen CI integration issue | Medium | Archive (not delete) old tests; 1-day rollback path |
| Scope creep into Java scoring engine port | Low (Phase 0 discipline) | Phase 7 explicitly NOT in this plan |
| Phase 5 batch reveals missing infrastructure | Medium-Low | Pause batch; backfill infrastructure; resume |
| Team capacity for 30–50 hours doesn't materialize | High | Plan is phase-independent; each phase can be paused indefinitely without invalidating prior phases |

## Total estimated effort

| Phase | Estimate (hours) |
|---|---:|
| Phase 0 — Observation protocol + baseline | 2 |
| Phase 1 — Diagnostic migration | 2–3 |
| Phase 2 — ConnectEditorPage + first workflow | 10–14 |
| Phase 3 — Workflow test family | 4–6 |
| Phase 4 — ExploreMenu family | 8–12 |
| Phase 5 — Coverage batch | 4–6 |
| Phase 6 — Cut-over | 4–8 |
| **Total** | **34–51 hours** |

Aligns with the earlier 30–50 hour estimate. Within each phase, the
upper bound is the realistic-with-debug number; the lower bound
assumes no surprises.

## What this plan is explicitly NOT doing

To prevent scope creep, the following are EXCLUDED from this plan:

1. **Java scoring engine port** (Phase 7 in the original migration plan). Stays Java permanently unless a future business decision changes that.
2. **Dashboard HTML rewrite.** The Java dashboard reads Python snapshot data via the bridge; the dashboard itself stays Java.
3. **PDF report port.** Same reason.
4. **Performance optimization of the Python suite during migration.** Optimization is a separate workstream after Phase 6 cut-over.
5. **Test design changes during port.** The Python port mirrors Java exactly. Any test improvements (better selectors, additional assertions, better fixtures) come after Phase 6.

## Recommended cadence

Without a specific calendar:

- Phase 0 can start immediately. It produces results in 5 calendar days.
- Phase 1 follows Phase 0; ~half a day of engineering once Phase 0 results are in.
- Phase 2 should be done in one focused multi-day block, not interleaved with other work. The ConnectEditorPage design needs context retention.
- Phases 3, 4, 5 can each be done in 1–3 focused blocks.
- Phase 6 should be done with the team available to monitor cut-over and execute rollback if needed.

Realistic calendar from a standing start, if engineering capacity is ~5–8 hours per week:
- Phase 0 — 1 week
- Phase 1 — 1 week
- Phase 2 — 2–3 weeks
- Phase 3 — 1–2 weeks
- Phase 4 — 2–3 weeks
- Phase 5 — 1–2 weeks
- Phase 6 — 1 week
- **Total: ~9–13 calendar weeks** at part-time engineering capacity.

If dedicated engineering capacity (~30 hours/week) becomes available,
the whole plan compresses to ~3–5 weeks.

## How this plan differs from the original migration plan

The original `docs/python-playwright-migration-plan.md` in
`automate-workflow-test/` was written *before* any migration work
existed. This plan supersedes it for execution. Key differences:

| Original plan | This plan |
|---|---|
| Phase 0 was a "should we migrate?" decision gate | Phase 0 is operationalized as observation protocol + stability baseline |
| Phase 3/4 had a 5-day comparison gate as the unlock condition | Comparison data is risk reduction, not unlock gate — full migration is committed to |
| Phase 7 (scoring engine port) was opt-in | Phase 7 is explicitly excluded from this plan |
| Effort estimates were per-phase but no total | Total: 34–51 hours; per-phase caps and overflow rules defined |
| No hypothesis tables per phase | Every phase has explicit hypotheses + decision rules |

## Reference

- Original migration plan: `../automate-workflow-test/docs/python-playwright-migration-plan.md`
- Evidence-first analysis framework: `../automate-workflow-test/docs/evidence-first-analysis.md`
- Snapshot contract: `docs/snapshot-contract.md`
- Java scoring system: `../automate-workflow-test/docs/scoring-system.md`

## When to revise this plan

- After Phase 0 completes — adjust later-phase estimates based on observed stability
- After Phase 1 result — adjust Phase 2 design based on diagnostic outcome
- After Phase 2 first workflow port — recalibrate Phase 3/4 estimates with real-world ConnectEditorPage experience
- After any phase exceeds its cost cap — pause; revise plan; resume

Do NOT revise this plan to expand scope without an explicit Phase
0-style re-evaluation. Phase 7 stays excluded unless an explicit
business decision changes that.
